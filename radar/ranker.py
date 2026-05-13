"""Composite vol-normalized ranker.

Score = sum(weight_i * z_i) * class_multiplier

Components:
  pop_score      : current |1h-return| / rolling stdev of 1h returns
  oi_velocity_z  : z-score of 1h delta in open interest
  volume_z       : z-score of current 1h volume vs rolling mean
  funding_z      : z-score of current funding rate
  wash_penalty   : 1.0 if volume_24h_usd / oi_usd > 50, else 0.0

History contract — `history` is a dict keyed by ticker with arrays:
    {
        "ret_1h":   [float, ...],   # most recent at the end
        "vol_1h":   [float, ...],
        "oi_1h":    [float, ...],
        "funding":  [float, ...],
    }
Missing or short series → that component contributes 0 and the ranker
falls back to the cold-start rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from . import config
from .universe import Market

History = Mapping[str, Sequence[float]]


# ---------- numerical helpers ----------

def _safe_std(arr: Sequence[float]) -> float:
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size < 2:
        return 0.0
    s = float(np.std(a, ddof=1))
    return s if math.isfinite(s) else 0.0


def _safe_mean(arr: Sequence[float]) -> float:
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 0.0
    return float(np.mean(a))


def _z(value: float, arr: Sequence[float]) -> float:
    if not math.isfinite(value):
        return 0.0
    s = _safe_std(arr)
    m = _safe_mean(arr)
    if s == 0.0:
        # Constant series — use deviation from the mean expressed as a
        # multiple of |mean| so a spike against a flat history still scores.
        if m == 0.0 or value == m:
            return 0.0
        return float(np.clip((value - m) / abs(m), -10.0, 10.0))
    z = (value - m) / s
    return float(np.clip(z, -10.0, 10.0))


# ---------- component scores ----------

_COLD_START_TYPICAL_HOURLY_SIGMA = 0.02


def _pop_score(market: Market, history: History) -> float:
    """|1h return| normalized by 30-day stdev of 1h returns (Z-like, bounded).

    If history is too thin we fall back to a single global typical hourly vol
    (class-independent on purpose — CLASS_MULTIPLIER handles the per-class skew).
    """
    rets = history.get("ret_1h") or []
    if len(rets) >= 24:
        sigma = _safe_std(rets)
        if sigma > 0:
            return float(min(abs(market.pct_1h or 0.0) / 100.0 / sigma, 10.0))
    return float(min(abs(market.pct_1h or 0.0) / 100.0 / _COLD_START_TYPICAL_HOURLY_SIGMA, 10.0))


def _oi_velocity_z(market: Market, history: History) -> float:
    series = list(history.get("oi_1h") or [])
    if len(series) < 4:
        return 0.0
    deltas = np.diff(np.asarray(series, dtype=float))
    if deltas.size == 0:
        return 0.0
    return _z(float(deltas[-1]), deltas[:-1].tolist() or [0.0, 0.0])


def _volume_z(market: Market, history: History) -> float:
    series = list(history.get("vol_1h") or [])
    if len(series) < 4:
        return 0.0
    return _z(float(series[-1]), series[:-1])


def _funding_z(market: Market, history: History) -> float:
    series = list(history.get("funding") or [])
    if len(series) < 4:
        return _z(market.funding_1h, series) if series else 0.0
    return _z(market.funding_1h, series)


def _wash_penalty(market: Market) -> float:
    """Cheap proxy: extreme turnover (vol/OI) hints at wash trading."""
    if market.oi_usd <= 0:
        return 0.0
    turnover = market.volume_24h_usd / market.oi_usd
    return 1.0 if turnover > 50.0 else 0.0


# ---------- public API ----------

def compute_score(market: Market, history: History | None = None) -> float:
    history = history or {}
    components = {
        "pop_score": _pop_score(market, history),
        "oi_velocity_z": _oi_velocity_z(market, history),
        "volume_z": _volume_z(market, history),
        "funding_z": _funding_z(market, history),
        "wash_penalty": _wash_penalty(market),
    }
    raw = sum(config.RANKER_WEIGHTS[k] * components[k] for k in components)
    mult = config.CLASS_MULTIPLIER.get(market.asset_class, 1.0)
    return float(raw * mult)


def _cold_start_pct_threshold(asset_class: str) -> float:
    if asset_class in ("crypto_t1", "equity"):
        return 5.0
    return 10.0


def _cold_start_pass(market: Market) -> bool:
    return abs(market.pct_1h or 0.0) > _cold_start_pct_threshold(market.asset_class)


def _is_cold_start(history: History) -> bool:
    """True when we lack the rolling history needed for full vol-normalization."""
    rets = history.get("ret_1h") or []
    return len(rets) < 24


def top_n_movers(
    markets: Iterable[Market],
    histories: Mapping[str, History] | None = None,
    n: int = config.TOP_N_CANDIDATES,
) -> list[tuple[Market, float]]:
    """Filter by min-volume, score, and return the top N as (market, score).

    Cold-start markets (no rolling 1h history yet) must additionally clear the
    asset-class |pct_1h| threshold or they're dropped — score alone isn't
    trustworthy without history.
    """
    histories = histories or {}
    scored: list[tuple[Market, float]] = []

    for m in markets:
        if m.volume_24h_usd < config.MIN_VOLUME_24H_USD:
            continue
        hist = histories.get(m.ticker, {})
        if _is_cold_start(hist) and not _cold_start_pass(m):
            continue
        score = compute_score(m, hist)
        scored.append((m, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:n]


# ============================================================================
# BOS FILTER — structural swing detection + breakout confirmation
# ============================================================================
#
# The legacy engine alerted on closed-bar Donchian breaks, which fired ~4h
# late on a 4h timeframe (after the breakout candle had already closed). The
# BOS filter replaces that with structural swing-high/swing-low references —
# the actual prior pivots the market had been respecting — and a real-time
# trigger that fires the moment live mark price crosses a stored level with
# range expansion confirmed on the in-progress bar.


@dataclass
class SwingReference:
    price: float
    timestamp: datetime
    bars_validated: int  # number of subsequent bars that confirmed it as unbroken


def _bar_ts_to_datetime(ts: int) -> datetime:
    """Bars store unix-second `ts`. SwingReference.timestamp is a `datetime`.
    Use naive UTC to match storage's `datetime.utcnow().isoformat()` convention."""
    return datetime.utcfromtimestamp(int(ts))


