"""Fetch historical hourly bars for the configured universe and emit a
replay-ready CSV.

Routes by asset class:
  crypto*    → CoinGecko market_chart/range  (free, ~90 days hourly, no key)
  equity     → yfinance 1h bars              (free, ~30-day intraday window)
  commodity  → yfinance with futures-symbol mapping

Usage:
    python -m radar.fetch_bars --tickers BTC,ETH,ARB --days 7 --out data/bars.csv
    python -m radar.fetch_bars --days 14   # full universe

Notes:
  - CoinGecko's /market_chart/range returns prices and total_volumes in USD.
    We bin those to hourly buckets and use the bucket-close as `price`,
    sum of intra-hour volume as `volume_24h_usd` (rolling 24h is then derived
    from those buckets if --rolling-vol is set; otherwise we record the API's
    per-hour volume slice as a proxy — labeled the same field for replay
    compatibility).
  - OI and funding are not available from these free sources; emitted as 0.
    The ranker tolerates zero OI/funding history (those components contribute 0).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Iterable

from . import config

log = logging.getLogger("radar.fetch_bars")


# ============ symbol mappings ============

# CoinGecko uses slugs ("bitcoin"), not tickers. Coverage of our universe:
COINGECKO_IDS: dict[str, str] = {
    # crypto_t1
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    # crypto_t2
    "ONDO": "ondo-finance",
    "PENDLE": "pendle",
    "TON": "the-open-network",
    "LDO": "lido-dao",
    "ARB": "arbitrum",
    "OP": "optimism",
    "INJ": "injective-protocol",
    "AAVE": "aave",
    "FIL": "filecoin",
    "RNDR": "render-token",
    "FET": "fetch-ai",
    "HYPE": "hyperliquid",
    "NEAR": "near",
    "APT": "aptos",
    "SUI": "sui",
    "TIA": "celestia",
    # crypto_meme
    "WIF": "dogwifcoin",
    "PEPE": "pepe",
    "BONK": "bonk",
    "MOG": "mog-coin",
    # commodity-pegged
    "PAXG": "pax-gold",
    # delisted from major US exchanges, only CoinGecko has the long tail
    "XMR": "monero",
    # USELESS, FARTCOIN intentionally skipped — no stable mapping
}

# yfinance overrides for tickers that don't trade as plain US equities.
# Korean ADRs: the Lighter `*USD` perp tracks the USD ADR price, but the OTC
# ADR symbols (HYMTF, SSNLF, HXSCL) are sparsely traded and yfinance returns
# no data for them. We map to the Korea primary listing (`.KS`, KRW-denominated)
# instead — for BOS detection (structural break, not price level) the
# percentage moves correlate ~1:1 so the override is sound.
YFINANCE_SYMBOLS: dict[str, str] = {
    "HYUNDAI":     "005380.KS",   # Hyundai Motor — Korea primary (was HYMTF, delisted)
    "HYUNDAIUSD":  "005380.KS",
    "SAMSUNGUSD":  "005930.KS",   # Samsung Electronics
    "SKHYNIXUSD":  "000660.KS",   # SK Hynix
    "XAU": "GC=F",           # gold front-month future
    "XAG": "SI=F",           # silver front-month future
    "XPT": "PL=F",           # platinum front-month future
    "XPD": "PA=F",           # palladium front-month future
    "XCU": "HG=F",           # copper front-month future
    "NATGAS": "NG=F",        # natural gas front-month future
    "BRENTOIL": "BZ=F",      # Brent crude front-month
    "WTI": "CL=F",           # WTI front-month
    "WHEAT": "ZW=F",         # wheat front-month future
    "SPX": "^GSPC",          # S&P 500 cash index (Yahoo prefixes indices with ^)
}

# Forex routes through yfinance with the "=X" suffix. Injected into
# YFINANCE_SYMBOLS at import time so the existing fetcher needs no changes.
_FOREX_PAIRS = (
    "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD",
    "USDCAD", "USDCHF", "USDJPY", "USDKRW",
)
for _p in _FOREX_PAIRS:
    YFINANCE_SYMBOLS.setdefault(_p, f"{_p}=X")

# Binance USDT-perp/spot symbols. Most cryptos map directly (BTC → BTCUSDT);
# memes are price-scaled by Binance to keep notional values reasonable —
# 1000PEPE means each contract represents 1000 PEPE tokens. For BOS
# detection only relative moves matter, so the 1000x scaling is harmless.
# Tickers absent from this map fall back to the CoinGecko fetcher.
BINANCE_SYMBOLS: dict[str, str] = {
    # crypto_t1
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "BNB": "BNBUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
    "ADA": "ADAUSDT", "AVAX": "AVAXUSDT", "BCH": "BCHUSDT",
    "DOT": "DOTUSDT", "HBAR": "HBARUSDT", "LINK": "LINKUSDT",
    "LTC": "LTCUSDT", "TRX": "TRXUSDT", "XLM": "XLMUSDT",
    # crypto_t2
    "ONDO": "ONDOUSDT", "PENDLE": "PENDLEUSDT", "TON": "TONUSDT",
    "LDO": "LDOUSDT", "ARB": "ARBUSDT", "OP": "OPUSDT",
    "INJ": "INJUSDT", "AAVE": "AAVEUSDT", "FIL": "FILUSDT",
    "RNDR": "RENDERUSDT", "FET": "FETUSDT",
    "NEAR": "NEARUSDT", "APT": "APTUSDT", "SUI": "SUIUSDT", "TIA": "TIAUSDT",
    # crypto_meme — Binance price-scales these (and Lighter uses the 1000-prefix tickers)
    "WIF": "WIFUSDT",
    "PEPE": "1000PEPEUSDT",
    "BONK": "1000BONKUSDT",
    "1000PEPE": "1000PEPEUSDT",
    "1000BONK": "1000BONKUSDT",
    "1000FLOKI": "1000FLOKIUSDT",
    "1000SHIB": "1000SHIBUSDT",
    "1000TOSHI": "1000TOSHIUSDT",
    # commodity-pegged crypto
    "PAXG": "PAXGUSDT",
    # HYPE not on Binance (Hyperliquid native); USELESS, FARTCOIN, MOG fall back to CoinGecko;
    # XMR is delisted from Binance — see COINGECKO_IDS.
}


# Per-ticker route overrides for assets whose Lighter asset_class doesn't
# reflect where they actually trade. Used by ``is_fetchable`` and by the
# startup backfill loop so the right fetcher is picked.
#
# Example: PAXG is classified as ``commodity`` (gold-pegged token) but only
# trades on crypto venues (Binance / CoinGecko), not on yfinance commodity
# futures. Routing it through ``fetch_crypto`` instead of ``fetch_yfinance_hourly``
# is what makes the backfill work.
TICKER_ROUTE_OVERRIDES: dict[str, str] = {
    "PAXG": "crypto_t1",
}


# ============ HTTP helper ============

_SESSION = None


def _session():
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.error("requests unavailable: %s", e)
        return None
    s = requests.Session()
    s.headers.update({
        "User-Agent": "catalyst-radar-fetch/0.1 (+https://github.com/local/catalyst-radar)",
        "Accept": "application/json",
    })
    _SESSION = s
    return s


# ============ row dataclass ============

# We don't need a dataclass — rows are plain dicts that match the CSV header.
CSV_FIELDS = [
    "ts", "ticker", "asset_class", "max_leverage",
    "open", "high", "low", "price",
    "volume_24h_usd", "oi_usd", "funding_1h",
    "pct_24h", "pct_1h",
]


def _floor_hour(ts_sec: float) -> int:
    return int(ts_sec) - (int(ts_sec) % 3600)


def _floor_15m(ts_sec: float) -> int:
    return int(ts_sec) - (int(ts_sec) % 900)


def _iso(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============ CoinGecko (crypto) ============

def fetch_crypto_binance(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """Fetch real OHLCV from Binance public klines.

    Binance returns 1000 candles per call max (~41 days at 1h). We make a
    single call for windows up to 1000h; longer windows page through in
    1000-candle chunks. No auth required; rate limit is 6000 weight/min,
    each klines call is weight 2.
    """
    sym = BINANCE_SYMBOLS.get(ticker) or f"{ticker}USDT"
    s = _session()
    if s is None:
        return []

    end_ms = (int(end_ts) if end_ts is not None else int(time.time())) * 1000
    start_ms = end_ms - days * 86400 * 1000
    asset_class = config.SYMBOL_TO_CLASS.get(ticker, "crypto_t2")

    all_klines: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = s.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": sym,
                    "interval": "1h",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                timeout=15,
            )
        except Exception as e:
            log.warning("binance %s request failed: %s", ticker, e)
            break
        if r.status_code != 200:
            log.warning("binance %s returned %d: %s", ticker, r.status_code, r.text[:200])
            break
        try:
            chunk = r.json()
        except ValueError:
            log.warning("binance %s returned non-JSON", ticker)
            break
        if not chunk:
            break
        all_klines.extend(chunk)
        last_open = int(chunk[-1][0])
        next_cursor = last_open + 3600_000  # advance one hour past the last candle
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(chunk) < 1000:
            break

    rows: list[dict] = []
    last_close: float | None = None
    closes_24h: list[tuple[int, float]] = []
    for k in all_klines:
        # kline: [open_time_ms, open, high, low, close, volume,
        #         close_time_ms, quote_volume, trades, taker_buy_base, ...]
        try:
            ts_sec = int(k[0]) // 1000
            h = _floor_hour(ts_sec)
            open_ = float(k[1])
            high = float(k[2])
            low = float(k[3])
            close = float(k[4])
            quote_vol = float(k[7])  # already in USDT terms
        except (TypeError, ValueError, IndexError):
            continue

        pct_1h = ((close - last_close) / last_close * 100.0) if last_close else 0.0
        pct_24h = 0.0
        cutoff_24h = h - 24 * 3600
        for prev_h, prev_close in reversed(closes_24h):
            if prev_h <= cutoff_24h:
                pct_24h = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
                break

        rows.append({
            "ts": _iso(h),
            "ticker": ticker,
            "asset_class": asset_class,
            "max_leverage": 10,
            "open": open_,
            "high": high,
            "low": low,
            "price": close,
            "volume_24h_usd": quote_vol,  # per-bar quote volume; not rolling 24h
            "oi_usd": 0,
            "funding_1h": 0,
            "pct_24h": round(pct_24h, 4),
            "pct_1h": round(pct_1h, 4),
        })
        last_close = close
        closes_24h.append((h, close))

    log.info("binance %s (%s): %d hourly bars", ticker, sym, len(rows))
    return rows


def fetch_crypto_coinbase(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """Fetch hourly OHLCV from Coinbase Exchange public API.

    Public, no auth, real OHLC, US-accessible. The endpoint returns at most
    300 candles per call; we page in 300-hour windows.

    Product format: ``BTC-USD``. Memes that Coinbase prices in a ``-USD`` pair
    work directly; price-scaled symbols like ``1000PEPE`` are normalized to
    ``PEPE-USD`` (and the row scale will be different from Binance — fine for
    BOS detection since only relative moves matter).
    """
    s = _session()
    if s is None:
        return []
    raw_ticker = ticker.upper().lstrip("0")
    if raw_ticker.startswith("1000"):
        raw_ticker = raw_ticker[4:]
    product = f"{raw_ticker}-USD"
    asset_class = config.SYMBOL_TO_CLASS.get(ticker, "crypto_t2")

    end = int(end_ts) if end_ts is not None else int(time.time())
    start = end - days * 86400
    granularity = 3600
    window_secs = 300 * granularity  # 300 candles per request

    all_candles: list[list] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + window_secs, end)
        try:
            r = s.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={
                    "start": _iso(cursor),
                    "end": _iso(chunk_end),
                    "granularity": granularity,
                },
                timeout=15,
            )
        except Exception as e:
            log.warning("coinbase %s request failed: %s", ticker, e)
            break
        if r.status_code == 404:
            log.info("coinbase %s: product not listed (404)", product)
            return []
        if r.status_code != 200:
            log.warning("coinbase %s returned %d: %s", ticker, r.status_code, r.text[:160])
            break
        try:
            chunk = r.json()
        except ValueError:
            log.warning("coinbase %s returned non-JSON", ticker)
            break
        if not isinstance(chunk, list) or not chunk:
            cursor = chunk_end
            continue
        all_candles.extend(chunk)
        cursor = chunk_end

    # Coinbase candles are [time, low, high, open, close, volume], DESC order.
    all_candles.sort(key=lambda c: c[0])
    rows: list[dict] = []
    last_close: float | None = None
    closes_24h: list[tuple[int, float]] = []
    for c in all_candles:
        try:
            h = _floor_hour(int(c[0]))
            low = float(c[1])
            high = float(c[2])
            open_ = float(c[3])
            close = float(c[4])
            volume = float(c[5])  # base-asset volume; convert to USD via close
        except (TypeError, ValueError, IndexError):
            continue
        usd_vol = volume * close

        pct_1h = ((close - last_close) / last_close * 100.0) if last_close else 0.0
        pct_24h = 0.0
        cutoff_24h = h - 24 * 3600
        for prev_h, prev_close in reversed(closes_24h):
            if prev_h <= cutoff_24h:
                pct_24h = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
                break

        rows.append({
            "ts": _iso(h),
            "ticker": ticker,
            "asset_class": asset_class,
            "max_leverage": 10,
            "open": open_, "high": high, "low": low, "price": close,
            "volume_24h_usd": usd_vol,
            "oi_usd": 0, "funding_1h": 0,
            "pct_24h": round(pct_24h, 4), "pct_1h": round(pct_1h, 4),
        })
        last_close = close
        closes_24h.append((h, close))

    log.info("coinbase %s (%s): %d hourly bars", ticker, product, len(rows))
    return rows


def fetch_crypto_bybit(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """Fetch hourly OHLCV from Bybit v5 public kline API.

    No auth, free, US-accessible for public market data. Bybit USDT-perp
    symbols match Binance for most tickers (``BTCUSDT``, ``1000PEPEUSDT``).
    Max 1000 candles per request; we page in 1000-hour windows.
    """
    s = _session()
    if s is None:
        return []
    sym = BINANCE_SYMBOLS.get(ticker) or f"{ticker}USDT"  # same convention
    asset_class = config.SYMBOL_TO_CLASS.get(ticker, "crypto_t2")

    end_ms = (int(end_ts) if end_ts is not None else int(time.time())) * 1000
    start_ms = end_ms - days * 86400 * 1000

    all_klines: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + 1000 * 3600 * 1000, end_ms)
        try:
            r = s.get(
                "https://api.bybit.com/v5/market/kline",
                params={
                    "category": "linear",
                    "symbol": sym,
                    "interval": "60",
                    "start": cursor,
                    "end": chunk_end,
                    "limit": 1000,
                },
                timeout=15,
            )
        except Exception as e:
            log.warning("bybit %s request failed: %s", ticker, e)
            break
        if r.status_code != 200:
            log.warning("bybit %s returned %d: %s", ticker, r.status_code, r.text[:160])
            break
        try:
            data = r.json()
        except ValueError:
            log.warning("bybit %s returned non-JSON", ticker)
            break
        ret_code = data.get("retCode")
        if ret_code not in (0, "0"):
            log.info("bybit %s retCode=%s msg=%s", ticker, ret_code, data.get("retMsg"))
            return []
        chunk = (data.get("result") or {}).get("list") or []
        if not chunk:
            cursor = chunk_end
            continue
        all_klines.extend(chunk)
        cursor = chunk_end

    # Bybit kline: [startTime_ms, open, high, low, close, volume, turnover], DESC
    all_klines.sort(key=lambda k: int(k[0]))
    rows: list[dict] = []
    last_close: float | None = None
    closes_24h: list[tuple[int, float]] = []
    for k in all_klines:
        try:
            h = _floor_hour(int(k[0]) // 1000)
            open_ = float(k[1])
            high = float(k[2])
            low = float(k[3])
            close = float(k[4])
            turnover = float(k[6])  # quote (USDT) volume
        except (TypeError, ValueError, IndexError):
            continue

        pct_1h = ((close - last_close) / last_close * 100.0) if last_close else 0.0
        pct_24h = 0.0
        cutoff_24h = h - 24 * 3600
        for prev_h, prev_close in reversed(closes_24h):
            if prev_h <= cutoff_24h:
                pct_24h = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
                break

        rows.append({
            "ts": _iso(h),
            "ticker": ticker,
            "asset_class": asset_class,
            "max_leverage": 10,
            "open": open_, "high": high, "low": low, "price": close,
            "volume_24h_usd": turnover,
            "oi_usd": 0, "funding_1h": 0,
            "pct_24h": round(pct_24h, 4), "pct_1h": round(pct_1h, 4),
        })
        last_close = close
        closes_24h.append((h, close))

    log.info("bybit %s (%s): %d hourly bars", ticker, sym, len(rows))
    return rows


def fetch_crypto_hourly(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    coin_id = COINGECKO_IDS.get(ticker)
    if not coin_id:
        log.warning("no CoinGecko id mapping for %s — skipping", ticker)
        return []
    s = _session()
    if s is None:
        return []

    end = int(end_ts) if end_ts is not None else int(time.time())
    start = end - days * 86400
    try:
        r = s.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range",
            params={"vs_currency": "usd", "from": start, "to": end},
            timeout=15,
        )
    except Exception as e:
        log.warning("coingecko %s request failed: %s", ticker, e)
        return []
    if r.status_code != 200:
        log.warning("coingecko %s returned %d: %s", ticker, r.status_code, r.text[:200])
        return []

    try:
        data = r.json()
    except ValueError:
        return []

    prices = data.get("prices") or []         # [[ts_ms, price_usd], ...]
    volumes = data.get("total_volumes") or []  # [[ts_ms, vol_usd], ...]

    # Bin by floored hour. CoinGecko returns ~5-min granularity for short
    # ranges; collapse each hour to the most recent point and sum volume.
    by_hour: dict[int, dict] = {}
    for ts_ms, price in prices:
        h = _floor_hour(ts_ms / 1000.0)
        by_hour.setdefault(h, {"prices": [], "vols": []})
        by_hour[h]["prices"].append((ts_ms, price))
    for ts_ms, vol in volumes:
        h = _floor_hour(ts_ms / 1000.0)
        by_hour.setdefault(h, {"prices": [], "vols": []})
        by_hour[h]["vols"].append(float(vol))

    rows: list[dict] = []
    sorted_hours = sorted(by_hour.keys())
    last_close = None
    closes_24h: list[tuple[int, float]] = []

    for h in sorted_hours:
        bucket = by_hour[h]
        if not bucket["prices"]:
            continue
        # Sort the intra-hour snapshots by ts so open=first, close=last.
        bucket["prices"].sort()
        intra_prices = [float(p) for _, p in bucket["prices"]]
        close = intra_prices[-1]
        # CoinGecko returns 5-min ticks only for 1-day windows; for multi-day
        # ranges we get 1 tick per hour. When only one snapshot exists we
        # synthesize OHLC from the previous bar's close so the bar has a
        # real (non-zero) range equal to the 1h price move. Crude but lets
        # BOS detection work.
        if len(intra_prices) >= 2:
            open_ = intra_prices[0]
            high = max(intra_prices)
            low = min(intra_prices)
        else:
            open_ = float(last_close) if last_close else close
            high = max(open_, close)
            low = min(open_, close)
        # API gives a snapshot of rolling 24h volume at each tick, so use the
        # last point's value, not the sum, to avoid double-counting.
        vol_24h = float(bucket["vols"][-1]) if bucket["vols"] else 0.0

        # 1h pct from previous bucket close
        pct_1h = ((close - last_close) / last_close * 100.0) if last_close else 0.0
        # 24h pct from the closest bucket ~24h ago
        pct_24h = 0.0
        cutoff_24h = h - 24 * 3600
        for prev_h, prev_close in reversed(closes_24h):
            if prev_h <= cutoff_24h:
                pct_24h = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
                break

        rows.append({
            "ts": _iso(h),
            "ticker": ticker,
            "asset_class": config.SYMBOL_TO_CLASS.get(ticker, "crypto_t2"),
            "max_leverage": 10,
            "open": open_,
            "high": high,
            "low": low,
            "price": close,
            "volume_24h_usd": vol_24h,
            "oi_usd": 0,
            "funding_1h": 0,
            "pct_24h": round(pct_24h, 4),
            "pct_1h": round(pct_1h, 4),
        })
        last_close = close
        closes_24h.append((h, close))

    log.info("coingecko %s: %d hourly bars", ticker, len(rows))
    return rows


# ============ yfinance (equity / commodity) ============

def fetch_yfinance_hourly(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    # yfinance doesn't support arbitrary historical 1h windows (intraday is
    # capped at ~60 days from now); end_ts is accepted for signature parity
    # but ignored. yfinance always returns a window ending now.
    _ = end_ts
    try:
        import yfinance as yf  # type: ignore
    except Exception as e:
        log.error("yfinance unavailable: %s", e)
        return []

    sym = YFINANCE_SYMBOLS.get(ticker, ticker)
    period = f"{max(1, min(days, 60))}d"
    try:
        df = yf.download(sym, period=period, interval="1h", progress=False, auto_adjust=False)
    except Exception as e:
        log.warning("yfinance %s download failed: %s", ticker, e)
        return []
    if df is None or len(df) == 0:
        log.warning("yfinance %s returned no data", ticker)
        return []

    rows: list[dict] = []
    last_close = None
    closes_24h: list[tuple[int, float]] = []
    asset_class = config.SYMBOL_TO_CLASS.get(ticker, "equity")

    # Normalize multiindex columns (yfinance returns ('Close','TICKER') for some calls)
    cols = df.columns
    if hasattr(cols, "levels") and len(cols.levels) > 1:
        df = df.droplevel(1, axis=1)

    for ts, row in df.iterrows():
        try:
            close = float(row["Close"])
            open_ = float(row.get("Open", close) or close)
            high = float(row.get("High", close) or close)
            low = float(row.get("Low", close) or close)
            vol = float(row.get("Volume", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        ts_sec = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
        h = _floor_hour(ts_sec)

        pct_1h = ((close - last_close) / last_close * 100.0) if last_close else 0.0
        pct_24h = 0.0
        cutoff_24h = h - 24 * 3600
        for prev_h, prev_close in reversed(closes_24h):
            if prev_h <= cutoff_24h:
                pct_24h = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
                break

        # yfinance volume is shares; multiply by close for an approx USD figure.
        vol_usd = vol * close

        rows.append({
            "ts": _iso(h),
            "ticker": ticker,
            "asset_class": asset_class,
            "max_leverage": 5,
            "open": open_,
            "high": high,
            "low": low,
            "price": close,
            "volume_24h_usd": vol_usd,
            "oi_usd": 0,
            "funding_1h": 0,
            "pct_24h": round(pct_24h, 4),
            "pct_1h": round(pct_1h, 4),
        })
        last_close = close
        closes_24h.append((h, close))

    log.info("yfinance %s (%s): %d hourly bars", ticker, sym, len(rows))
    return rows


# ============ routing ============

def fetch_crypto(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """Crypto router with US-friendly fallback chain: Coinbase → Bybit → Binance → CoinGecko.

    Coinbase Exchange public API and Bybit v5 are both free, US-accessible, and
    return real OHLC (vs CoinGecko's synthesized hourly). Binance is kept as
    a third try for environments where it isn't geo-blocked. CoinGecko is the
    final fallback for long-tail tokens.
    """
    rows = fetch_crypto_coinbase(ticker, days, end_ts=end_ts)
    if rows:
        return rows
    rows = fetch_crypto_bybit(ticker, days, end_ts=end_ts)
    if rows:
        return rows
    rows = fetch_crypto_binance(ticker, days, end_ts=end_ts)
    if rows:
        return rows
    log.info("all real-OHLC sources empty for %s — falling back to CoinGecko (synthesized)", ticker)
    return fetch_crypto_hourly(ticker, days, end_ts=end_ts)


# ============ 15m fetchers (sub-hourly confirmation timeframe) ============
#
# Coinbase: granularity=900 (15m), 300 candles/call ≈ 75h per request.
# Bybit:    interval="15", 1000 candles/call ≈ 250h per request.
# Binance / CoinGecko not used at 15m (Binance is geo-blocked from the host;
# CoinGecko's free tier doesn't surface sub-hourly OHLC). For tickers without
# a Coinbase or Bybit listing the 15m path returns empty and the BOS engine
# silently falls back to the 1h-only confirmation gate.


def fetch_crypto_coinbase_15m(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """15m OHLC from Coinbase Exchange. Shape mirrors fetch_crypto_coinbase."""
    s = _session()
    if s is None:
        return []
    raw_ticker = ticker.upper().lstrip("0")
    if raw_ticker.startswith("1000"):
        raw_ticker = raw_ticker[4:]
    product = f"{raw_ticker}-USD"
    asset_class = config.SYMBOL_TO_CLASS.get(ticker, "crypto_t2")

    end = int(end_ts) if end_ts is not None else int(time.time())
    start = end - days * 86400
    granularity = 900
    window_secs = 300 * granularity  # 300 candles per request ≈ 75h

    all_candles: list[list] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + window_secs, end)
        try:
            r = s.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={
                    "start": _iso(cursor),
                    "end": _iso(chunk_end),
                    "granularity": granularity,
                },
                timeout=15,
            )
        except Exception as e:
            log.warning("coinbase 15m %s request failed: %s", ticker, e)
            break
        if r.status_code == 404:
            log.info("coinbase 15m %s: product not listed (404)", product)
            return []
        if r.status_code != 200:
            log.warning("coinbase 15m %s returned %d: %s", ticker, r.status_code, r.text[:160])
            break
        try:
            chunk = r.json()
        except ValueError:
            log.warning("coinbase 15m %s returned non-JSON", ticker)
            break
        if not isinstance(chunk, list) or not chunk:
            cursor = chunk_end
            continue
        all_candles.extend(chunk)
        cursor = chunk_end

    all_candles.sort(key=lambda c: c[0])
    rows: list[dict] = []
    for c in all_candles:
        try:
            ts = _floor_15m(int(c[0]))
            low = float(c[1]); high = float(c[2])
            open_ = float(c[3]); close = float(c[4])
            volume = float(c[5])
        except (TypeError, ValueError, IndexError):
            continue
        rows.append({
            "ts": _iso(ts), "ticker": ticker, "asset_class": asset_class,
            "max_leverage": 10,
            "open": open_, "high": high, "low": low, "price": close,
            "volume_24h_usd": volume * close,
            "oi_usd": 0, "funding_1h": 0,
            "pct_24h": 0.0, "pct_1h": 0.0,
        })
    log.info("coinbase 15m %s (%s): %d bars", ticker, product, len(rows))
    return rows


def fetch_crypto_bybit_15m(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """15m OHLC from Bybit v5. Shape mirrors fetch_crypto_bybit."""
    s = _session()
    if s is None:
        return []
    sym = BINANCE_SYMBOLS.get(ticker) or f"{ticker}USDT"
    asset_class = config.SYMBOL_TO_CLASS.get(ticker, "crypto_t2")

    end_ms = (int(end_ts) if end_ts is not None else int(time.time())) * 1000
    start_ms = end_ms - days * 86400 * 1000

    all_klines: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + 1000 * 900 * 1000, end_ms)  # 1000 candles × 15m
        try:
            r = s.get(
                "https://api.bybit.com/v5/market/kline",
                params={
                    "category": "linear", "symbol": sym, "interval": "15",
                    "start": cursor, "end": chunk_end, "limit": 1000,
                },
                timeout=15,
            )
        except Exception as e:
            log.warning("bybit 15m %s request failed: %s", ticker, e)
            break
        if r.status_code != 200:
            log.warning("bybit 15m %s returned %d: %s", ticker, r.status_code, r.text[:160])
            break
        try:
            data = r.json()
        except ValueError:
            log.warning("bybit 15m %s returned non-JSON", ticker)
            break
        if data.get("retCode") not in (0, "0"):
            log.info("bybit 15m %s retCode=%s", ticker, data.get("retCode"))
            return []
        chunk = (data.get("result") or {}).get("list") or []
        if not chunk:
            cursor = chunk_end
            continue
        all_klines.extend(chunk)
        cursor = chunk_end

    all_klines.sort(key=lambda k: int(k[0]))
    rows: list[dict] = []
    for k in all_klines:
        try:
            ts = _floor_15m(int(k[0]) // 1000)
            open_ = float(k[1]); high = float(k[2])
            low = float(k[3]); close = float(k[4])
            turnover = float(k[6])
        except (TypeError, ValueError, IndexError):
            continue
        rows.append({
            "ts": _iso(ts), "ticker": ticker, "asset_class": asset_class,
            "max_leverage": 10,
            "open": open_, "high": high, "low": low, "price": close,
            "volume_24h_usd": turnover,
            "oi_usd": 0, "funding_1h": 0,
            "pct_24h": 0.0, "pct_1h": 0.0,
        })
    log.info("bybit 15m %s (%s): %d bars", ticker, sym, len(rows))
    return rows


def fetch_crypto_15m(ticker: str, days: int, end_ts: int | None = None) -> list[dict]:
    """15m crypto router: Coinbase → Bybit. No Binance (geo-block) / CoinGecko
    (no free sub-hourly). Returns ``[]`` when neither venue lists the ticker;
    BOS then falls back to 1h-only confirmation."""
    rows = fetch_crypto_coinbase_15m(ticker, days, end_ts=end_ts)
    if rows:
        return rows
    rows = fetch_crypto_bybit_15m(ticker, days, end_ts=end_ts)
    if rows:
        return rows
    log.info("no 15m source available for %s — 1h-only confirmation", ticker)
    return []


ROUTES = {
    "crypto_t1": fetch_crypto,
    "crypto_t2": fetch_crypto,
    "crypto_meme": fetch_crypto,
    "equity": fetch_yfinance_hourly,
    "commodity": fetch_yfinance_hourly,
    "forex": fetch_yfinance_hourly,
}

# 15m routes — keyed by asset_class, same as ROUTES. Only crypto today (yfinance
# intraday minimum is 1m via period=, but we don't need 15m on equities since
# they're not the speed-bottleneck use case).
ROUTES_15M: dict[str, "callable"] = {
    "crypto_t1": fetch_crypto_15m,
    "crypto_t2": fetch_crypto_15m,
    "crypto_meme": fetch_crypto_15m,
}


def is_fetchable(ticker: str, asset_class: str) -> bool:
    """Whether we have a working backfill route for this (ticker, asset_class).

    Used by the startup backfill in main.py to bucket the Lighter universe
    into "fetch now" vs "let it cold-start the slow way" and log the latter
    once so we know which exotic symbols are uncovered.

    Crypto: always True. ``fetch_crypto`` has a Coinbase → Bybit → Binance →
    CoinGecko fallback chain plus a ``{TICKER}USDT`` symbol default, so any
    coin listed on one of those exchanges will backfill. Truly long-tail
    tokens just produce empty fetches that the per-ticker error handler logs
    and moves past — cost is bounded by ``BACKFILL_PER_TICKER_TIMEOUT_SEC``.

    Equity: True. yfinance accepts raw symbols.
    Commodity / Forex: must have a YFINANCE_SYMBOLS override.
    Anything in TICKER_ROUTE_OVERRIDES is fetchable via the overridden route.
    """
    t = ticker.upper().strip()
    if t in TICKER_ROUTE_OVERRIDES:
        return True
    cls = asset_class.lower().strip()
    if cls.startswith("crypto"):
        return True
    if cls == "equity":
        return True
    if cls in ("commodity", "forex"):
        return t in YFINANCE_SYMBOLS
    return False


def fetch_universe(
    tickers: Iterable[str] | None = None,
    days: int = 7,
    sleep_between: float = 0.6,
    end_ts: int | None = None,
) -> list[dict]:
    """Fetch bars for a list of tickers (or the full configured universe).

    `end_ts` is the unix-second end of the window; defaults to now.
    """
    if tickers is None:
        targets = [t for cls in config.ASSET_CLASSES.values() for t in cls]
    else:
        targets = [t.strip().upper() for t in tickers if t.strip()]

    from . import lighter  # local import to avoid cycles
    rows: list[dict] = []
    for ticker in targets:
        cls = config.SYMBOL_TO_CLASS.get(ticker) or lighter.classify(ticker)
        fetcher = ROUTES.get(cls)
        if fetcher is None:
            log.warning("no fetcher for asset_class=%s (%s) — skipping", cls, ticker)
            continue
        try:
            fetched = fetcher(ticker, days, end_ts=end_ts)
            for r in fetched:
                r["asset_class"] = cls
            rows.extend(fetched)
        except Exception as e:
            log.warning("%s fetch blew up: %s", ticker, e)
        time.sleep(sleep_between)  # be polite to free APIs
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    rows = sorted(rows, key=lambda r: (r["ts"], r["ticker"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    log.info("wrote %d rows to %s", len(rows), path)


# ============ CLI ============

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m radar.fetch_bars")
    p.add_argument("--tickers", default=None,
                   help="comma-separated list (e.g. BTC,ETH,ARB). "
                        "Default = full ASSET_CLASSES universe.")
    p.add_argument("--days", type=int, default=7,
                   help="lookback window in days (default 7; CoinGecko caps "
                        "hourly at ~90, yfinance intraday at ~30-60).")
    p.add_argument("--end", default=None,
                   help="end of window as YYYY-MM-DD or ISO timestamp "
                        "(default: now). CoinGecko supports arbitrary historical "
                        "ranges; yfinance ignores this and always uses now.")
    p.add_argument("--out", default="data/bars.csv",
                   help="output CSV path (default data/bars.csv)")
    p.add_argument("--sleep", type=float, default=0.6,
                   help="seconds to sleep between API calls (default 0.6)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_argparser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    tickers = args.tickers.split(",") if args.tickers else None
    end_ts: int | None = None
    if args.end:
        from datetime import datetime, timezone
        s = args.end.strip()
        try:
            if "T" in s or " " in s:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_ts = int(dt.timestamp())
        except ValueError:
            log.error("could not parse --end %r", args.end)
            return 1
    rows = fetch_universe(tickers, days=args.days, sleep_between=args.sleep, end_ts=end_ts)
    if not rows:
        log.error("no rows fetched — aborting")
        return 1
    write_csv(rows, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
