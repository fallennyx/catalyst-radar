"""TradePlan tests — deterministic SL/TP computation.

The trade plan is fed (market, history, metadata, direction). Direction is
already validated upstream by the suppression chain, so these tests focus on
the pure level-math: stop placement, TP1 = 1.5R, TP2 = next-swing-or-3R, and
the risk-too-tight short circuit.
"""

from __future__ import annotations

import pytest

from radar import config, trade_plan
from radar.storage import Bar
from radar.universe import Market


def _market(ticker: str = "AMD", asset_class: str = "equity",
            price: float = 100.0) -> Market:
    return Market(
        ticker=ticker,
        asset_class=asset_class,
        market_id=ticker,
        max_leverage=10.0,
        price=price,
        volume_24h_usd=1_000_000,
        oi_usd=100_000,
        funding_1h=0.0,
        pct_24h=2.0,
        pct_1h=1.0,
    )


def _flat_history(close: float = 90.0, n: int = 30) -> list[Bar]:
    """Bars with high/low close to ``close`` — no historical pivots above
    the breakout level. Used for the 3R-fallback test."""
    return [
        Bar(ticker="X", ts=i * 3600, open=close, high=close + 0.1,
            low=close - 0.1, close=close, volume=0, oi=0, funding=0)
        for i in range(n)
    ]


def _history_with_pivot(pivot_high: float, n: int = 30) -> list[Bar]:
    """Flat history with a single elevated pivot bar in the middle."""
    bars: list[Bar] = []
    for i in range(n):
        if i == n // 2:
            bars.append(Bar(ticker="X", ts=i * 3600, open=90.0,
                            high=pivot_high, low=89.0, close=90.0,
                            volume=0, oi=0, funding=0))
        else:
            bars.append(Bar(ticker="X", ts=i * 3600, open=90.0, high=90.5,
                            low=89.5, close=90.0, volume=0, oi=0, funding=0))
    return bars


# ============================================================================
# 1. long plan — entry / stop / targets
# ============================================================================

def test_long_plan_entry_stop_targets():
    market = _market(price=110.0)
    metadata = {"breakout_level": 100.0}

    plan = trade_plan.compute_plan(market, _flat_history(), metadata, "long")

    assert plan is not None
    assert plan.direction == "long"
    assert plan.entry == pytest.approx(110.0)
    # stop sits the configured buffer below the broken swing high
    expected_stop = 100.0 * (1.0 - config.STOP_BUFFER_PCT)
    assert plan.stop == pytest.approx(expected_stop)
    # risk_per_unit = entry - stop
    assert plan.risk_per_unit == pytest.approx(110.0 - expected_stop)
    # TP1 = entry + 1.5R
    assert plan.tp1 == pytest.approx(110.0 + 1.5 * plan.risk_per_unit)
    assert plan.r_multiple_tp1 == pytest.approx(1.5)
    # TP2 falls back to 3R since flat history has no pivot above entry
    assert plan.tp2 == pytest.approx(110.0 + 3.0 * plan.risk_per_unit)
    assert plan.r_multiple_tp2 == pytest.approx(3.0)


# ============================================================================
# 2. short plan mirrors long
# ============================================================================

def test_short_plan_mirrors_long():
    market = _market(price=90.0)
    metadata = {"breakout_level": 100.0}

    plan = trade_plan.compute_plan(market, _flat_history(close=110.0), metadata, "short")

    assert plan is not None
    assert plan.direction == "short"
    assert plan.entry == pytest.approx(90.0)
    expected_stop = 100.0 * (1.0 + config.STOP_BUFFER_PCT)
    assert plan.stop == pytest.approx(expected_stop)
    assert plan.stop > plan.entry  # mirror invariant
    assert plan.risk_per_unit == pytest.approx(expected_stop - 90.0)
    # TP1/TP2 below entry
    assert plan.tp1 < plan.entry
    assert plan.tp2 < plan.entry
    assert plan.tp1 == pytest.approx(90.0 - 1.5 * plan.risk_per_unit)
    # Favorable-direction reward stays positive for a winning short.
    assert plan.r_multiple_tp1 == pytest.approx(1.5)
    assert plan.r_multiple_tp2 == pytest.approx(3.0)


# ============================================================================
# 3. tp2 uses the next prior swing when one sits closer than 3R
# ============================================================================

