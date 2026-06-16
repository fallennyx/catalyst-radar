"""Execution layer — turns an EMIT into a risk-managed Lighter position.

See EXECUTOR_SPEC.md. This module owns the *decision* (which tier, what size,
is the circuit breaker tripped) and the *data capture* (signal_snapshots,
executions, positions). The actual exchange writes live in
``radar.lighter_exec``; the per-position exit lifecycle lives in
``radar.exit_engine``.

Invariants:
  * Fail-open — ``maybe_execute`` never raises. A bug here must never block the
    Telegram alert that already fired (matches the engine's no-suppression
    philosophy).
  * Two-stage gate — ``EXECUTOR_ENABLED`` runs the full decision + capture
    pipeline; ``EXECUTOR_LIVE`` is the separate switch that lets us touch real
    money. Shadow mode (ENABLED, not LIVE) captures everything and lets the
    exit engine simulate counterfactual exits with zero exchange interaction.
  * Risk-defined sizing — the dollar loss at the stop is fixed at
    ``MAX_LOSS_PER_TRADE_USD`` regardless of how wide the structural stop is.
    Leverage never determines size (the liquidation lesson).
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import config, storage

log = logging.getLogger(__name__)


# ============================================================================
# config versioning — stamp every trade so threshold changes never poison data
# ============================================================================

_GIT_SHA: str | None = None
_GIT_SHA_RESOLVED = False


def _git_sha() -> str | None:
    global _GIT_SHA, _GIT_SHA_RESOLVED
    if _GIT_SHA_RESOLVED:
        return _GIT_SHA
    _GIT_SHA_RESOLVED = True
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        _GIT_SHA = out.stdout.strip() or None
    except Exception:
        _GIT_SHA = None
    return _GIT_SHA


def _executor_config_dict() -> dict[str, Any]:
    """The executor-relevant config knobs that define a 'version' of the rules."""
    return {
        "enabled_tiers": sorted(config.EXECUTOR_ENABLED_TIERS),
        "tier_size_mult": config.TIER_SIZE_MULT,
        "tier_a_alpha_z_min": config.TIER_A_ALPHA_Z_MIN,
        "tier_a_score_pctile_min": config.TIER_A_SCORE_PCTILE_MIN,
        "tier_b_score_pctile_min": config.TIER_B_SCORE_PCTILE_MIN,
        "tier_cpop_cluster_min": config.TIER_CPOP_CLUSTER_MIN,
        "tier_cpop_btc_ret_4h_min": config.TIER_CPOP_BTC_RET_4H_MIN,
        "skip_trap_alpha_z_lo": config.SKIP_TRAP_ALPHA_Z_LO,
        "skip_trap_alpha_z_hi": config.SKIP_TRAP_ALPHA_Z_HI,
        "skip_trap_score_pctile": config.SKIP_TRAP_SCORE_PCTILE,
        "skip_blowoff_vol_ratio": config.SKIP_BLOWOFF_VOL_RATIO,
        "skip_inert_classes": sorted(config.SKIP_INERT_CLASSES),
        "max_loss_per_trade": config.MAX_LOSS_PER_TRADE_USD,
        "max_concurrent": config.MAX_CONCURRENT_POSITIONS,
        "max_total_exposure": config.MAX_TOTAL_EXPOSURE_USD,
        "leverage_cap": config.LEVERAGE_CAP,
        "time_exit_hours": config.TIME_EXIT_HOURS,
        "extension_threshold_r": config.EXTENSION_THRESHOLD_R,
        "max_hold_hours": config.MAX_HOLD_HOURS,
        "daily_max_loss": config.DAILY_MAX_LOSS_USD,
        "daily_max_trades": config.DAILY_MAX_TRADES,
        "consecutive_loss_halt": config.CONSECUTIVE_LOSS_HALT,
    }


def current_config_version_id(db_path: str | None = None) -> int:
    """Get-or-create the config_versions row for the live executor config."""
    cfg = _executor_config_dict()
    blob = json.dumps(cfg, sort_keys=True, default=str)
    config_hash = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return storage.get_or_create_config_version(
        config_hash,
        {
            "git_sha": _git_sha(),
            "created_at": datetime.utcnow().isoformat(),
            "enabled_tiers": ",".join(sorted(config.EXECUTOR_ENABLED_TIERS)),
            "max_loss_per_trade": config.MAX_LOSS_PER_TRADE_USD,
            "leverage_cap": float(config.LEVERAGE_CAP),
            "time_exit_hours": config.TIME_EXIT_HOURS,
            "extension_threshold_r": config.EXTENSION_THRESHOLD_R,
            "max_concurrent": config.MAX_CONCURRENT_POSITIONS,
            "daily_max_loss": config.DAILY_MAX_LOSS_USD,
            "full_config_json": blob,
        },
        db_path=db_path,
    )


# ============================================================================
# §2 tiering — direction-gate, pure function over validated features
# ============================================================================

@dataclass
class TierDecision:
    tier: str            # "A" | "B" | "C_pop" | "SKIP"
    reason: str          # machine reason tag
    tradeable: bool      # tier ∈ EXECUTOR_ENABLED_TIERS


def classify_tier(
    *,
    alpha_z: float | None,
    score_pctile: float | None,
    cluster_size: int | None,
    btc_ret_4h: float | None,
    vol_ratio: float | None,
    asset_class: str,
) -> TierDecision:
    """Map backtest-validated features → conviction tier. SKIP traps are checked
    first because a high score with marginal alpha_z is the 16.7%-WR trap, not a
    Tier-B trade (score is orthogonal to direction). [VALIDATED]"""
    a = abs(float(alpha_z)) if alpha_z is not None else 0.0
    sp = float(score_pctile) if score_pctile is not None else 0.0
    cs = int(cluster_size) if cluster_size is not None else 0
    btc = float(btc_ret_4h) if btc_ret_4h is not None else 0.0
    vr = float(vol_ratio) if vol_ratio is not None else 0.0

    def _decide(tier: str, reason: str) -> TierDecision:
        return TierDecision(tier, reason, tier in config.EXECUTOR_ENABLED_TIERS)

    # ---- SKIP traps (override tier assignment) ----
    if asset_class in config.SKIP_INERT_CLASSES:
        return _decide("SKIP", "inert_class")
    if vr > config.SKIP_BLOWOFF_VOL_RATIO and a < config.TIER_A_ALPHA_Z_MIN:
        return _decide("SKIP", "blowoff")
    if (config.SKIP_TRAP_ALPHA_Z_LO <= a < config.SKIP_TRAP_ALPHA_Z_HI
            and sp >= config.SKIP_TRAP_SCORE_PCTILE):
        return _decide("SKIP", "alpha_z_trap")

    # ---- positive tiers ----
    if a >= config.TIER_A_ALPHA_Z_MIN and sp >= config.TIER_A_SCORE_PCTILE_MIN:
        return _decide("A", "tier_a")
    if sp >= config.TIER_B_SCORE_PCTILE_MIN:
        return _decide("B", "tier_b")
    if cs >= config.TIER_CPOP_CLUSTER_MIN or btc > config.TIER_CPOP_BTC_RET_4H_MIN:
        return _decide("C_pop", "tier_cpop")
    return _decide("SKIP", "below_all_tiers")


# ============================================================================
# §3 sizing — risk_per_unit → contracts. Fixed $ loss at stop.
# ============================================================================

@dataclass
class Sizing:
    size_usd: float
    contracts: float
    score_mult: float
    leverage_used: float
    margin_usd: float


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_sizing(
    *,
    plan: Any,
    tier: str,
    score_pctile: float | None,
    max_leverage: float | None,
) -> Sizing | None:
    """size_usd = MAX_LOSS / (risk_per_unit/entry); contracts = size_usd/entry;
    × tier multiplier; × score multiplier (Tier A only). Returns None if the
    plan is unusable (non-positive entry or risk)."""
    entry = float(getattr(plan, "entry", 0.0) or 0.0)
    risk_per_unit = float(getattr(plan, "risk_per_unit", 0.0) or 0.0)
    if entry <= 0.0 or risk_per_unit <= 0.0:
        return None

    risk_frac = risk_per_unit / entry          # |entry-stop| / entry
    size_usd = config.MAX_LOSS_PER_TRADE_USD / risk_frac
    contracts = size_usd / entry

    tier_mult = float(config.TIER_SIZE_MULT.get(tier, 0.0))
    contracts *= tier_mult

    # Score sizes *up within a tier*, never overrides the gate — Tier A only.
    score_mult = 1.0
    if tier == "A" and score_pctile is not None:
        score_mult = _clamp(
            float(score_pctile) / config.SCORE_SIZE_PCTILE_DIVISOR,
            1.0, config.SCORE_SIZE_MULT_MAX,
        )
    contracts *= score_mult

    final_size_usd = contracts * entry
    lev = float(config.LEVERAGE_CAP)
    if max_leverage:
        lev = min(lev, float(max_leverage))
    lev = max(1.0, lev)
    margin_usd = final_size_usd / lev
    return Sizing(
        size_usd=final_size_usd,
        contracts=contracts,
        score_mult=score_mult,
        leverage_used=lev,
        margin_usd=margin_usd,
    )


# ============================================================================
# §6 circuit breaker — anti-blowup, independent of edge
# ============================================================================

@dataclass
class BreakerStatus:
    halted: bool
    reason: str | None


_BREAKER_PINGED: set[str] = set()


def _notify_breaker(reason: str | None) -> None:
    """Telegram ping on a breaker breach (§6), deduped per reason+UTC-day so a
    halt doesn't spam every EMIT. Best-effort — never raises."""
    if not reason:
        return
    key = f"{reason}:{datetime.utcnow().date().isoformat()}"
    if key in _BREAKER_PINGED:
        return
    _BREAKER_PINGED.add(key)
    try:
        from . import telegram
        telegram._send_main(
            f"🛑 *EXECUTOR HALT* — circuit breaker tripped: `{reason}`.\n"
            f"New entries blocked. Existing server-side stops stay live.",
            market_label="executor_breaker",
        )
    except Exception as e:
        log.warning("executor: breaker Telegram ping failed: %s", e)


