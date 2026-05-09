"""Deterministic SL/TP attached to every BOS alert.

Direction comes from the suppression chain (already validated against the
classifier). Stop sits a small buffer beyond the broken swing level so a
re-test doesn't immediately stop you out. TP1 is 1.5R. TP2 is the next
prior swing OR 3R from entry, whichever is closer.

No second LLM call. No position sizing. The engine never trades — these
levels are advisory only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import config, ranker

log = logging.getLogger(__name__)


@dataclass
class TradePlan:
    direction: str           # "long" or "short"
    entry: float             # current close at alert time
    stop: float              # broken swing level + small buffer (initial stop)
    tp1: float               # 1.5R from entry
    tp2: float               # next prior swing OR 3R, whichever is closer
    risk_per_unit: float     # |entry - stop|
    r_multiple_tp1: float    # favorable-direction reward, signed positive when target is profitable
    r_multiple_tp2: float

    # ---- Multi-stage exit ladder (improvement #3) ----
    # All optional with defaults so old call sites keep working.
    tp1_fraction: float = 0.0          # close this fraction at TP1 (e.g., 0.33)
    tp2_fraction: float = 0.0          # close this fraction at TP2
    breakeven_trigger: float | None = None   # move stop to entry when price reaches this
    trail_atr: float | None = None     # 14h ATR — base unit for the trail
    trail_atr_mult: float = 0.0        # trailing distance = trail_atr * trail_atr_mult
    runner_fraction: float = 0.0       # remaining fraction trailed after TP2

    @property
    def has_runner(self) -> bool:
        """True when the plan has an ATR-trailed final tranche."""
        return (
            self.trail_atr is not None
            and self.trail_atr > 0
            and self.runner_fraction > 0
        )


def _find_next_swing_above(history: list, threshold: float) -> float | None:
    """Lowest historical bar high strictly above ``threshold``.

    Excludes the in-progress (last) bar so the breakout candle's own wick
    can't be picked as the next target.
    """
    candidates: list[float] = []
    for b in (history or [])[:-1]:
        h = getattr(b, "high", None)
        if h is None:
            continue
        if float(h) > float(threshold):
            candidates.append(float(h))
    return min(candidates) if candidates else None


def _find_next_swing_below(history: list, threshold: float) -> float | None:
    """Highest historical bar low strictly below ``threshold``.

    Mirrors ``_find_next_swing_above``.
    """
    candidates: list[float] = []
    for b in (history or [])[:-1]:
        lo = getattr(b, "low", None)
        if lo is None:
            continue
        if float(lo) < float(threshold):
            candidates.append(float(lo))
    return max(candidates) if candidates else None


def compute_plan(
    market: Any,
    history: list,
    metadata: dict,
    direction: str,
) -> TradePlan | None:
    """Build a deterministic trade plan from BOS metadata + price history.

    Returns ``None`` when the structural break is too tight for a real
    trade or the inputs are unusable (no breakout level, bad direction,
    non-positive entry).
    """
    direction = (direction or "").lower()
    if direction not in ("long", "short"):
        return None

    breakout_level = metadata.get("breakout_level") if metadata else None
    if breakout_level is None:
        return None

    entry = float(getattr(market, "price", 0.0) or 0.0)
    if entry <= 0.0:
        return None

    breakout_level = float(breakout_level)
    buffer_pct = float(config.STOP_BUFFER_PCT)
    tp1_r = float(config.TP1_R_MULTIPLE)
    tp2_r = float(config.TP2_FALLBACK_R_MULTIPLE)

    if direction == "long":
        stop = breakout_level * (1.0 - buffer_pct)
    else:
        stop = breakout_level * (1.0 + buffer_pct)

    risk_per_unit = abs(entry - stop)
    min_risk = float(config.MIN_RISK_PCT_OF_ENTRY) * entry
    if risk_per_unit < min_risk:
        log.warning(
            "trade_plan: risk_per_unit %.6f < min %.6f for %s "
            "(entry=%.6f stop=%.6f) — structural break too tight",
            risk_per_unit, min_risk,
            getattr(market, "ticker", "?"),
            entry, stop,
        )
        return None

    if direction == "long":
        tp1 = entry + tp1_r * risk_per_unit
        tp2_fallback = entry + tp2_r * risk_per_unit
        # Only honor a structural pivot as TP2 if it's beyond TP1 — otherwise
        # TP1 would trigger first and TP2 becomes a lower-profit target, which
        # is nonsensical in a tiered take-profit ladder.
        next_swing = _find_next_swing_above(history or [], tp1)
        if next_swing is not None:
            tp2 = min(next_swing, tp2_fallback)
        else:
            tp2 = tp2_fallback
        r_multiple_tp1 = (tp1 - entry) / risk_per_unit
        r_multiple_tp2 = (tp2 - entry) / risk_per_unit
    else:
        tp1 = entry - tp1_r * risk_per_unit
        tp2_fallback = entry - tp2_r * risk_per_unit
        next_swing = _find_next_swing_below(history or [], tp1)
        if next_swing is not None:
            tp2 = max(next_swing, tp2_fallback)
        else:
            tp2 = tp2_fallback
        # Favorable-direction reward: positive when the short hits its target.
        r_multiple_tp1 = (entry - tp1) / risk_per_unit
        r_multiple_tp2 = (entry - tp2) / risk_per_unit

    # ---- Multi-stage exit ladder (#3) ----
    # ATR drives the trailing stop on the final tranche. When ATR isn't
    # computable (cold-start, tiny history) we omit the trail and the runner
    # collapses; TP1/TP2 still fire as static targets.
    atr_1h = ranker.compute_atr(history or [], period=int(config.ATR_PERIOD_HOURS))
    tp1_fraction = float(config.TP1_FRACTION)
    tp2_fraction = float(config.TP2_FRACTION)
    runner_fraction = max(0.0, 1.0 - tp1_fraction - tp2_fraction)

    if direction == "long":
        breakeven_trigger = entry + float(config.BREAKEVEN_TRIGGER_R) * risk_per_unit
    else:
        breakeven_trigger = entry - float(config.BREAKEVEN_TRIGGER_R) * risk_per_unit

    trail_atr_mult = float(config.TRAIL_ATR_MULT) if atr_1h else 0.0

    return TradePlan(
        direction=direction,
        entry=entry,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        risk_per_unit=risk_per_unit,
        r_multiple_tp1=r_multiple_tp1,
        r_multiple_tp2=r_multiple_tp2,
        tp1_fraction=tp1_fraction,
        tp2_fraction=tp2_fraction,
        breakeven_trigger=breakeven_trigger,
        trail_atr=atr_1h,
        trail_atr_mult=trail_atr_mult,
        runner_fraction=runner_fraction,
    )