def test_tp2_uses_next_swing_when_available():
    """A pivot beyond TP1 but inside the 3R window becomes TP2."""
    market = _market(price=100.0)
    metadata = {"breakout_level": 90.0}
    # buffer 0.2% → stop ≈ 89.82, risk ≈ 10.18
    # TP1 = 100 + 1.5 * 10.18 ≈ 115.27, 3R ≈ 130.55
    # Place a pivot at 125.0 — beyond TP1, inside 3R window.
    history = _history_with_pivot(pivot_high=125.0)

    plan = trade_plan.compute_plan(market, history, metadata, "long")

    assert plan is not None
    assert plan.tp2 == pytest.approx(125.0)
    # r_multiple should reflect the actual TP2 distance, not 3R
    expected_r = (125.0 - 100.0) / plan.risk_per_unit
    assert plan.r_multiple_tp2 == pytest.approx(expected_r)
    assert plan.r_multiple_tp2 < 3.0
    assert plan.r_multiple_tp2 > 1.5  # beyond TP1


def test_tp2_uses_next_swing_for_short():
    """Mirror: a pivot beyond short-TP1 (above 3R-down) becomes TP2."""
    market = _market(price=100.0)
    metadata = {"breakout_level": 110.0}
    # buffer 0.2% → stop ≈ 110.22, risk ≈ 10.22
    # TP1 = 100 - 1.5 * 10.22 ≈ 84.67, 3R-down ≈ 69.34
    # Place a low pivot at 75.0 — beyond TP1, inside 3R window.
    bars: list[Bar] = []
    for i in range(30):
        if i == 15:
            bars.append(Bar(ticker="X", ts=i * 3600, open=110.0,
                            high=111.0, low=75.0, close=110.0,
                            volume=0, oi=0, funding=0))
        else:
            bars.append(Bar(ticker="X", ts=i * 3600, open=110.0, high=110.5,
                            low=109.5, close=110.0, volume=0, oi=0, funding=0))

    plan = trade_plan.compute_plan(market, bars, metadata, "short")

    assert plan is not None
    assert plan.tp2 == pytest.approx(75.0)
    assert plan.r_multiple_tp2 < 3.0
    assert plan.r_multiple_tp2 > 1.5  # beyond TP1


# ============================================================================
# 4. tp2 falls back to 3R when no eligible swing exists
# ============================================================================

def test_tp2_falls_back_to_3r_when_no_swing():
    market = _market(price=110.0)
    metadata = {"breakout_level": 100.0}
    # Flat history with all bars below entry — no pivot above.
    history = _flat_history(close=90.0)

    plan = trade_plan.compute_plan(market, history, metadata, "long")

    assert plan is not None
    assert plan.tp2 == pytest.approx(110.0 + 3.0 * plan.risk_per_unit)
    assert plan.r_multiple_tp2 == pytest.approx(3.0)


# ============================================================================
# 5. risk too tight → None
# ============================================================================

def test_risk_too_tight_returns_none(monkeypatch):
    """When the structural break is narrower than MIN_RISK_PCT_OF_ENTRY, no plan."""
    # Bump the threshold so even the buffer-only minimum risk is too tight.
    monkeypatch.setattr(config, "MIN_RISK_PCT_OF_ENTRY", 0.05)  # 5%
    market = _market(price=100.0)
    metadata = {"breakout_level": 100.0}

    plan = trade_plan.compute_plan(market, _flat_history(), metadata, "long")

    assert plan is None


# ============================================================================
# extra: defensive paths
# ============================================================================

def test_returns_none_when_no_breakout_level():
    market = _market(price=100.0)
    plan = trade_plan.compute_plan(market, [], {}, "long")
    assert plan is None


def test_returns_none_for_unsupported_direction():
    market = _market(price=100.0)
    metadata = {"breakout_level": 95.0}
    assert trade_plan.compute_plan(market, [], metadata, "neutral") is None
    assert trade_plan.compute_plan(market, [], metadata, "") is None