def breaker_status(db_path: str | None = None) -> BreakerStatus:
    """Evaluate the four halt conditions. Existing server-side stops stay live
    regardless; this only gates *opening* new positions."""
    import os
    try:
        if os.path.exists(config.KILL_SWITCH_FILE):
            return BreakerStatus(True, "kill_switch")
        if storage.daily_realized_pnl_usd(db_path) <= -abs(config.DAILY_MAX_LOSS_USD):
            return BreakerStatus(True, "daily_max_loss")
        if storage.trades_opened_today(db_path) >= config.DAILY_MAX_TRADES:
            return BreakerStatus(True, "daily_max_trades")
        if storage.consecutive_losses(db_path) >= config.CONSECUTIVE_LOSS_HALT:
            return BreakerStatus(True, "consecutive_losses")
    except Exception as e:
        log.warning("breaker_status check failed (treating as halted): %s", e)
        return BreakerStatus(True, "breaker_check_error")
    return BreakerStatus(False, None)


# ============================================================================
# feature extraction + snapshot capture
# ============================================================================

def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _bundle(adjudicated: Any) -> dict:
    return dict(getattr(adjudicated, "signal_bundle", None) or {})


def _capture_snapshot(
    *, market: Any, metadata: dict, adjudicated: Any, tier_decision: TierDecision,
    tier_source: int, news_items: Any, config_version_id: int,
    db_path: str | None = None,
) -> int:
    """Write the full feature vector at alert time (one row per EMIT)."""
    b = _bundle(adjudicated)
    pred = getattr(adjudicated, "predictor_result", None)
    clf = metadata.get("classifier_result")
    now = datetime.now(tz=timezone.utc)

    def _clf(attr: str) -> Any:
        return getattr(clf, attr, None) if clf is not None else None

    evidence = _clf("evidence_quotes")
    try:
        evidence_json = json.dumps(list(evidence)) if evidence else None
    except (TypeError, ValueError):
        evidence_json = None
    try:
        news_json = json.dumps([
            {k: getattr(n, k, None) for k in ("source", "title", "url", "published")}
            for n in (news_items or [])
        ]) if news_items else None
    except Exception:
        news_json = None

    row = {
        "alert_ts": int(time.time()),
        "ticker": market.ticker,
        "asset_class": market.asset_class,
        "tier_decision": tier_decision.tier,
        "structure_type": metadata.get("structure_type"),
        "breakout_level": _f(metadata.get("breakout_level")),
        "swing_high_4h": _f(metadata.get("swing_high_reference")),
        "swing_low_4h": _f(metadata.get("swing_low_reference")),
        "swing_ref_ts": str(metadata.get("swing_reference_timestamp"))
            if metadata.get("swing_reference_timestamp") is not None else None,
        "median_bar_range_1h": _f(metadata.get("median_bar_range")),
        "distance_past_pivot_pct": _f(b.get("distance_past_pivot_pct")),
        "range_ratio_1h": _f(b.get("range_ratio_1h")),
        "range_ratio_15m": _f(b.get("range_ratio_15m")),
        "score": _f(metadata.get("score")),
        "score_pctile": _f(metadata.get("score_pctile")),
        "alpha_z": _f(metadata.get("alpha_z")),
        "r_alpha_pct": _f(metadata.get("r_alpha_pct")),
        "vol_ratio": _f(metadata.get("vol_ratio") if metadata.get("vol_ratio") is not None
                        else b.get("volume_ratio")),
        "volume_z": _f(b.get("volume_z")),
        "cluster_size": _i(metadata.get("cluster_size")),
        "pop_score": _f(metadata.get("pop_score")),
        "oi_velocity_z": _f(metadata.get("oi_velocity_z")),
        "funding_z": _f(metadata.get("funding_z")),
        "wash_penalty": _f(metadata.get("wash_penalty")),
        "oi_usd": _f(b.get("oi_usd")),
        "funding_1h": _f(b.get("funding_pct")),
        "book_bid_usd": _f(b.get("book_bid_usd") if b.get("book_bid_usd") is not None
                           else metadata.get("book_bid_usd")),
        "book_ask_usd": _f(b.get("book_ask_usd")),
        "book_ratio": _f(metadata.get("book_ratio") if metadata.get("book_ratio") is not None
                         else b.get("book_ratio")),
        "book_sentiment": metadata.get("book_sentiment") or b.get("book_sentiment"),
        "vpoc_price": _f(metadata.get("vpoc_price")),
        "vpoc_near_breakout": 1 if metadata.get("vpoc_near_breakout") else 0,
        "btc_ret_4h": _f(metadata.get("btc_ret_4h")),
        "btc_range_expansion": _f(metadata.get("btc_range_expansion")),
        "htf_trend_align_7d": (1 if b.get("htf_aligned") else 0)
            if b.get("htf_aligned") is not None else None,
        "adj_direction": getattr(adjudicated, "direction", None),
        "adj_confidence": _f(getattr(pred, "direction_confidence", None)),
        "adj_setup_quality": _f(getattr(pred, "setup_quality", None)),
        "adj_conviction_tier": getattr(adjudicated, "conviction_tier", None),
        "adj_flipped": 1 if getattr(adjudicated, "flipped", False) else 0,
        "adj_thesis": getattr(pred, "thesis", None) or getattr(pred, "continuation_thesis", None),
        "clf_catalyst_type": _clf("catalyst_type"),
        "clf_direction": _clf("direction"),
        "clf_confidence": _f(_clf("confidence")),
        "clf_summary": _clf("summary"),
        "clf_evidence_quotes": evidence_json,
        "news_items_json": news_json,
        "utc_hour": now.hour,
        "day_of_week": now.weekday(),
        "tier_source": tier_source,
        "watchlist_age_hours": _f(metadata.get("hours_on_watchlist")),
        "config_version_id": config_version_id,
        "raw_json": json.dumps({"signal_bundle": b}, default=str),
    }
    return storage.insert_row("signal_snapshots", row, db_path=db_path)


