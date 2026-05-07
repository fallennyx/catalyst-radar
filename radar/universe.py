"""Leveraged-universe loader. Pulls the live Lighter DEX market list (single
source of truth) and returns a list of `Market` records. Anything not active
on Lighter is invisible to the engine.

Cached for 60 seconds so the main loop can call this freely.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import config, lighter

log = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "markets": []}
_CACHE_TTL_SEC = 60


@dataclass
class Market:
    ticker: str
    asset_class: str
    market_id: str | None = None
    max_leverage: float = 1.0
    price: float = 0.0
    volume_24h_usd: float = 0.0
    oi_usd: float = 0.0
    funding_1h: float = 0.0
    pct_24h: float = 0.0
    # optional: short-window pct change used by the ranker's cold-start fallback
    pct_1h: float = 0.0
    # raw fields from upstream, kept for debugging
    raw: dict[str, Any] = field(default_factory=dict)


_TICKER_RE = re.compile(r"^[A-Z]{2,10}$")


def _normalize_ticker(raw: str | None) -> str | None:
    """Strip exchange suffixes (-USD, -PERP, /USDT, etc.) and uppercase."""
    if not raw:
        return None
    s = str(raw).upper().strip()
    for sep in ("/", "-", ":", "_"):
        if sep in s:
            s = s.split(sep, 1)[0]
    s = re.sub(r"PERP$|USD$|USDT$|USDC$", "", s).strip("-_/:")
    if not _TICKER_RE.match(s):
        return None
    return s


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _market_from_raw(raw: dict[str, Any]) -> Market | None:
    """Best-effort mapping from a Lighter market dict to our Market type.

    Lighter's payload schema is duck-typed here: we look for a handful of
    common field names so we tolerate library version drift.
    """
    ticker = (
        _normalize_ticker(raw.get("symbol"))
        or _normalize_ticker(raw.get("ticker"))
        or _normalize_ticker(raw.get("base"))
        or _normalize_ticker(raw.get("name"))
    )
    if not ticker:
        return None

    asset_class = config.SYMBOL_TO_CLASS.get(ticker)
    if asset_class is None:
        return None

    max_leverage = _coerce_float(
        raw.get("max_leverage")
        or raw.get("maxLeverage")
        or raw.get("leverage_max"),
        default=1.0,
    )
    if max_leverage <= 1:
        return None

    return Market(
        ticker=ticker,
        asset_class=asset_class,
        market_id=str(raw.get("market_id") or raw.get("id") or raw.get("marketIndex") or ticker),
        max_leverage=max_leverage,
        price=_coerce_float(raw.get("price") or raw.get("mark_price") or raw.get("last")),
        volume_24h_usd=_coerce_float(
            raw.get("volume_24h_usd")
            or raw.get("quote_volume_24h")
            or raw.get("volume_24h")
        ),
        oi_usd=_coerce_float(raw.get("open_interest_usd") or raw.get("oi") or raw.get("open_interest")),
        funding_1h=_coerce_float(raw.get("funding_rate_1h") or raw.get("funding") or raw.get("funding_rate")),
        pct_24h=_coerce_float(raw.get("price_change_24h_pct") or raw.get("pct_24h") or raw.get("change_24h")),
        pct_1h=_coerce_float(raw.get("price_change_1h_pct") or raw.get("pct_1h") or raw.get("change_1h")),
        raw=raw,
    )


def _fetch_lighter_markets() -> list[dict[str, Any]]:
    """Call Lighter SDK. Wrapped so failures don't crash the loop."""
    try:
        import lighter  # type: ignore
    except Exception as e:  # pragma: no cover — depends on env
        log.warning("Lighter SDK not importable: %s", e)
        return []

    try:
        # The SDK surface differs by version; try a few likely entry points.
        if hasattr(lighter, "Client"):
            client = lighter.Client()
            if hasattr(client, "markets"):
                return list(client.markets())  # type: ignore[no-any-return]
            if hasattr(client, "get_markets"):
                return list(client.get_markets())  # type: ignore[no-any-return]
        if hasattr(lighter, "get_markets"):
            return list(lighter.get_markets())  # type: ignore[no-any-return]
        if hasattr(lighter, "markets"):
            return list(lighter.markets())  # type: ignore[no-any-return]
    except Exception as e:
        log.warning("Lighter fetch failed: %s", e)
        return []

    log.warning("Lighter SDK present but no recognized market accessor")
    return []


def get_leveraged_universe(force: bool = False) -> list[Market]:
    """Return the current leveraged universe — every active perp on Lighter.

    Each market's asset_class is auto-classified by `lighter.classify()`.
    Price/volume/OI/funding are filled where the Lighter API exposes them
    (most are 0 from the orderBooks endpoint; the discovery scan refreshes
    them via `get_market_snapshot`).

    Falls back to the cached value (possibly empty) on API failure — the
    main loop logs and sleeps rather than crashing.
    """
    now = time.time()
    if not force and (now - _CACHE["ts"]) < _CACHE_TTL_SEC and _CACHE["markets"]:
        return list(_CACHE["markets"])

    lighter_markets = lighter.fetch_universe(force=force)
    markets: list[Market] = []
    for lm in lighter_markets:
        markets.append(Market(
            ticker=lm.symbol,
            asset_class=lm.asset_class,
            market_id=str(lm.market_id),
            max_leverage=10.0,  # Lighter perps are always leveraged; orderBooks doesn't expose max
            price=0.0,
            volume_24h_usd=0.0,
            oi_usd=0.0,
            funding_1h=0.0,
            pct_24h=0.0,
            pct_1h=0.0,
            raw=lm.raw or {},
        ))

    _CACHE["ts"] = now
    _CACHE["markets"] = markets
    log.info("universe: %d Lighter perps", len(markets))
    return list(markets)


def get_market_snapshot(ticker: str) -> Market | None:
    """Fetch live state for a single ticker. Used by Tier 2 polling.

    Lighter's SDK doesn't expose a single-ticker accessor consistently across
    versions, so we fall back to filtering the (60s-cached) full universe.
    Cheap on the hot path because the cache hit is the common case."""
    if not ticker:
        return None
    universe = get_leveraged_universe()
    needle = ticker.upper().strip()
    for m in universe:
        if m.ticker == needle:
            return m
    return None