def test_short_pivot_below_3r_is_capped():
    """A short with a pivot more than 3R away should still cap at 3R."""
    market = _market(price=100.0)
    metadata = {"breakout_level": 110.0}
    # Pivot far below 3R — the cap should still apply.
    bars: list[Bar] = []
    for i in range(30):
        if i == 15:
            bars.append(Bar(ticker="X", ts=i * 3600, open=110.0,
                            high=111.0, low=10.0, close=110.0,
                            volume=0, oi=0, funding=0))
        else:
            bars.append(Bar(ticker="X", ts=i * 3600, open=110.0, high=110.5,
                            low=109.5, close=110.0, volume=0, oi=0, funding=0))

    plan = trade_plan.compute_plan(market, bars, metadata, "short")
    assert plan is not None
    # Capped at 3R — the deep pivot at 10.0 was further than 3R away.
    assert plan.tp2 == pytest.approx(100.0 - 3.0 * plan.risk_per_unit)
    assert plan.r_multiple_tp2 == pytest.approx(3.0)


def test_tp2_ignores_pivots_inside_tp1():
    """A pivot between entry and TP1 should be skipped (TP1 would fire first).

    Without this guard, TP2 could land less aggressive than TP1 — a
    nonsense ladder.
    """
    market = _market(price=100.0)
    metadata = {"breakout_level": 90.0}
    # TP1 ≈ 115.27. A pivot at 110 is between entry and TP1 — skip it.
    history = _history_with_pivot(pivot_high=110.0)

    plan = trade_plan.compute_plan(market, history, metadata, "long")
    assert plan is not None
    # Should fall back to 3R since the only pivot is inside TP1
    assert plan.tp2 == pytest.approx(100.0 + 3.0 * plan.risk_per_unit)
    assert plan.r_multiple_tp2 == pytest.approx(3.0)


# ============================================================================
# Multi-stage exit ladder (improvement #3) — scale-out, breakeven, ATR trail
# ============================================================================

def _bars_with_volatility(n: int = 30, close: float = 90.0,
                          range_width: float = 0.5) -> list[Bar]:
    """Bars with non-zero range so ATR is computable."""
    return [
        Bar(ticker="X", ts=i * 3600,
            open=close - range_width / 2,
            high=close + range_width / 2,
            low=close - range_width / 2,
            close=close, volume=0, oi=0, funding=0)
        for i in range(n)
    ]


def test_plan_emits_scale_out_fractions():
    market = _market(price=110.0)
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.compute_plan(market, _bars_with_volatility(), metadata, "long")
    assert plan is not None
    assert plan.tp1_fraction == pytest.approx(config.TP1_FRACTION)
    assert plan.tp2_fraction == pytest.approx(config.TP2_FRACTION)
    # Runner = 1 - tp1 - tp2 fractions
    assert plan.runner_fraction == pytest.approx(
        1.0 - config.TP1_FRACTION - config.TP2_FRACTION,
    )


def test_plan_breakeven_trigger_long_is_one_R_above_entry():
    market = _market(price=110.0)
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.compute_plan(market, _bars_with_volatility(), metadata, "long")
    assert plan is not None
    expected = 110.0 + config.BREAKEVEN_TRIGGER_R * plan.risk_per_unit
    assert plan.breakeven_trigger == pytest.approx(expected)


def test_plan_breakeven_trigger_short_is_one_R_below_entry():
    market = _market(price=90.0)
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.compute_plan(market, _bars_with_volatility(close=110.0),
                                   metadata, "short")
    assert plan is not None
    expected = 90.0 - config.BREAKEVEN_TRIGGER_R * plan.risk_per_unit
    assert plan.breakeven_trigger == pytest.approx(expected)


def test_plan_populates_atr_when_history_sufficient():
    market = _market(price=110.0)
    metadata = {"breakout_level": 100.0}
    bars = _bars_with_volatility(n=30, range_width=0.5)
    plan = trade_plan.compute_plan(market, bars, metadata, "long")
    assert plan is not None
    assert plan.trail_atr is not None and plan.trail_atr > 0
    assert plan.trail_atr_mult == pytest.approx(config.TRAIL_ATR_MULT)
    assert plan.has_runner is True


def test_plan_omits_trail_when_history_too_short():
    market = _market(price=110.0)
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.compute_plan(market, _flat_history(n=5), metadata, "long")
    # ATR-14 needs ≥15 bars; with 5 it's None, trail collapses
    assert plan is not None
    assert plan.trail_atr is None
    assert plan.trail_atr_mult == 0.0
    assert plan.has_runner is False
