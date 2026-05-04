"""BTC-beta gate.

For crypto assets we regress 1h returns of the ticker on 1h BTC returns over a
30-day rolling window, compute the alpha residual r_alpha = r_ticker - β·r_btc,
and Z-score the most recent residual against the historical residual series.

For non-crypto we don't have a meaningful BTC reference, so this gate is a
no-op: alpha_z = +inf and r_alpha_pct = market.pct_24h.

Public API:
    compute_alpha_z(market, history) -> tuple[alpha_z, r_alpha_pct]

`history` shape:
    {
        "ret_1h":     [...],   # ticker hourly returns, oldest → newest
        "btc_ret_1h": [...],   # BTC hourly returns same indexing
    }
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np

from . import config
from .universe import Market

History = Mapping[str, Sequence[float]]


def _is_crypto(asset_class: str) -> bool:
    return asset_class.startswith("crypto")


def _ols_beta(y: np.ndarray, x: np.ndarray) -> float:
    """Simple OLS slope (no intercept term — returns are zero-centered enough
    over a month that this is fine for a gating heuristic)."""
    if x.size < 24 or y.size < 24:
        return 0.0
    var_x = float(np.var(x, ddof=1))
    if var_x == 0.0 or not math.isfinite(var_x):
        return 0.0
    cov = float(np.mean((x - np.mean(x)) * (y - np.mean(y))))
    return cov / var_x


def compute_alpha_z(market: Market, history: History | None = None) -> tuple[float, float]:
    """Return (alpha_z, r_alpha_pct).

    For crypto, both numbers describe how unusual the most recent BTC-residual
    return is. For non-crypto, alpha_z is +inf (always passes) and r_alpha_pct
    falls back to pct_24h.
    """
    history = history or {}
    if not _is_crypto(market.asset_class):
        return float("inf"), float(market.pct_24h or 0.0)

    rets = list(history.get("ret_1h") or [])
    btc = list(history.get("btc_ret_1h") or [])
    n = min(len(rets), len(btc))
    if n < 24:
        # fall through: no history → treat as passing the gate but use pct_24h
        return float("inf"), float(market.pct_24h or 0.0)

    y = np.asarray(rets[-n:], dtype=float)
    x = np.asarray(btc[-n:], dtype=float)
    mask = np.isfinite(y) & np.isfinite(x)
    y, x = y[mask], x[mask]
    if y.size < 24:
        return float("inf"), float(market.pct_24h or 0.0)

    beta = _ols_beta(y, x)
    residuals = y - beta * x
    if residuals.size < 2:
        return 0.0, 0.0

    last = float(residuals[-1])
    body = residuals[:-1]
    sigma = float(np.std(body, ddof=1)) if body.size >= 2 else 0.0
    if sigma == 0.0 or not math.isfinite(sigma):
        return 0.0, last * 100.0

    alpha_z = (last - float(np.mean(body))) / sigma
    alpha_z = float(np.clip(alpha_z, -50.0, 50.0))

    # r_alpha_pct: the residual return expressed in % units
    r_alpha_pct = last * 100.0
    return alpha_z, r_alpha_pct