# ============================================================================
# public entry point — called from the EMIT branches in main.py
# ============================================================================

def maybe_execute(
    *,
    market: Any,
    plan: Any,
    metadata: dict,
    adjudicated: Any,
    tier: int,                 # engine tier (1 or 2) — the SOURCE, not conviction
    news_items: Any = None,
    db_path: str | None = None,
) -> dict | None:
    """Decide → size → capture → (optionally) place. Never raises.

    Returns a small result dict for logging/testing, or None when the executor
    is disabled or a fatal-but-swallowed error occurred.
    """
    if not config.EXECUTOR_ENABLED:
        return None
    try:
        return _execute_inner(
            market=market, plan=plan, metadata=metadata, adjudicated=adjudicated,
            tier_source=tier, news_items=news_items, db_path=db_path,
        )
    except Exception as e:  # fail-open — the alert already went out
        log.exception("executor.maybe_execute swallowed error for %s: %s",
                      getattr(market, "ticker", "?"), e)
        return None


def _execute_inner(
    *, market: Any, plan: Any, metadata: dict, adjudicated: Any,
    tier_source: int, news_items: Any, db_path: str | None,
) -> dict:
    ticker = market.ticker
    cfg_id = current_config_version_id(db_path)

    # ---- tier gate (direction) ----
    decision = classify_tier(
        alpha_z=_f(metadata.get("alpha_z")),
        score_pctile=_f(metadata.get("score_pctile")),
        cluster_size=_i(metadata.get("cluster_size")),
        btc_ret_4h=_f(metadata.get("btc_ret_4h")),
        vol_ratio=_f(metadata.get("vol_ratio")),
        asset_class=market.asset_class,
    )

    # ---- snapshot (always, even on skip) ----
    snap_id = _capture_snapshot(
        market=market, metadata=metadata, adjudicated=adjudicated,
        tier_decision=decision, tier_source=tier_source, news_items=news_items,
        config_version_id=cfg_id, db_path=db_path,
    )

    # adjudicated 'no_trade' or a plan we couldn't build → never act
    adj_dir = getattr(adjudicated, "direction", None)
    if plan is None or adj_dir not in ("long", "short"):
        return _record_skip(snap_id, decision, plan, "no_plan_or_no_trade", cfg_id, db_path)

    if not decision.tradeable:
        return _record_skip(snap_id, decision, plan,
                            f"tier_{decision.tier.lower()}:{decision.reason}", cfg_id, db_path)

    # ---- circuit breaker ----
    breaker = breaker_status(db_path)
    if breaker.halted:
        log.warning("executor: breaker HALT (%s) — skipping %s", breaker.reason, ticker)
        _notify_breaker(breaker.reason)
        return _record_skip(snap_id, decision, plan, f"breaker_{breaker.reason}", cfg_id, db_path)

    # ---- concurrency + exposure caps ----
    if storage.open_position_count(db_path) >= config.MAX_CONCURRENT_POSITIONS:
        return _record_skip(snap_id, decision, plan, "max_concurrent", cfg_id, db_path)

    sizing = compute_sizing(
        plan=plan, tier=decision.tier,
        score_pctile=_f(metadata.get("score_pctile")),
        max_leverage=getattr(market, "max_leverage", None),
    )
    if sizing is None or sizing.contracts <= 0:
        return _record_skip(snap_id, decision, plan, "bad_sizing", cfg_id, db_path)

    projected_exposure = storage.total_open_exposure_usd(db_path) + sizing.size_usd
    if projected_exposure > config.MAX_TOTAL_EXPOSURE_USD:
        return _record_skip(snap_id, decision, plan, "max_exposure", cfg_id, db_path)

    # ---- record the execution decision (acted reflects LIVE reality) ----
    will_act_live = bool(config.EXECUTOR_LIVE)
    exec_id = storage.insert_row("executions", {
        "signal_snapshot_id": snap_id,
        "acted": 1 if will_act_live else 0,
        "skip_reason": None if will_act_live else "shadow_mode",
        "conviction_tier": decision.tier,
        "risk_per_unit": float(plan.risk_per_unit),
        "intended_entry": float(plan.entry),
        "intended_stop": float(plan.stop),
        "intended_tp1": float(plan.tp1),
        "intended_tp2": float(plan.tp2),
        "computed_size_usd": sizing.size_usd,
        "computed_contracts": sizing.contracts,
        "size_mult_score": sizing.score_mult,
        "leverage_used": sizing.leverage_used,
        "free_margin_at_decision": None,
        "config_version_id": cfg_id,
        "created_at": datetime.utcnow().isoformat(),
    }, db_path=db_path)

    log.info(
        "executor %s: tier=%s %s size=$%.2f (%.6f contracts, ×%.2f score, lev=%.0f) "
        "entry=%.6f stop=%.6f tp1=%.6f [%s]",
        ticker, decision.tier, adj_dir, sizing.size_usd, sizing.contracts,
        sizing.score_mult, sizing.leverage_used, plan.entry, plan.stop, plan.tp1,
        "LIVE" if will_act_live else "SHADOW",
    )

    if not will_act_live:
        # Shadow: register a simulated position so the exit engine can mark it
        # and compute counterfactual exits — the actual v1 deliverable (§7).
        pos_id = _open_position_row(
            exec_id=exec_id, market=market, plan=plan, sizing=sizing,
            direction=adj_dir, tier=decision.tier, metadata=metadata,
            entry_price=float(plan.entry), config_version_id=cfg_id, db_path=db_path,
        )
        return {"acted": False, "shadow": True, "tier": decision.tier,
                "execution_id": exec_id, "position_id": pos_id, "sizing": sizing}

    # ---- LIVE placement ----
    from . import lighter_exec
    pos_id = lighter_exec.open_position(
        market=market, plan=plan, sizing=sizing, direction=adj_dir,
        tier=decision.tier, execution_id=exec_id, metadata=metadata,
        config_version_id=cfg_id, db_path=db_path,
    )
    return {"acted": True, "shadow": False, "tier": decision.tier,
            "execution_id": exec_id, "position_id": pos_id, "sizing": sizing}


