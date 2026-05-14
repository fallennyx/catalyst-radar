"""Direction adjudicator — single entry point for "what direction should we tell the user?"

The structural BOS cross is the TRIGGER (every BOS-confirmed event still produces
a Telegram message — v3 no-suppression invariant). What this module decides is
the DIRECTION the user sees in that message and the direction the trade plan is
built against.

Both Tier 1 (`run_discovery_cycle`) and Tier 2 (`run_trigger_poll`) call
``decide(...)``. Internally it:

  1. Assembles a structured signal bundle from live data (order book, OI, funding,
     volume, HTF trend, BTC macro, current bar wick shape).
  2. Hands the bundle plus news + classifier + bar history to the predictor
     (`radar.predictor.analyze`), which uses Gemini 2.5 Flash to return a
     final direction + conviction.
  3. Maps the LLM's direction_confidence × setup_quality into a conviction tier
     (STRONG / OK / TENTATIVE / NO_TRADE) for Telegram rendering.

Fail-safe semantics: if the LLM call fails for any reason, fall back to the
structural direction with conviction_tier="OK". The alert always sends.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from . import config, lighter, predictor, ranker
from .catalysts import NewsItem
from .predictor import PredictorResult

log = logging.getLogger(__name__)


ConvictionTier = Literal["STRONG", "OK", "TENTATIVE", "NO_TRADE"]


@dataclass
class AdjudicatedDirection:
    """The final word on a BOS-triggered alert's direction.

    ``direction``        — "long" | "short" | "no_trade" — drives the trade plan.
    ``conviction_tier``  — STRONG | OK | TENTATIVE | NO_TRADE — drives the Telegram render.
    ``flipped``          — True if direction differs from the structural direction.
    ``predictor_result`` — The full PredictorResult (thesis, kill, risks, etc.) — None on fallback.
    ``signal_bundle``    — The structured signals the LLM saw — used in logs + Telegram tooltips.
    ``fallback``         — True if we fell back to structural direction (LLM unreachable).
    """
    direction: str
    conviction_tier: ConvictionTier
    flipped: bool
    predictor_result: PredictorResult | None
    signal_bundle: dict
    fallback: bool = False


# ============================================================================
# signal bundle assembly
# ============================================================================

def _safe_pct(num: float | None, denom: float | None) -> float | None:
    if num is None or denom is None or denom == 0:
        return None
    try:
        return float(num) / float(denom) * 100.0
    except Exception:
        return None


def _volume_z(history: list, current_volume: float | None) -> tuple[float | None, float | None]:
    """Returns (volume_ratio, volume_z) for the current bar against the 30d rolling window.

    volume_ratio = current_volume / median_volume_30d (None if no history)
    volume_z     = (current - mean) / std (None if std == 0)
    """
    if not history or current_volume is None:
        return (None, None)
    vols = [float(b.volume or 0.0) for b in history[:-1] if (b.volume or 0.0) > 0]
    if not vols:
        return (None, None)
    median_v = ranker.compute_median_volume(history[:-1])
    ratio = (float(current_volume) / median_v) if median_v > 0 else None
    mean = sum(vols) / len(vols)
    var = sum((v - mean) ** 2 for v in vols) / len(vols)
    std = math.sqrt(var) if var > 0 else 0.0
    z = ((float(current_volume) - mean) / std) if std > 0 else None
    return (ratio, z)


def _oi_delta_pct(history: list) -> float | None:
    """1h OI delta % — current bar's OI vs the prior bar's OI."""
    if not history or len(history) < 2:
        return None
    try:
        cur = float(history[-1].oi or 0.0)
        prev = float(history[-2].oi or 0.0)
        if prev <= 0:
            return None
        return (cur - prev) / prev * 100.0
    except Exception:
        return None


def _range_ratio(bars: list, lookback: int) -> float | None:
    """current_bar_range / median_range over the prior `lookback` bars."""
    if not bars or len(bars) < 2:
        return None
    cur = bars[-1]
    cur_range = float((cur.high or 0.0) - (cur.low or 0.0))
    lookback_window = bars[-(lookback + 1):-1]
    median = ranker.compute_median_range(lookback_window)
    if median <= 0:
        return None
    return cur_range / median


def _btc_24h_pct(btc_history: list) -> float | None:
    if not btc_history or len(btc_history) < 25:
        return None
    try:
        last = float(btc_history[-1].close or 0.0)
        prior = float(btc_history[-25].close or 0.0)
        if prior <= 0:
            return None
        return (last - prior) / prior * 100.0
    except Exception:
        return None


def _fetch_book_safe(market: Any) -> tuple[float | None, float | None, float | None, str]:
    """Live order-book depth — returns (bid_usd, ask_usd, ratio, sentiment_label).
    Best-effort; returns all-None + 'unknown' on any failure."""
    try:
        mid = lighter.market_id_for(market.ticker)
        if mid is None:
            return (None, None, None, "unknown")
        depth = lighter.fetch_order_book_depth(mid, levels=10)
        if depth is None:
            return (None, None, None, "unknown")
        bid_usd, ask_usd = depth
        ratio, sentiment = lighter.imbalance_sentiment(bid_usd, ask_usd)
        return (bid_usd, ask_usd, ratio, sentiment)
    except Exception as e:
        log.debug("adjudicator: book fetch failed for %s: %s", market.ticker, e)
        return (None, None, None, "unknown")


def _build_signal_bundle(
    *,
    market: Any,
    history: list,
    history_15m: list | None,
    btc_history: list,
    suppression_metadata: dict,
    tier: int,
    watchlist_age_hours: float | None,
) -> dict:
    """Assemble the structured signal block the LLM consumes. Pure compute over
    in-memory data + one live order-book fetch."""

    structure_dir = suppression_metadata.get("structure_direction")
    breakout_level = suppression_metadata.get("breakout_level")
    median_range_1h = suppression_metadata.get("median_bar_range")

    # --- price/structure metrics ---
    distance_pct = None
    if breakout_level and market.price:
        try:
            distance_pct = (float(market.price) - float(breakout_level)) / float(breakout_level) * 100.0
        except Exception:
            distance_pct = None

    range_ratio_1h = _range_ratio(history, config.SWING_LOOKBACK_HOURS)
    range_ratio_15m = _range_ratio(history_15m or [], config.SWING_LOOKBACK_15M_BARS) if history_15m else None

    cur_1h = history[-1] if history else None
    cur_15m = history_15m[-1] if history_15m else None
    wick_1h = predictor._wick_analysis(cur_1h, breakout_level, structure_dir or "long")
    wick_15m = predictor._wick_analysis(cur_15m, breakout_level, structure_dir or "long") if cur_15m else "(no 15m bar)"

    # --- positioning ---
    cur_volume = float(cur_1h.volume or 0.0) if cur_1h else None
    volume_ratio, volume_z = _volume_z(history, cur_volume)
    oi_delta = _oi_delta_pct(history)
    funding = None
    if cur_1h is not None and cur_1h.funding is not None:
        try:
            funding = float(cur_1h.funding) * 100.0   # store rate → percent
        except Exception:
            funding = None
    oi_usd = float(cur_1h.oi or 0.0) if cur_1h else None

    # --- book (live) ---
    bid_usd, ask_usd, book_ratio, book_sentiment = _fetch_book_safe(market)

    # --- HTF / macro ---
    htf_aligned = None
    if structure_dir in ("long", "short"):
        try:
            htf_aligned = ranker.htf_trend_aligned(history, structure_dir, config.HTF_TREND_LOOKBACK_HOURS)
        except Exception:
            htf_aligned = None
    btc_24h = _btc_24h_pct(btc_history)

    # --- timing ---
    utc_hour = datetime.now(tz=timezone.utc).hour

    def _fmt_float(v: float | None, places: int = 4) -> str | None:
        if v is None:
            return None
        try:
            return f"{float(v):.{places}f}"
        except Exception:
            return None

    return {
        # structural prior
        "structure_direction": structure_dir,
        "structure_type": suppression_metadata.get("structure_type"),
        "breakout_level": _fmt_float(breakout_level, 6),
        "current_price": _fmt_float(market.price, 6),
        "distance_past_pivot_pct": _fmt_float(distance_pct, 3),
        "range_ratio_1h": _fmt_float(range_ratio_1h, 2),
        "range_ratio_15m": _fmt_float(range_ratio_15m, 2),
        # wick / sweep
        "wick_1h": wick_1h,
        "wick_15m": wick_15m,
        # book
        "book_bid_usd": _fmt_float(bid_usd, 0),
        "book_ask_usd": _fmt_float(ask_usd, 0),
        "book_ratio": _fmt_float(book_ratio, 2),
        "book_sentiment": book_sentiment,
        # positioning
        "oi_usd": _fmt_float(oi_usd, 0),
        "oi_delta_pct": _fmt_float(oi_delta, 2),
        "funding_pct": _fmt_float(funding, 4),
        "volume_ratio": _fmt_float(volume_ratio, 2),
        "volume_z": _fmt_float(volume_z, 2),
        # HTF / macro
        "htf_aligned": htf_aligned,
        "btc_24h_pct": _fmt_float(btc_24h, 2),
        # timing
        "tier": tier,
        "watchlist_age_hours": _fmt_float(watchlist_age_hours, 1) if watchlist_age_hours is not None else None,
        "utc_hour": utc_hour,
    }


# ============================================================================
# conviction tiering
# ============================================================================

def _conviction_tier(pred: PredictorResult) -> ConvictionTier:
    """Map (direction_confidence, setup_quality, final_direction) → render tier."""
    if pred.final_direction == "no_trade":
        return "NO_TRADE"
    conf = float(pred.direction_confidence or 0.0)
    quality = float(pred.setup_quality or 0.0)
    # Geometric mean rewards both being high; one near 0 collapses to NO_TRADE.
    score = math.sqrt(max(0.0, conf) * max(0.0, quality)) if (conf > 0 and quality > 0) else 0.0
    if score >= config.DIR_CONVICTION_STRONG:
        return "STRONG"
    if score >= config.DIR_CONVICTION_OK:
        return "OK"
    if score >= config.DIR_CONVICTION_TENTATIVE:
        return "TENTATIVE"
    return "NO_TRADE"


# ============================================================================
# public entry point
# ============================================================================

def decide(
    *,
    market: Any,
    history: list,
    history_15m: list | None,
    btc_history: list,
    suppression_metadata: dict,
    classifier_result: Any,
    news_items: Iterable[NewsItem],
    prior_alerts: list[dict] | None,
    tier: int,
    watchlist_age_hours: float | None = None,
) -> AdjudicatedDirection:
    """Decide the alert direction for a BOS-triggered event.

    See module docstring for behavior. This function never raises — on any
    error path it returns a structural-direction fallback so the alert still
    sends with the legacy (deterministic) direction.
    """
    structural_dir = suppression_metadata.get("structure_direction") or "long"
    bundle = _build_signal_bundle(
        market=market,
        history=history,
        history_15m=history_15m,
        btc_history=btc_history,
        suppression_metadata=suppression_metadata,
        tier=tier,
        watchlist_age_hours=watchlist_age_hours,
    )

    # Adjudicator disabled → fall back to structural direction, OK tier.
    if not config.DIRECTION_ADJUDICATOR_ENABLED:
        return AdjudicatedDirection(
            direction=structural_dir,
            conviction_tier="OK",
            flipped=False,
            predictor_result=None,
            signal_bundle=bundle,
            fallback=True,
        )

    t0 = time.time()
    try:
        pred = predictor.analyze(
            market=market,
            classifier_result=classifier_result,
            signal_bundle=bundle,
            bar_history=history,
            bars_15m=history_15m,
            news_items=list(news_items or []),
            prior_alerts=prior_alerts or [],
        )
    except Exception as e:
        log.warning("adjudicator: predictor crashed for %s: %s", market.ticker, e)
        pred = None
    dt = time.time() - t0

    if pred is None:
        log.info("adjudicator %s: fallback to structural %s (%.2fs)",
                 market.ticker, structural_dir, dt)
        return AdjudicatedDirection(
            direction=structural_dir,
            conviction_tier="OK",
            flipped=False,
            predictor_result=None,
            signal_bundle=bundle,
            fallback=True,
        )

    tier_label = _conviction_tier(pred)
    flipped = (
        pred.final_direction in ("long", "short")
        and structural_dir in ("long", "short")
        and pred.final_direction != structural_dir
    )
    log.info(
        "adjudicator %s: structure=%s → LLM=%s conf=%.2f quality=%.2f tier=%s flipped=%s (%.2fs)",
        market.ticker, structural_dir, pred.final_direction,
        pred.direction_confidence, pred.setup_quality, tier_label, flipped, dt,
    )
    return AdjudicatedDirection(
        direction=pred.final_direction,
        conviction_tier=tier_label,
        flipped=flipped,
        predictor_result=pred,
        signal_bundle=bundle,
        fallback=False,
    )