# ---------- 4h bar synthesis ----------

_FOUR_HOUR_SECS = 4 * 3600


def synthesize_4h_bars(bars_1h: list) -> list:
    """Resample 1h Bar list into UTC-aligned 4h Bars.

    4h buckets start at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC.
    The most recent (in-progress) 4h bucket is included with whatever 1h bars
    are present so far — callers should treat ``-min_age_4h_bars`` as the
    cutoff rather than excluding it manually.

    Open/close come from the first/last hourly bar in the bucket; high/low
    are bucket extremes; volume/oi/funding are summed/averaged where they
    have meaning. Buckets with no 1h bars are silently skipped.
    """
    if not bars_1h:
        return []

    # Import here to avoid circular: ranker → storage → ranker would re-trigger.
    from .storage import Bar

    # Group 1h bars by their 4h bucket key.
    buckets: dict[int, list] = {}
    for b in bars_1h:
        key = (int(b.ts) // _FOUR_HOUR_SECS) * _FOUR_HOUR_SECS
        buckets.setdefault(key, []).append(b)

    out: list[Bar] = []
    for key in sorted(buckets):
        members = sorted(buckets[key], key=lambda b: int(b.ts))
        ticker = members[0].ticker
        opens = [m.open for m in members if m.open is not None]
        closes = [m.close for m in members if m.close is not None]
        highs = [m.high for m in members if m.high is not None]
        lows = [m.low for m in members if m.low is not None]
        vols = [m.volume for m in members if m.volume is not None]
        ois = [m.oi for m in members if m.oi is not None]
        fundings = [m.funding for m in members if m.funding is not None]
        out.append(Bar(
            ticker=ticker,
            ts=key,
            open=opens[0] if opens else None,
            high=max(highs) if highs else None,
            low=min(lows) if lows else None,
            close=closes[-1] if closes else None,
            volume=sum(vols) if vols else None,
            oi=ois[-1] if ois else None,  # OI is a stock, not a flow — use last
            funding=(sum(fundings) / len(fundings)) if fundings else None,
        ))
    return out


def find_swing_high(
    history: list,
    lookback_hours: int,
    min_age_hours: int,
    min_bars_validation: int,
) -> SwingReference | None:
    """Find the most recent significant unbroken swing high.

    A valid swing high is a bar whose high is the highest within an
    *eligible* window (the lookback window minus the most recent
    ``min_age_hours`` bars, which may include the in-progress break) AND
    has not been exceeded by any of the >=``min_bars_validation`` bars
    that came after it (excluding the current in-progress bar).

    Returns the highest qualifying swing — i.e. the candidate the market
    most recently respected. Returns None if no such pivot exists or if
    history is too short.
    """
    if len(history) < lookback_hours + min_age_hours + min_bars_validation:
        return None

    # Eligible window excludes the most recent `min_age_hours` bars so we
    # never pick the breakout candle's own wick as the reference.
    eligible = history[-(lookback_hours + min_age_hours):-min_age_hours]
    if not eligible:
        return None

    # Highest first — first unbroken candidate wins (matches the docstring
    # "Returns the highest qualifying swing"). The prompt's reference code
    # sorted by ts descending which contradicted the docstring; we honor the
    # docstring and the AMD-style use case (the level the market actually
    # respected, not just the most recent local high).
    candidates = sorted(
        eligible,
        key=lambda b: (b.high if b.high is not None else float("-inf")),
        reverse=True,
    )
    last_ts = history[-1].ts

    for candidate in candidates:
        candidate_high = candidate.high
        if candidate_high is None:
            continue
        subsequent = [b for b in history if b.ts > candidate.ts and b.ts < last_ts]
        if len(subsequent) < min_bars_validation:
            continue
        if any((b.high or float("-inf")) > candidate_high for b in subsequent):
            continue
        return SwingReference(
            price=float(candidate_high),
            timestamp=_bar_ts_to_datetime(candidate.ts),
            bars_validated=len(subsequent),
        )

    # Fallback: strict logic exhausted — every candidate was broken by a
    # subsequent bar. This is what trending markets look like (price ratchets
    # up making sequential new highs), and under strict logic the engine
    # would lose its reference at exactly the moment a breakout occurs.
    # Use the absolute highest in the eligible window as a Donchian-style
    # reference. bars_validated=0 signals "fallback" to consumers.
    best = candidates[0] if candidates else None
    if best is not None and best.high is not None:
        return SwingReference(
            price=float(best.high),
            timestamp=_bar_ts_to_datetime(best.ts),
            bars_validated=0,
        )
    return None


def find_swing_low(
    history: list,
    lookback_hours: int,
    min_age_hours: int,
    min_bars_validation: int,
) -> SwingReference | None:
    """Mirror of `find_swing_high`. Returns the most recent unbroken swing low."""
    if len(history) < lookback_hours + min_age_hours + min_bars_validation:
        return None
    eligible = history[-(lookback_hours + min_age_hours):-min_age_hours]
    if not eligible:
        return None
    # Lowest first — first unbroken candidate wins (mirror of find_swing_high).
    candidates = sorted(
        eligible,
        key=lambda b: (b.low if b.low is not None else float("inf")),
    )
    last_ts = history[-1].ts

    for candidate in candidates:
        candidate_low = candidate.low
        if candidate_low is None:
            continue
        subsequent = [b for b in history if b.ts > candidate.ts and b.ts < last_ts]
        if len(subsequent) < min_bars_validation:
            continue
        if any((b.low or float("inf")) < candidate_low for b in subsequent):
            continue
        return SwingReference(
            price=float(candidate_low),
            timestamp=_bar_ts_to_datetime(candidate.ts),
            bars_validated=len(subsequent),
        )

    # Fallback: strict logic exhausted — Donchian-style absolute lowest
    # in the eligible window. See find_swing_high for rationale.
    best = candidates[0] if candidates else None
    if best is not None and best.low is not None:
        return SwingReference(
            price=float(best.low),
            timestamp=_bar_ts_to_datetime(best.ts),
            bars_validated=0,
        )
    return None


def compute_volume_profile_poc(
    bars: list,
    n_buckets: int = 30,
) -> float | None:
    """Volume-by-price histogram → point-of-control (highest-volume price).

    Buckets the close-price range over ``bars`` into ``n_buckets`` equal-width
    slices and sums each bar's volume into the slice containing its close.
    The POC is the midpoint of the highest-volume slice — the price where the
    most trading happened over the lookback window. Useful as a "real" S/R
    reference: a break above a high-volume node is structurally more
    meaningful than a break above a random recent high.

    Returns ``None`` if bars are insufficient or volume is uniformly zero.
    """
    closes_vols: list[tuple[float, float]] = []
    for b in bars:
        c = getattr(b, "close", None)
        v = getattr(b, "volume", None)
        if c is None or v is None:
            continue
        cf, vf = float(c), float(v)
        if vf > 0 and cf > 0:
            closes_vols.append((cf, vf))
    if len(closes_vols) < n_buckets:
        return None
    prices = [c for c, _ in closes_vols]
    lo, hi = min(prices), max(prices)
    if hi <= lo:
        return None
    width = (hi - lo) / n_buckets
    buckets = [0.0] * n_buckets
    for c, v in closes_vols:
        idx = min(int((c - lo) / width), n_buckets - 1)
        buckets[idx] += v
    best = max(range(n_buckets), key=lambda i: buckets[i])
    poc_price = lo + (best + 0.5) * width
    return float(poc_price)


def is_breakout_near_poc(
    breakout_level: float | None,
    poc_price: float | None,
    tolerance_pct: float = 0.005,
) -> bool:
    """True if the breakout level is within ±tolerance_pct of the VPOC.
    Used as a confirmation badge in the alert body — not a suppression gate.
    A break that aligns with the volume node is structurally bigger than a
    break above a random recent high."""
    if breakout_level is None or poc_price is None:
        return False
    return abs(float(breakout_level) - float(poc_price)) / float(poc_price) <= tolerance_pct


def compute_median_range(bars: list) -> float:
    """Median (high - low) over the provided bars."""
    ranges: list[float] = []
    for b in bars:
        h, l = getattr(b, "high", None), getattr(b, "low", None)
        if h is None or l is None:
            continue
        ranges.append(float(h) - float(l))
    if not ranges:
        return 0.0
    ranges.sort()
    return ranges[len(ranges) // 2]


def compute_median_volume(bars: list) -> float:
    """Median volume over the provided bars. 0.0 if insufficient data."""
    vols: list[float] = []
    for b in bars:
        v = getattr(b, "volume", None)
        if v is None:
            continue
        vf = float(v)
        if vf > 0:
            vols.append(vf)
    if not vols:
        return 0.0
    vols.sort()
    return vols[len(vols) // 2]


def compute_atr(bars: list, period: int = 14) -> float | None:
    """Wilder's true-range mean over the last `period` bars.

    Returns ``None`` if there are fewer than ``period + 1`` usable bars (we
    need a previous close for each TR calculation).
    """
    if not bars or len(bars) < period + 1:
        return None
    trs: list[float] = []
    prev_close: float | None = None
    for b in bars:
        h = getattr(b, "high", None)
        l = getattr(b, "low", None)
        c = getattr(b, "close", None)
        if h is None or l is None or c is None:
            prev_close = None
            continue
        h, l, c = float(h), float(l), float(c)
        if prev_close is None:
            prev_close = c
            continue
        tr = max(h - l, abs(h - prev_close), abs(prev_close - l))
        trs.append(tr)
        prev_close = c
    if len(trs) < period:
        return None
    window = trs[-period:]
    return sum(window) / len(window)


def htf_trend_aligned(history: list, direction: str, lookback_hours: int) -> bool:
    """Soft daily-trend filter using the median close over `lookback_hours`.

    Long alerts must have current price strictly above the lookback median;
    short alerts must have it strictly below. Returns True (aligned) when
    insufficient history exists — we'd rather alert than over-filter on
    cold-start.
    """
    if not history or lookback_hours <= 0:
        return True
    window = history[-lookback_hours:] if len(history) > lookback_hours else history
    closes = [float(b.close) for b in window if getattr(b, "close", None) is not None]
    if len(closes) < 24:  # need at least one day of context
        return True
    closes.sort()
    median_close = closes[len(closes) // 2]
    last_close = float(history[-1].close or 0.0)
    if direction == "long":
        return last_close > median_close
    if direction == "short":
        return last_close < median_close
    return True


def _confirm_range_expansion(
    history: list,
    multiplier: float,
    lookback_window: int,
) -> bool:
    """Check the in-progress bar's range + volume against the lookback median.

    Used by both the 1h and 15m confirmation paths in has_breakout_structure.
    Returns True iff the bar at history[-1] has range > multiplier × median
    AND (if volume data is present) volume > VOLUME_EXPANSION_MULTIPLIER ×
    median volume.
    """
    if not history:
        return False
    lookback = history[-(lookback_window + 1):-1]
    if len(lookback) < 10:
        return False
    median_range = compute_median_range(lookback)
    if median_range <= 0:
        return False
    current_bar = history[-1]
    current_range = float((current_bar.high or 0.0) - (current_bar.low or 0.0))
    if current_range <= multiplier * median_range:
        return False
    if config.REQUIRE_VOLUME_CONFIRMATION:
        median_vol = compute_median_volume(lookback)
        current_vol = float(getattr(current_bar, "volume", 0.0) or 0.0)
        if median_vol > 0 and current_vol > 0:
            if current_vol < config.VOLUME_EXPANSION_MULTIPLIER * median_vol:
                return False
    return True


def has_breakout_structure(
    market: Any,
    history: list,
    current_price: float | None = None,
    history_15m: list | None = None,
) -> tuple[bool, str | None, float | None, str | None]:
    """Multi-timeframe BOS detection: 4h structural path + 1h early-detection path.

    Returns ``(broke_structure, direction, breakout_level, structure_type)`` where
    ``structure_type`` is ``"4h"`` (high-conviction 4h pivot break) or ``"1h"``
    (early-detection 1h pivot break). ``breakout_level`` is the reference pivot.

    Priority order: 4h path fires first when it can; 1h path is the fallback
    (lower confirmation threshold, smaller pivot, fires mid-candle or sooner).

    4h path requirements (high conviction):
      - 1h range ≥ RANGE_EXPANSION_MULTIPLIER (2.0×) OR 15m range ≥ 2.5×
      - Price crosses a UTC-aligned 4h swing pivot

    1h path requirements (early detection, BOS_1H_ENABLED must be True):
      - 1h range ≥ RANGE_EXPANSION_MULTIPLIER_1H_ENTRY (1.5×) OR 15m range ≥ 2.5×
      - Price crosses a 1h swing pivot (24h lookback, 2h validation)
    """
    if not history:
        return (False, None, None, None)
    if current_price is None:
        current_price = float(history[-1].close or 0.0)
    else:
        current_price = float(current_price)

    # ---- Range confirmation pre-checks ----
    confirmed_1h = _confirm_range_expansion(
        history,
        multiplier=config.RANGE_EXPANSION_MULTIPLIER,
        lookback_window=config.SWING_LOOKBACK_HOURS,
    )
    confirmed_15m = False
    if history_15m:
        confirmed_15m = _confirm_range_expansion(
            history_15m,
            multiplier=config.RANGE_EXPANSION_MULTIPLIER_15M,
            lookback_window=config.SWING_LOOKBACK_15M_BARS,
        )

    # ---- Priority 1: 4h structural break (high conviction) ----
    if confirmed_1h or confirmed_15m:
        bars_4h = synthesize_4h_bars(history)
        needed_4h = (
            config.SWING_LOOKBACK_4H_BARS
            + config.SWING_MIN_AGE_4H_BARS
            + config.SWING_MIN_BARS_VALIDATION_4H
        )
        if len(bars_4h) >= needed_4h:
            swing_high_4h = find_swing_high(
                bars_4h,
                lookback_hours=config.SWING_LOOKBACK_4H_BARS,
                min_age_hours=config.SWING_MIN_AGE_4H_BARS,
                min_bars_validation=config.SWING_MIN_BARS_VALIDATION_4H,
            )
            if swing_high_4h and current_price > swing_high_4h.price:
                if config.REQUIRE_HTF_TREND_ALIGNMENT and not htf_trend_aligned(
                    history, "long", config.HTF_TREND_LOOKBACK_HOURS,
                ):
                    pass  # fall through to 1h path
                else:
                    return (True, "long", swing_high_4h.price, "4h")

            swing_low_4h = find_swing_low(
                bars_4h,
                lookback_hours=config.SWING_LOOKBACK_4H_BARS,
                min_age_hours=config.SWING_MIN_AGE_4H_BARS,
                min_bars_validation=config.SWING_MIN_BARS_VALIDATION_4H,
            )
            if swing_low_4h and current_price < swing_low_4h.price:
                if config.REQUIRE_HTF_TREND_ALIGNMENT and not htf_trend_aligned(
                    history, "short", config.HTF_TREND_LOOKBACK_HOURS,
                ):
                    pass  # fall through to 1h path
                else:
                    return (True, "short", swing_low_4h.price, "4h")

    # ---- Priority 2: 1h structural break (early detection) ----
    if not config.BOS_1H_ENABLED:
        return (False, None, None, None)

    # 1h path uses a lower range expansion threshold (1.5×) so it fires earlier.
    confirmed_1h_entry = confirmed_1h or _confirm_range_expansion(
        history,
        multiplier=config.RANGE_EXPANSION_MULTIPLIER_1H_ENTRY,
        lookback_window=config.SWING_LOOKBACK_1H_BOS_BARS,
    )
    if not (confirmed_1h_entry or confirmed_15m):
        return (False, None, None, None)

    needed_1h = (
        config.SWING_LOOKBACK_1H_BOS_BARS
        + config.SWING_MIN_AGE_1H_BOS_BARS
        + config.SWING_MIN_BARS_VALIDATION_1H
    )
    if len(history) < needed_1h:
        return (False, None, None, None)

    swing_high_1h = find_swing_high(
        history,
        lookback_hours=config.SWING_LOOKBACK_1H_BOS_BARS,
        min_age_hours=config.SWING_MIN_AGE_1H_BOS_BARS,
        min_bars_validation=config.SWING_MIN_BARS_VALIDATION_1H,
    )
    if swing_high_1h and current_price > swing_high_1h.price:
        if config.REQUIRE_HTF_TREND_ALIGNMENT and not htf_trend_aligned(
            history, "long", config.HTF_TREND_LOOKBACK_HOURS,
        ):
            return (False, None, None, None)
        return (True, "long", swing_high_1h.price, "1h")

    swing_low_1h = find_swing_low(
        history,
        lookback_hours=config.SWING_LOOKBACK_1H_BOS_BARS,
        min_age_hours=config.SWING_MIN_AGE_1H_BOS_BARS,
        min_bars_validation=config.SWING_MIN_BARS_VALIDATION_1H,
    )
    if swing_low_1h and current_price < swing_low_1h.price:
        if config.REQUIRE_HTF_TREND_ALIGNMENT and not htf_trend_aligned(
            history, "short", config.HTF_TREND_LOOKBACK_HOURS,
        ):
            return (False, None, None, None)
        return (True, "short", swing_low_1h.price, "1h")

    return (False, None, None, None)


def precompute_references_for_watchlist(
    market: Any,
    history: list,
) -> tuple[float | None, float | None, datetime | None, float]:
    """Precompute **4h** swing references + 1h median range for a watchlist row.

    The watchlist now stores the 4h structural levels (since those are what
    Tier 2 polls against). The median range stays on the 1h frame because
    Tier 2's range-expansion check runs on the in-progress 1h bar.

    Returns ``(swing_high_price, swing_low_price, swing_timestamp, median_range_1h)``.
    If history is too short, returns ``(None, None, None, 0.0)``.
    """
    if not history:
        return (None, None, None, 0.0)

    bars_4h = synthesize_4h_bars(history)
    swing_high = swing_low = None
    if len(bars_4h) >= (
        config.SWING_LOOKBACK_4H_BARS
        + config.SWING_MIN_AGE_4H_BARS
        + config.SWING_MIN_BARS_VALIDATION_4H
    ):
        swing_high = find_swing_high(
            bars_4h,
            config.SWING_LOOKBACK_4H_BARS,
            config.SWING_MIN_AGE_4H_BARS,
            config.SWING_MIN_BARS_VALIDATION_4H,
        )
        swing_low = find_swing_low(
            bars_4h,
            config.SWING_LOOKBACK_4H_BARS,
            config.SWING_MIN_AGE_4H_BARS,
            config.SWING_MIN_BARS_VALIDATION_4H,
        )

    lookback_1h = history[-(config.SWING_LOOKBACK_HOURS + 1):-1]
    median_range_1h = compute_median_range(lookback_1h)

    swing_high_price = swing_high.price if swing_high else None
    swing_low_price = swing_low.price if swing_low else None
    swing_ts: datetime | None = None
    if swing_high and swing_low:
        swing_ts = max(swing_high.timestamp, swing_low.timestamp)
    elif swing_high:
        swing_ts = swing_high.timestamp
    elif swing_low:
        swing_ts = swing_low.timestamp

    return (swing_high_price, swing_low_price, swing_ts, median_range_1h)


def check_breakout_against_stored_references(
    current_price: float,
    current_bar_range: float,
    swing_high_reference: float | None,
    swing_low_reference: float | None,
    median_bar_range: float,
    direction_bias: str,
) -> tuple[bool, str | None, float | None]:
    """Tier-2 lightweight BOS check. Compares live price + current bar range
    against PRE-STORED references; never re-runs swing detection.

    Only fires in the direction that matches ``direction_bias`` — this prevents
    a long-bias watchlist entry from firing on a short-side break."""
    if median_bar_range is None or median_bar_range <= 0:
        return (False, None, None)
    if float(current_bar_range) <= config.RANGE_EXPANSION_MULTIPLIER * float(median_bar_range):
        return (False, None, None)

    if direction_bias == "long" and swing_high_reference is not None:
        if float(current_price) > float(swing_high_reference):
            return (True, "long", float(swing_high_reference))

    if direction_bias == "short" and swing_low_reference is not None:
        if float(current_price) < float(swing_low_reference):
            return (True, "short", float(swing_low_reference))

    return (False, None, None)
