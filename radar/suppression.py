"""BOS-aware suppression chain.

Rules evaluated in order (first match wins). The signature is:

    evaluate(market, alert, history) -> tuple[decision, reason, metadata]

where ``decision`` is one of ``"EMIT"``, ``"WATCHLIST"``, or ``"DROP"`` and
``metadata`` carries the swing references / breakout level for downstream
consumers (telegram payload, alerts log).

Rules
-----
0. **Structural BOS check.**
   - If structure broke at scan time AND direction agrees with the LLM bias,
     remove any existing watchlist entry for the ticker and continue to
     Rules 1-4 to gate the emit.
   - If structure broke but direction disagrees → DROP (``structure_direction_conflict``).
   - If structure didn't break:
       * score >= ``WATCHLIST_SCORE_THRESHOLD`` → add to watchlist, return WATCHLIST.
       * else → DROP (``no_structure_break``).
1. **Per-catalyst dedup.** If an EMIT for this ticker + same catalyst_type
   fired in the last 4h → DROP (``dedup_4h``).
2. **BTC-beta gate (crypto only).** Drop if alpha_z OR r_alpha_pct fall
   below their respective minima (see notes in BOS_FILTER_NOTES.md — this is
   stricter than the legacy AND-gate).
3. **Sector-day cluster.** If the asset_class has already emitted
   ``SECTOR_DAY_THRESHOLD`` alerts in the last 4h, only the highest-scoring
   new candidate breaks through.
4. **Daily budget throttle.** Once daily emits hit ``DAILY_ALERT_BUDGET``,
   only candidates above today's median EMIT score break through.

Anything that survives all four gates returns ``("EMIT", "ok", metadata)``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import beta, config, ranker, storage

log = logging.getLogger(__name__)


@dataclass
class Alert:
    """Lightweight alert payload threaded through the suppression chain.

    ``classifier_result`` is the full pydantic ``ClassifierResult`` and is
    required by the new BOS rules (we read direction, catalyst_type, etc.)
    """
    ticker: str
    asset_class: str
    score: float
    alpha_z: float = 0.0
    r_alpha_pct: float = 0.0
    classifier_result: Any | None = None


# ---------- helpers ----------

def _is_crypto(asset_class: str) -> bool:
    return asset_class.startswith("crypto")


def _classifier_json(result: Any) -> str:
    """Serialize the pydantic ClassifierResult to a JSON string for storage."""
    if result is None:
        return "{}"
    if hasattr(result, "model_dump_json"):
        return result.model_dump_json()
    if hasattr(result, "dict"):
        try:
            return json.dumps(result.dict())
        except Exception:
            return "{}"
    return "{}"


def _direction(result: Any) -> str | None:
    if result is None:
        return None
    return getattr(result, "direction", None)


def _catalyst_type(result: Any) -> str | None:
    if result is None:
        return None
    return getattr(result, "catalyst_type", None)


def _summary(result: Any) -> str:
    if result is None:
        return ""
    return (
        getattr(result, "primary_catalyst", None)
        or getattr(result, "summary", "")
        or ""
    )


# ---------- the chain ----------

def evaluate(
    market: Any,
    alert: Alert,
    history: list,
    history_15m: list | None = None,
) -> tuple[str, str, dict]:
    """Run the BOS-aware suppression chain. See module docstring for rule order.

    ``history_15m`` (optional) enables the parallel 15m confirmation gate —
    BOS fires when EITHER the in-progress 1h or 15m bar's range clears its
    expansion multiplier. When None, only the 1h gate is evaluated.
    """

    # Compute BOS + watchlist references up-front so every return path can
    # ship them in the metadata dict.
    broke_structure, structure_dir, breakout_level, structure_type = (
        ranker.has_breakout_structure(
            market, history, current_price=market.price, history_15m=history_15m,
        )
    )
    swing_high_ref, swing_low_ref, swing_ts, median_range = (
        ranker.precompute_references_for_watchlist(market, history)
    )
    metadata = {
        "breakout_level": breakout_level,
        "structure_direction": structure_dir,
        "structure_type": structure_type,
        "swing_high_reference": swing_high_ref,
        "swing_low_reference": swing_low_ref,
        "swing_reference_timestamp": swing_ts,
        "median_bar_range": median_range,
    }

    classifier_dir = _direction(alert.classifier_result)

    # ---- Rule 0: structural break or watchlist routing ----
    if broke_structure:
        # v3.2: structure is the TRIGGER, not the direction. The downstream
        # direction_adjudicator (radar.direction_adjudicator) is the authority
        # on the final long/short/no_trade call. We no longer flag a
        # "direction_conflict" here — the adjudicator decides what to do with
        # classifier vs structure disagreement.
        # BOS confirmed → promote any existing watchlist entry for this ticker
        # by removing it (the EMIT we're about to fire supersedes it). Then
        # fall through to the remaining rules.
        try:
            storage.remove_from_watchlist(market.ticker)
        except Exception as e:
            log.warning("watchlist remove failed for %s: %s", market.ticker, e)
    else:
        # No BOS yet → maybe park on the watchlist
        if alert.score >= config.WATCHLIST_SCORE_THRESHOLD and classifier_dir in ("long", "short"):
            try:
                storage.add_to_watchlist(
                    ticker=market.ticker,
                    asset_class=market.asset_class,
                    direction_bias=classifier_dir,
                    score=float(alert.score),
                    catalyst_summary=_summary(alert.classifier_result),
                    classifier_json=_classifier_json(alert.classifier_result),
                    swing_high_reference=swing_high_ref,
                    swing_low_reference=swing_low_ref,
                    swing_reference_timestamp=swing_ts,
                    median_bar_range=median_range or 0.0,
                    ttl_hours=config.WATCHLIST_TTL_HOURS,
                )
            except Exception as e:
                log.warning("watchlist add failed for %s: %s", market.ticker, e)
            return ("WATCHLIST", "awaiting_structure_break", metadata)
        return ("DROP", "no_structure_break", metadata)

    # ---- Rule 1: per-catalyst-type dedup over 4h ----
    if storage.recent_alert_exists(market.ticker, _catalyst_type(alert.classifier_result),
                                    hours=config.DEDUP_HOURS):
        return ("DROP", "dedup_4h", metadata)

    # ---- Rule 2: BTC-beta gate (crypto only) ----
    # Bypass when the current bar range is a high-conviction impulse — a
    # >2.5x range break is a genuine move regardless of BTC correlation, and
    # filtering it out costs us first-leg breakouts (the engine's main weakness
    # before this carve-out: it only caught continuation breaks after the
    # 24h-pct move had grown past R_ALPHA_MIN_PCT).
    if _is_crypto(market.asset_class):
        is_impulse = _is_impulse_break(history, median_range)
        if not is_impulse:
            rets, btc_rets = _crypto_returns_from_history(market, history)
            alpha_z, r_alpha = beta.compute_alpha_z(market, {"ret_1h": rets, "btc_ret_1h": btc_rets})
            # Per spec: drop if EITHER is weak. Stricter than the legacy AND gate.
            if abs(alpha_z) < config.ALPHA_Z_MIN or abs(r_alpha) < config.R_ALPHA_MIN_PCT:
                return ("DROP", "pure_btc_beta", metadata)

    # ---- Rule 3 REMOVED ----
    # Sector-day clustering was suppressing leg-following alerts in hot
    # sectors. Per the v3 enrichment-only policy, every BOS-confirmed setup
    # fires — alert fatigue is not a concern.

    # ---- Rule 4: daily budget throttle ----
    if storage.count_alerts_today() >= config.DAILY_ALERT_BUDGET:
        if alert.score < storage.median_score_today():
            return ("DROP", "budget_throttle", metadata)

    return ("EMIT", "ok", metadata)


def _is_impulse_break(history: list, median_range: float | None) -> bool:
    """True if the in-progress bar's range is a high-conviction impulse — i.e.
    significantly wider than the BOS minimum (1.5x). When True we trust the
    structural break enough to bypass the BTC-beta correlation filter."""
    if not history or median_range is None or median_range <= 0:
        return False
    cur = history[-1]
    cur_range = float((cur.high or 0.0) - (cur.low or 0.0))
    return cur_range > config.IMPULSE_BYPASS_MULTIPLIER * float(median_range)


def _crypto_returns_from_history(market: Any, history: list) -> tuple[list[float], list[float]]:
    """Project the BAR-shaped history into hourly returns for the beta gate.
    BTC returns are pulled separately from storage; if BTC isn't in the DB
    yet the gate effectively passes (alpha_z = +inf in beta.compute_alpha_z).
    """
    closes = [b.close for b in history if b.close is not None]
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev)
    btc_rets: list[float] = []
    if market.ticker != "BTC":
        try:
            btc_bars = storage.recent_bars("BTC", hours=config.ROLLING_WINDOW_DAYS * 24)
            btc_closes = [b.close for b in btc_bars if b.close is not None]
            for i in range(1, len(btc_closes)):
                prev = btc_closes[i - 1]
                if prev:
                    btc_rets.append((btc_closes[i] - prev) / prev)
        except Exception as e:
            log.debug("btc history fetch failed: %s", e)
    return rets, btc_rets
