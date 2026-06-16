"""Executor decision/sizing/breaker + exit-engine logic + data capture."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from radar import config, exit_engine, executor, storage


# ---------- builders ----------

def _market(asset_class="crypto_t2", price=100.0):
    return SimpleNamespace(
        ticker="ZZZ", asset_class=asset_class, market_id="5",
        price=price, max_leverage=10.0,
    )


def _plan(entry=100.0, stop=98.0, tp1=103.0, tp2=106.0):
    return SimpleNamespace(
        entry=entry, stop=stop, tp1=tp1, tp2=tp2,
        risk_per_unit=abs(entry - stop),
    )


def _adj(direction="long"):
    return SimpleNamespace(
        direction=direction, conviction_tier="STRONG", flipped=False,
        predictor_result=None, signal_bundle={"volume_ratio": "2.0"},
    )


# ============ §2 tiering ============

def test_tier_a():
    d = executor.classify_tier(alpha_z=3.5, score_pctile=80, cluster_size=0,
                               btc_ret_4h=0, vol_ratio=2, asset_class="crypto_t2")
    assert d.tier == "A" and d.tradeable is True


def test_tier_b_not_tradeable_v1():
    d = executor.classify_tier(alpha_z=1.0, score_pctile=80, cluster_size=0,
                               btc_ret_4h=0, vol_ratio=2, asset_class="crypto_t2")
    assert d.tier == "B" and d.tradeable is False


def test_tier_cpop_cluster():
    d = executor.classify_tier(alpha_z=1.0, score_pctile=40, cluster_size=5,
                               btc_ret_4h=0, vol_ratio=2, asset_class="crypto_t2")
    assert d.tier == "C_pop"


def test_skip_inert_class_overrides_everything():
    d = executor.classify_tier(alpha_z=5.0, score_pctile=99, cluster_size=9,
                               btc_ret_4h=5, vol_ratio=1, asset_class="crypto_t1")
    assert d.tier == "SKIP" and d.reason == "inert_class"


def test_skip_blowoff():
    d = executor.classify_tier(alpha_z=2.0, score_pctile=90, cluster_size=0,
                               btc_ret_4h=0, vol_ratio=20, asset_class="crypto_t2")
    assert d.tier == "SKIP" and d.reason == "blowoff"


def test_skip_alpha_z_trap_beats_tier_b():
    # |alpha_z| in [2,3) AND pctile>=50 → the 16.7%-WR trap, not Tier B.
    d = executor.classify_tier(alpha_z=2.5, score_pctile=80, cluster_size=0,
                               btc_ret_4h=0, vol_ratio=2, asset_class="crypto_t2")
    assert d.tier == "SKIP" and d.reason == "alpha_z_trap"


def test_skip_below_all():
    d = executor.classify_tier(alpha_z=1.0, score_pctile=40, cluster_size=0,
                               btc_ret_4h=0, vol_ratio=2, asset_class="crypto_t2")
    assert d.tier == "SKIP" and d.reason == "below_all_tiers"


# ============ §3 sizing ============

def test_sizing_fixes_dollar_loss_at_stop():
    plan = _plan(entry=100.0, stop=98.0)            # 2% stop
    s = executor.compute_sizing(plan=plan, tier="A", score_pctile=75, max_leverage=10)
    # loss at stop = contracts × |entry-stop| must equal MAX_LOSS_PER_TRADE_USD
    loss = s.contracts * plan.risk_per_unit
    assert loss == pytest.approx(config.MAX_LOSS_PER_TRADE_USD, rel=1e-9)


def test_sizing_independent_of_stop_width():
    tight = executor.compute_sizing(plan=_plan(100.0, 99.0), tier="A",
                                    score_pctile=75, max_leverage=10)
    wide = executor.compute_sizing(plan=_plan(100.0, 90.0), tier="A",
                                   score_pctile=75, max_leverage=10)
    tight_loss = tight.contracts * 1.0
    wide_loss = wide.contracts * 10.0
    assert tight_loss == pytest.approx(wide_loss) == pytest.approx(config.MAX_LOSS_PER_TRADE_USD)


def test_sizing_score_mult_clamped():
    hi = executor.compute_sizing(plan=_plan(), tier="A", score_pctile=200, max_leverage=10)
    lo = executor.compute_sizing(plan=_plan(), tier="A", score_pctile=10, max_leverage=10)
    assert hi.score_mult == config.SCORE_SIZE_MULT_MAX
    assert lo.score_mult == 1.0


def test_sizing_tier_mult_applied():
    a = executor.compute_sizing(plan=_plan(), tier="A", score_pctile=75, max_leverage=10)
    b = executor.compute_sizing(plan=_plan(), tier="B", score_pctile=75, max_leverage=10)
    assert b.contracts == pytest.approx(a.contracts * config.TIER_SIZE_MULT["B"])


def test_sizing_score_mult_only_tier_a():
    b = executor.compute_sizing(plan=_plan(), tier="B", score_pctile=200, max_leverage=10)
    assert b.score_mult == 1.0


def test_sizing_bad_plan_returns_none():
    assert executor.compute_sizing(plan=_plan(0.0, 0.0), tier="A",
                                   score_pctile=75, max_leverage=10) is None


# ============ §6 circuit breaker ============

def _insert_closed_position(pnl, exit_ts, db_path):
    storage.insert_row("positions", {
        "ticker": "ZZZ", "direction": "long", "open_ts": exit_ts - 3600,
        "entry_avg_price": 100.0, "size_contracts": 1.0, "conviction_tier": "A",
        "exit_ts": exit_ts, "exit_reason": "stop", "realized_pnl_usd": pnl,
    }, db_path=db_path)


def test_breaker_clean(tmp_db):
    assert executor.breaker_status(tmp_db).halted is False


def test_breaker_daily_loss(tmp_db):
    now = storage._now()
    _insert_closed_position(-60.0, now, tmp_db)
    s = executor.breaker_status(tmp_db)
    assert s.halted and s.reason == "daily_max_loss"


def test_breaker_consecutive_losses(tmp_db):
    now = storage._now()
    for i in range(config.CONSECUTIVE_LOSS_HALT):
        _insert_closed_position(-1.0, now - (10 - i), tmp_db)
    s = executor.breaker_status(tmp_db)
    assert s.halted and s.reason == "consecutive_losses"


def test_breaker_consecutive_resets_on_win(tmp_db):
    now = storage._now()
    _insert_closed_position(-1.0, now - 5, tmp_db)
    _insert_closed_position(+1.0, now - 4, tmp_db)   # most recent is a win
    _insert_closed_position(-1.0, now - 3, tmp_db)
    assert storage.consecutive_losses(tmp_db) == 1


def test_breaker_kill_switch(tmp_db, monkeypatch, tmp_path):
    kf = tmp_path / "halt"
    kf.write_text("x")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(kf))
    assert executor.breaker_status(tmp_db).reason == "kill_switch"


# ============ config versioning ============

def test_config_version_is_stable_then_changes(tmp_db, monkeypatch):
    a = executor.current_config_version_id(tmp_db)
    b = executor.current_config_version_id(tmp_db)
    assert a == b                                   # same config → same row
    monkeypatch.setattr(config, "MAX_LOSS_PER_TRADE_USD", 7.5)
    c = executor.current_config_version_id(tmp_db)
    assert c != a                                   # changed config → new row


# ============ maybe_execute shadow-mode capture ============

def test_shadow_mode_captures_and_opens_simulated_position(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "EXECUTOR_LIVE", False)
    metadata = {
        "breakout_level": 99.0, "structure_type": "4h",
        "swing_high_reference": 99.0, "median_bar_range": 0.5,
        "asset_class": "crypto_t2", "score": 80.0, "score_pctile": 90.0,
        "alpha_z": 3.5, "r_alpha_pct": 5.0, "cluster_size": 0,
        "btc_ret_4h": 0.0, "vol_ratio": 2.0, "classifier_result": None,
    }
    # wide stop keeps size_usd under the $200 exposure cap
    res = executor.maybe_execute(market=_market(), plan=_plan(100.0, 95.0),
                                 metadata=metadata, adjudicated=_adj(), tier=1)
    assert res["shadow"] is True and res["tier"] == "A"
    assert storage.open_position_count(tmp_db) == 1
    snaps = storage.execute("SELECT * FROM signal_snapshots")
    assert len(snaps) == 1 and snaps[0]["tier_decision"] == "A"
    execs = storage.execute("SELECT * FROM executions")
    assert len(execs) == 1 and execs[0]["skip_reason"] == "shadow_mode"


def test_skip_tier_records_execution_no_position(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "EXECUTOR_LIVE", False)
    metadata = {
        "breakout_level": 99.0, "asset_class": "crypto_t1",   # inert → SKIP
        "score": 80.0, "score_pctile": 90.0, "alpha_z": 5.0,
        "cluster_size": 0, "btc_ret_4h": 0.0, "vol_ratio": 2.0,
        "classifier_result": None,
    }
    res = executor.maybe_execute(market=_market(asset_class="crypto_t1"),
                                 plan=_plan(), metadata=metadata,
                                 adjudicated=_adj(), tier=1)
    assert res["acted"] is False
    assert storage.open_position_count(tmp_db) == 0
    execs = storage.execute("SELECT * FROM executions")
    assert execs[0]["skip_reason"].startswith("tier_skip")


def test_disabled_executor_is_noop(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_ENABLED", False)
    res = executor.maybe_execute(market=_market(), plan=_plan(), metadata={},
                                 adjudicated=_adj(), tier=1)
    assert res is None
    assert storage.execute("SELECT * FROM executions") == []


def test_max_concurrent_blocks_new_entry(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "EXECUTOR_LIVE", False)
    monkeypatch.setattr(config, "MAX_CONCURRENT_POSITIONS", 1)
    for _ in range(1):
        storage.insert_row("positions", {
            "ticker": "AAA", "direction": "long", "open_ts": storage._now(),
            "entry_avg_price": 1.0, "size_contracts": 1.0, "conviction_tier": "A",
        }, db_path=tmp_db)
    metadata = {"asset_class": "crypto_t2", "score_pctile": 90.0, "alpha_z": 3.5,
                "cluster_size": 0, "btc_ret_4h": 0.0, "vol_ratio": 2.0,
                "classifier_result": None, "breakout_level": 99.0}
    res = executor.maybe_execute(market=_market(), plan=_plan(), metadata=metadata,
                                 adjudicated=_adj(), tier=1)
    assert res["skip_reason"] == "max_concurrent"


# ============ §5 exit-engine evaluation ============

def _pos(direction="long", entry=100.0, stop=98.0, tier="A", risk=2.0,
         blowoff=0, trailing=0):
    return {
        "id": 1, "ticker": "ZZZ", "direction": direction, "entry_avg_price": entry,
        "stop_price_current": stop, "conviction_tier": tier, "blowoff_flag": blowoff,
        "trailing_active": trailing,
        "raw_json": json.dumps({"risk_per_unit": risk}),
    }


def test_exit_stop_hit():
    action, _ = exit_engine.evaluate_exit(_pos(stop=98.0), mark=97.0, minutes=20)
    assert action == "stop"


def test_exit_hold_before_1h():
    action, _ = exit_engine.evaluate_exit(_pos(stop=90.0), mark=101.0, minutes=30)
    assert action is None


def test_exit_tier_b_time_exit():
    action, _ = exit_engine.evaluate_exit(_pos(tier="B", stop=90.0), mark=105.0, minutes=65)
    assert action == "time_exit"


def test_exit_tier_a_extends_when_working():
    action, fields = exit_engine.evaluate_exit(
        _pos(tier="A", stop=90.0, risk=2.0), mark=105.0, minutes=65)
    assert action == "extend"
    assert fields["stop_price_current"] == 100.0 and fields["trailing_active"] == 1


def test_exit_tier_a_cuts_when_flat():
    action, _ = exit_engine.evaluate_exit(
        _pos(tier="A", stop=90.0, risk=2.0), mark=100.5, minutes=65)
    assert action == "time_exit"


def test_exit_blowoff_forces_close_even_if_working():
    action, _ = exit_engine.evaluate_exit(
        _pos(tier="A", stop=90.0, risk=2.0, blowoff=1), mark=110.0, minutes=65)
    assert action == "time_exit"


def test_exit_max_hold():
    action, _ = exit_engine.evaluate_exit(_pos(tier="A", stop=90.0), mark=101.0, minutes=250)
    assert action == "max_hold"


def test_exit_trailing_ratchets_stop_up():
    pos = _pos(tier="A", stop=100.0, risk=2.0, trailing=1)
    action, fields = exit_engine.evaluate_exit(pos, mark=110.0, minutes=120)
    assert action == "extend" and fields["stop_price_current"] == 108.0  # mark - 1R