def _open_position_row(
    *, exec_id: int, market: Any, plan: Any, sizing: Any, direction: str,
    tier: str, metadata: dict, entry_price: float, config_version_id: int,
    db_path: str | None,
) -> int:
    """Insert a positions row (shared by shadow + the post-fill live path)."""
    blowoff = _is_blowoff(metadata)
    return storage.insert_row("positions", {
        "execution_id": exec_id,
        "ticker": market.ticker,
        "direction": direction,
        "open_ts": int(time.time()),
        "entry_avg_price": float(entry_price),
        "size_contracts": float(sizing.contracts),
        "conviction_tier": tier,
        "stop_price_current": float(plan.stop),
        "plan_stop": float(plan.stop),
        "plan_tp1": float(plan.tp1),
        "blowoff_flag": 1 if blowoff else 0,
        "config_version_id": config_version_id,
        "raw_json": json.dumps({
            "tp2": float(plan.tp2),
            "risk_per_unit": float(plan.risk_per_unit),
            "alert_price": float(getattr(market, "price", 0.0) or 0.0),
            "size_usd": sizing.size_usd,
        }, default=str),
    }, db_path=db_path)


def _is_blowoff(metadata: dict) -> bool:
    """Force-+1h-close flag set: blowoff vol, meme, or hot cluster (§5)."""
    vr = _f(metadata.get("vol_ratio"))
    cs = _i(metadata.get("cluster_size"))
    ac = str(metadata.get("asset_class") or "")
    if vr is not None and vr > config.BLOWOFF_VOL_RATIO:
        return True
    if ac == "crypto_meme":
        return True
    if cs is not None and cs >= config.BLOWOFF_CLUSTER_MIN:
        return True
    return False


