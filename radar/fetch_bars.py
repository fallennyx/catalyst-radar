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
    # USELESS, FARTCOIN intentionally skipped — no stable mapping
}

# yfinance overrides for tickers that don't trade as plain US equities.
YFINANCE_SYMBOLS: dict[str, str] = {
    "HYUNDAI": "HYMTF",      # OTC ADR
    "XAU": "GC=F",           # gold front-month future
    "XAG": "SI=F",           # silver front-month future
    "BRENTOIL": "BZ=F",      # Brent crude front-month
    "WTI": "CL=F",           # WTI front-month
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
    "price", "volume_24h_usd", "oi_usd", "funding_1h",
    "pct_24h", "pct_1h",
]


def _floor_hour(ts_sec: float) -> int:
    return int(ts_sec) - (int(ts_sec) % 3600)


def _iso(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============ CoinGecko (crypto) ============

def fetch_crypto_hourly(ticker: str, days: int) -> list[dict]:
    coin_id = COINGECKO_IDS.get(ticker)
    if not coin_id:
        log.warning("no CoinGecko id mapping for %s — skipping", ticker)
        return []
    s = _session()
    if s is None:
        return []

    end = int(time.time())
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
        bucket["prices"].sort()
        close = float(bucket["prices"][-1][1])
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

def fetch_yfinance_hourly(ticker: str, days: int) -> list[dict]:
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

ROUTES = {
    "crypto_t1": fetch_crypto_hourly,
    "crypto_t2": fetch_crypto_hourly,
    "crypto_meme": fetch_crypto_hourly,
    "equity": fetch_yfinance_hourly,
    "commodity": fetch_yfinance_hourly,
}


def fetch_universe(
    tickers: Iterable[str] | None = None,
    days: int = 7,
    sleep_between: float = 0.6,
) -> list[dict]:
    """Fetch bars for a list of tickers (or the full configured universe)."""
    if tickers is None:
        targets = [t for cls in config.ASSET_CLASSES.values() for t in cls]
    else:
        targets = [t.strip().upper() for t in tickers if t.strip()]

    rows: list[dict] = []
    for ticker in targets:
        cls = config.SYMBOL_TO_CLASS.get(ticker)
        if not cls:
            log.warning("ticker %s not in ASSET_CLASSES — skipping", ticker)
            continue
        fetcher = ROUTES.get(cls)
        if fetcher is None:
            log.warning("no fetcher for asset_class=%s (%s)", cls, ticker)
            continue
        try:
            rows.extend(fetcher(ticker, days))
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
    rows = fetch_universe(tickers, days=args.days, sleep_between=args.sleep)
    if not rows:
        log.error("no rows fetched — aborting")
        return 1
    write_csv(rows, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
