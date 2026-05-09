"""Live Lighter DEX universe — the canonical source of truth for which
tickers the engine should process.

The legacy `config.ASSET_CLASSES` was a hand-curated list of ~44 tickers we
chose to track. Lighter actually lists 160+ active perps including equities,
ETFs, forex, and commodities — anything they've spun up a perp market for.
This module fetches that live list and classifies each ticker into an asset
class the rest of the engine understands.

Cached for 60 seconds so the orchestrator can call this freely.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

LIGHTER_API = "https://mainnet.zklighter.elliot.ai/api/v1"
_CACHE: dict[str, Any] = {"ts": 0.0, "markets": []}
_CACHE_TTL_SEC = 60


@dataclass
class LighterMarket:
    symbol: str
    market_id: int
    asset_class: str
    status: str = "active"
    raw: dict[str, Any] | None = None


# ============ classification ============

_TIER1_CRYPTO = {
    # majors by mkt cap / liquidity
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE",
    "ADA", "AVAX", "DOT", "LINK", "TRX", "LTC", "BCH", "XMR", "XLM", "TON", "HBAR",
}

_MEMES = {
    # meta-tag set; 1000-prefixed symbols are also routed here automatically
    "WIF", "PEPE", "BONK", "POPCAT", "FLOKI", "SHIB", "TRUMP", "PENGU", "PUMP",
    "MOG", "TOSHI", "BIRB", "FARTCOIN", "USELESS", "DOLO", "CHIP", "AERO",
}

_KNOWN_EQUITIES = {
    # US equities
    "AAPL", "AMZN", "MSFT", "NVDA", "GOOGL", "META", "TSLA", "INTC", "AMD",
    "ASML", "MSTR", "COIN", "MARA", "CRCL", "BMNR", "PLTR", "ORCL", "HOOD",
    "TSM", "MU", "SNDK", "STRC", "STRK", "CRWV",
}

_KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "EWY", "URA", "BOTZ", "SOXX", "ROBO",
    "SPX", "MET", "MAGS",
}

# Lighter's pre-IPO / foreign-listing perps suffix `USD`
_USD_SUFFIX_EQUITIES = {"HYUNDAIUSD", "SAMSUNGUSD", "SKHYNIXUSD"}

_FOREX_PAIRS = {
    "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD",
    "USDCAD", "USDCHF", "USDJPY", "USDKRW",
}

_COMMODITIES = {
    "XAU", "XAG", "XPD", "XPT", "XCU",
    "BRENTOIL", "WTI", "NATGAS", "PAXG",
}


def classify(symbol: str) -> str:
    """Map a Lighter symbol to one of the engine's asset classes.

    Returns one of: ``crypto_t1``, ``crypto_t2``, ``crypto_meme``,
    ``equity``, ``commodity``, ``forex``.
    """
    s = symbol.upper().strip()
    if s in _USD_SUFFIX_EQUITIES:
        return "equity"
    if s in _FOREX_PAIRS:
        return "forex"
    if s in _COMMODITIES:
        return "commodity"
    if s in _KNOWN_EQUITIES or s in _KNOWN_ETFS:
        return "equity"
    if s in _TIER1_CRYPTO:
        return "crypto_t1"
    if s.startswith("1000") or s in _MEMES:
        return "crypto_meme"
    # Default: this is a crypto-first DEX, so anything unclassified is treated
    # as a crypto_t2 (mid-cap) perp.
    return "crypto_t2"


# ============ API ============

def _fetch_order_books(timeout: int = 15) -> list[dict] | None:
    """Hit the Lighter mainnet `orderBooks` endpoint. Returns the raw list
    or None on failure."""
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.warning("requests unavailable: %s", e)
        return None
    try:
        r = requests.get(
            f"{LIGHTER_API}/orderBooks",
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "catalyst-radar/0.1"},
        )
    except Exception as e:
        log.warning("Lighter API request failed: %s", e)
        return None
    if r.status_code != 200:
        log.warning("Lighter API returned %d", r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        log.warning("Lighter API returned non-JSON")
        return None
    return data.get("order_books") or []


def fetch_universe(force: bool = False) -> list[LighterMarket]:
    """Return the live list of active perp markets on Lighter.

    Cached for 60 seconds. On API failure returns the last-good cached value
    (or empty list if we never had one)."""
    now = time.time()
    if not force and (now - _CACHE["ts"]) < _CACHE_TTL_SEC and _CACHE["markets"]:
        return list(_CACHE["markets"])

    books = _fetch_order_books()
    if books is None:
        # Network failure — return whatever we cached (possibly empty)
        return list(_CACHE["markets"])

    markets: list[LighterMarket] = []
    for b in books:
        if not isinstance(b, dict):
            continue
        if b.get("market_type") != "perp":
            continue
        if b.get("status") != "active":
            continue
        symbol = (b.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        try:
            mid = int(b.get("market_id"))
        except (TypeError, ValueError):
            continue
        markets.append(LighterMarket(
            symbol=symbol,
            market_id=mid,
            asset_class=classify(symbol),
            status=b.get("status", "active"),
            raw=b,
        ))

    _CACHE["ts"] = now
    _CACHE["markets"] = markets
    log.info("Lighter universe: %d active perps", len(markets))
    return list(markets)


def live_symbol_to_class() -> dict[str, str]:
    """{ticker: asset_class} for every active Lighter perp."""
    return {m.symbol: m.asset_class for m in fetch_universe()}


# ============ live market stats ============

_STATS_CACHE: dict[str, Any] = {"ts": 0.0, "by_symbol": {}}


def fetch_market_stats(timeout: int = 15) -> dict[str, dict[str, float]]:
    """Hit Lighter's `/exchangeStats` endpoint for live mark prices, 24h volume,
    and 24h price change per symbol.

    Cached 30 seconds (faster than the universe metadata cache because prices
    move). Returns a dict keyed by symbol with float fields:

        {
            "BTC": {
                "last_trade_price": 81234.5,
                "daily_quote_token_volume": 12_345_678.9,
                "daily_price_change": 2.34,    # percent, 24h
            },
            ...
        }

    On API failure returns the last-good cache (possibly empty).
    """
    now = time.time()
    if (now - _STATS_CACHE["ts"]) < 30 and _STATS_CACHE["by_symbol"]:
        return dict(_STATS_CACHE["by_symbol"])
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.warning("requests unavailable: %s", e)
        return dict(_STATS_CACHE["by_symbol"])
    try:
        r = requests.get(
            f"{LIGHTER_API}/exchangeStats",
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "catalyst-radar/0.1"},
        )
    except Exception as e:
        log.warning("Lighter exchangeStats request failed: %s", e)
        return dict(_STATS_CACHE["by_symbol"])
    if r.status_code != 200:
        log.warning("Lighter exchangeStats returned %d", r.status_code)
        return dict(_STATS_CACHE["by_symbol"])
    try:
        data = r.json()
    except ValueError:
        return dict(_STATS_CACHE["by_symbol"])
    rows = data.get("order_book_stats") or []
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        sym = (row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        try:
            out[sym] = {
                "last_trade_price": float(row.get("last_trade_price") or 0.0),
                "daily_quote_token_volume": float(row.get("daily_quote_token_volume") or 0.0),
                "daily_price_change": float(row.get("daily_price_change") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    _STATS_CACHE["ts"] = now
    _STATS_CACHE["by_symbol"] = out
    return dict(out)


def is_listed(ticker: str) -> bool:
    """True if `ticker` is currently listed on Lighter as an active perp."""
    if not ticker:
        return False
    needle = ticker.upper().strip()
    return any(m.symbol == needle for m in fetch_universe())