def _record_skip(
    snap_id: int, decision: TierDecision, plan: Any, reason: str,
    cfg_id: int, db_path: str | None,
) -> dict:
    """Persist a no-trade decision (still valuable: the v3 LLM-conflict re-test
    and the trap-WR audit need the skips too)."""
    exec_id = storage.insert_row("executions", {
        "signal_snapshot_id": snap_id,
        "acted": 0,
        "skip_reason": reason,
        "conviction_tier": decision.tier,
        "risk_per_unit": float(getattr(plan, "risk_per_unit", 0.0) or 0.0) if plan else None,
        "intended_entry": float(getattr(plan, "entry", 0.0) or 0.0) if plan else None,
        "intended_stop": float(getattr(plan, "stop", 0.0) or 0.0) if plan else None,
        "intended_tp1": float(getattr(plan, "tp1", 0.0) or 0.0) if plan else None,
        "intended_tp2": float(getattr(plan, "tp2", 0.0) or 0.0) if plan else None,
        "computed_size_usd": None,
        "computed_contracts": None,
        "size_mult_score": None,
        "leverage_used": None,
        "free_margin_at_decision": None,
        "config_version_id": cfg_id,
        "created_at": datetime.utcnow().isoformat(),
    }, db_path=db_path)
    return {"acted": False, "shadow": False, "skip_reason": reason,
            "tier": decision.tier, "execution_id": exec_id}
