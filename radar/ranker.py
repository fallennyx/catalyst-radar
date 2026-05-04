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
from typing import Iterable, Mapping, Sequence

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
