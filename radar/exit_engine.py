"""Exit engine — the highest-EV lever (EXECUTOR_SPEC.md §5).

A lightweight async loop (reuses the Tier-2 60s cadence) that, for every open
position:
  * samples a per-minute mark into ``position_marks`` (the #1 missing backtest
    variable — lets us answer "did the +1h win hold or just close green after a
    wick that stopped me out");
  * tracks MFE / MAE / pnl_at_1h / pnl_at_4h;
  * applies the asymmetric exit rule — cut flat/red at +1h, let Tier-A green run
    to +4h on a breakeven-then-ATR trail. [VALIDATED — flips book Σ −9.3 → +112.2]

Shadow mode (``EXECUTOR_ENABLED`` but not ``EXECUTOR_LIVE``) *simulates* the
exit: it marks the position closed in the DB at the triggering price with the
counterfactual PnL, so the dataset is built with zero exchange interaction.
Live mode routes the close/modify through ``radar.lighter_exec``.

Marks continue to +MAX_HOLD even after a simulated exit is not done here (the
spec's "keep marking to +4h after exit" is a research nicety we can add once
live fills exist); v1 stops marking at exit to keep the row count bounded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import config, storage, universe

log = logging.getLogger(__name__)

_RUNNING = True


def stop() -> None:
    global _RUNNING
    _RUNNING = False


# ============================================================================
# pnl helpers
# ============================================================================

def _pnl_pct(direction: str, entry: float, mark: float) -> float:
    if entry <= 0:
        return 0.0
    if direction == "long":
        return (mark - entry) / entry
    return (entry - mark) / entry


def _pnl_r(direction: str, entry: float, mark: float, risk_per_unit: float) -> float:
    if risk_per_unit <= 0:
        return 0.0
    if direction == "long":
        return (mark - entry) / risk_per_unit
    return (entry - mark) / risk_per_unit


def _stop_hit(direction: str, stop_price: float | None, mark: float) -> bool:
    if stop_price is None:
        return False
    if direction == "long":
        return mark <= float(stop_price)
    return mark >= float(stop_price)


def _raw(position: dict) -> dict:
    try:
        return json.loads(position.get("raw_json") or "{}")
    except (TypeError, ValueError):
        return {}


# ============================================================================
# per-position evaluation
# ============================================================================

def evaluate_exit(position: dict, mark: float, minutes: float) -> tuple[str | None, dict]:
    """Decide what to do with an open position at this mark.

    Returns ``(action, fields)`` where action is one of:
      * ``"stop"`` / ``"time_exit"`` / ``"max_hold"`` — close now, with reason.
      * ``"extend"`` — Tier-A runner working at +1h: move stop to breakeven /
        trail. ``fields`` carries the new stop_price_current + flags.
      * ``None`` — hold, no change.
    Pure function (no I/O) so it is unit-testable.
    """
    direction = position.get("direction") or "long"
    entry = float(position.get("entry_avg_price") or 0.0)
    tier = position.get("conviction_tier") or "A"
    stop_price = position.get("stop_price_current")
    risk = float(_raw(position).get("risk_per_unit") or 0.0)
    blowoff = bool(position.get("blowoff_flag"))

    # 1) stop hit (server-side in live; simulated in shadow)
    if _stop_hit(direction, stop_price, mark):
        return ("stop", {})

    # 2) hard cap
    if minutes >= config.MAX_HOLD_HOURS * 60.0:
        return ("max_hold", {})

    # 3) the +1h decision
    if minutes >= config.TIME_EXIT_HOURS * 60.0:
        # blowoff/meme/cluster → mandatory +1h close, override any extension
        if blowoff:
            return ("time_exit", {})
        if tier != "A":
            return ("time_exit", {})
        # Tier A: extend only if already working past +0.5R
        pnl_r = _pnl_r(direction, entry, mark, risk)
        if pnl_r > config.EXTENSION_THRESHOLD_R:
            if not position.get("trailing_active"):
                # first extension: move stop to breakeven, arm the trail
                return ("extend", {
                    "stop_price_current": entry,
                    "breakeven_moved": 1,
                    "trailing_active": 1,
                })
            # already trailing: ratchet the stop by 1R (ATR proxy) in our favor
            if direction == "long":
                new_stop = max(float(stop_price or entry), mark - risk)
            else:
                new_stop = min(float(stop_price or entry), mark + risk)
            if new_stop != stop_price:
                return ("extend", {"stop_price_current": new_stop})
            return (None, {})
        # not working at +1h → cut
        return ("time_exit", {})

    return (None, {})


# ============================================================================
# I/O: mark + close
# ============================================================================

def _mark_position(position: dict, mark: float, minutes: float, db_path: str | None) -> None:
    direction = position.get("direction") or "long"
    entry = float(position.get("entry_avg_price") or 0.0)
    pnl_pct = _pnl_pct(direction, entry, mark)
    storage.insert_row("position_marks", {
        "position_id": int(position["id"]),
        "ts": int(time.time()),
        "mark_price": float(mark),
        "unrealized_pnl_pct": pnl_pct * 100.0,
        "minutes_since_entry": minutes,
    }, db_path=db_path)

    patch: dict[str, Any] = {}
    # MFE / MAE in pct terms
    mfe = position.get("mfe_pct")
    mae = position.get("mae_pct")
    pct = pnl_pct * 100.0
    if mfe is None or pct > float(mfe):
        patch["mfe_pct"] = pct
        patch["time_to_mfe_min"] = minutes
    if mae is None or pct < float(mae):
        patch["mae_pct"] = pct
        patch["time_to_mae_min"] = minutes
    # snapshot pnl at the +1h / +4h marks (record once as we cross)
    if position.get("pnl_at_1h_pct") is None and minutes >= config.TIME_EXIT_HOURS * 60.0:
        patch["pnl_at_1h_pct"] = pct
    if position.get("pnl_at_4h_pct") is None and minutes >= config.MAX_HOLD_HOURS * 60.0:
        patch["pnl_at_4h_pct"] = pct
    if patch:
        storage.update_row("positions", int(position["id"]), patch, db_path=db_path)
        position.update(patch)


def _close_position(position: dict, market: Any, mark: float, minutes: float,
                    reason: str, db_path: str | None) -> None:
    direction = position.get("direction") or "long"
    entry = float(position.get("entry_avg_price") or 0.0)
    size_contracts = float(position.get("size_contracts") or 0.0)
    pnl_pct = _pnl_pct(direction, entry, mark)
    realized = pnl_pct * size_contracts * entry          # notional × pct
    stop_hit_before_1h = 1 if (reason == "stop" and minutes < config.TIME_EXIT_HOURS * 60.0) else 0

    if config.EXECUTOR_LIVE:
        try:
            from . import lighter_exec
            lighter_exec.close_position(position, market, reason, db_path=db_path)
        except Exception as e:
            log.exception("exit_engine: live close failed for %s: %s", position.get("ticker"), e)

    storage.update_row("positions", int(position["id"]), {
        "exit_ts": int(time.time()),
        "exit_reason": reason,
        "realized_pnl_usd": realized,
        "stop_hit_before_1h": stop_hit_before_1h,
    }, db_path=db_path)
    log.info("exit_engine: %s %s CLOSED %s @ %.6f (%.2f%%, $%.2f, %.0fm)%s",
             position.get("ticker"), direction, reason, mark, pnl_pct * 100.0,
             realized, minutes, "" if config.EXECUTOR_LIVE else " [SHADOW]")


def _apply_extend(position: dict, market: Any, fields: dict, db_path: str | None) -> None:
    if config.EXECUTOR_LIVE and fields.get("breakeven_moved"):
        try:
            from . import lighter_exec
            lighter_exec.modify_stop_to_breakeven(position, market, db_path=db_path)
        except Exception as e:
            log.exception("exit_engine: live BE move failed for %s: %s", position.get("ticker"), e)
    storage.update_row("positions", int(position["id"]), fields, db_path=db_path)
    position.update(fields)
    log.info("exit_engine: %s extended (stop→%.6f, trailing=%s)",
             position.get("ticker"), fields.get("stop_price_current", position.get("stop_price_current")),
             position.get("trailing_active"))


# ============================================================================
# the loop
# ============================================================================

async def run_exit_cycle(db_path: str | None = None) -> None:
    positions = storage.open_positions(db_path)
    if not positions:
        return
    now = time.time()
    for position in positions:
        ticker = position.get("ticker")
        try:
            market = await asyncio.to_thread(universe.get_market_snapshot, ticker)
            if market is None or not market.price:
                log.debug("exit_engine: no live mark for %s — skipping", ticker)
                continue
            mark = float(market.price)
            minutes = max(0.0, (now - float(position.get("open_ts") or now)) / 60.0)

            _mark_position(position, mark, minutes, db_path)
            action, fields = evaluate_exit(position, mark, minutes)
            if action in ("stop", "time_exit", "max_hold"):
                _close_position(position, market, mark, minutes, action, db_path)
            elif action == "extend":
                _apply_extend(position, market, fields, db_path)
        except Exception as e:
            log.exception("exit_engine error on %s: %s", ticker, e)

    await asyncio.to_thread(_write_equity_snapshot, db_path)


def _write_equity_snapshot(db_path: str | None = None) -> None:
    try:
        storage.insert_row("equity_snapshots", {
            "ts": int(time.time()),
            "balance_usd": None,
            "free_margin_usd": None,
            "total_exposure_usd": storage.total_open_exposure_usd(db_path),
            "open_position_count": storage.open_position_count(db_path),
            "daily_realized_pnl_usd": storage.daily_realized_pnl_usd(db_path),
            "consecutive_losses": storage.consecutive_losses(db_path),
            "raw_json": None,
        }, db_path=db_path)
    except Exception as e:
        log.warning("exit_engine: equity snapshot failed: %s", e)


async def exit_loop() -> None:
    """Main-loop entry. Cadence = EXIT_POLL_INTERVAL_SEC (60s)."""
    if not config.EXECUTOR_ENABLED:
        return
    log.info("exit_engine: loop started (%ds cadence, live=%s)",
             config.EXIT_POLL_INTERVAL_SEC, config.EXECUTOR_LIVE)
    while _RUNNING:
        try:
            await run_exit_cycle()
        except Exception as e:
            log.exception("exit_engine: cycle blew up: %s", e)
        await asyncio.sleep(config.EXIT_POLL_INTERVAL_SEC)
