"""Four-rule suppression chain.

Rules evaluated in order; first match wins:
  1. dedup_4h        — same ticker emitted within DEDUP_HOURS
  2. btc_beta        — for crypto, |alpha_z| < ALPHA_Z_MIN AND |r_alpha_pct| < R_ALPHA_MIN_PCT
  3. sector_day      — asset-class daily count already at SECTOR_DAY_THRESHOLD
  4. budget_throttle — total daily emitted alerts at DAILY_ALERT_BUDGET

Returns: ("DROP", reason) or ("EMIT", "ok").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import config, storage

log = logging.getLogger(__name__)


@dataclass
class Alert:
    """Lightweight alert payload for the suppression layer."""
    ticker: str
    asset_class: str
    score: float
    alpha_z: float = 0.0
    r_alpha_pct: float = 0.0


def _is_crypto(asset_class: str) -> bool:
    return asset_class.startswith("crypto")


def _rule_dedup_4h(alert: Any) -> tuple[bool, str]:
    rows = storage.recent_alerts_for_ticker(alert.ticker, config.DEDUP_HOURS)
    for r in rows:
        if r["decision"] == "EMIT":
            return True, f"dedup_4h: emitted within {config.DEDUP_HOURS}h"
    return False, ""


def _rule_btc_beta(alert: Any) -> tuple[bool, str]:
    if not _is_crypto(getattr(alert, "asset_class", "")):
        return False, ""
    az = abs(getattr(alert, "alpha_z", 0.0) or 0.0)
    ra = abs(getattr(alert, "r_alpha_pct", 0.0) or 0.0)
    if az < config.ALPHA_Z_MIN and ra < config.R_ALPHA_MIN_PCT:
        return True, (
            f"btc_beta: alpha_z={az:.2f}<{config.ALPHA_Z_MIN}, "
            f"r_alpha_pct={ra:.2f}<{config.R_ALPHA_MIN_PCT}"
        )
    return False, ""


def _rule_sector_day(alert: Any) -> tuple[bool, str]:
    n = storage.asset_class_alerts_today(alert.asset_class, decision="EMIT")
    if n >= config.SECTOR_DAY_THRESHOLD:
        return True, f"sector_day: {alert.asset_class} already emitted {n} today"
    return False, ""


def _rule_budget_throttle(alert: Any) -> tuple[bool, str]:
    n = storage.alerts_today_count(decision="EMIT")
    if n >= config.DAILY_ALERT_BUDGET:
        return True, f"budget_throttle: {n} alerts already emitted today"
    return False, ""


_RULES = (
    ("dedup_4h", _rule_dedup_4h),
    ("btc_beta", _rule_btc_beta),
    ("sector_day", _rule_sector_day),
    ("budget_throttle", _rule_budget_throttle),
)


def evaluate(alert: Any) -> tuple[str, str]:
    """Run the suppression chain and return a decision tuple."""
    for name, fn in _RULES:
        try:
            drop, reason = fn(alert)
        except Exception as e:
            log.warning("suppression rule %s blew up: %s", name, e)
            continue
        if drop:
            return "DROP", reason
    return "EMIT", "ok"
