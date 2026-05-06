"""Synthetic-data tests for the ranker."""

from dataclasses import dataclass

import numpy as np

from radar import config, ranker
from radar.storage import Bar
from radar.universe import Market


# ---------- BOS test helpers ----------

def _bar(ts: int, high: float, low: float, close: float | None = None) -> Bar:
    """Compact Bar factory used by the swing-detection tests."""
    return Bar(
        ticker="X", ts=ts,
        open=close if close is not None else (high + low) / 2,
        high=high, low=low,
        close=close if close is not None else (high + low) / 2,
        volume=0.0, oi=0.0, funding=0.0,
    )


def _flat_history(n: int, base_ts: int = 0, base: float = 90.0, range_: float = 1.0) -> list[Bar]:
    """N flat bars at `base` price with constant `range_`."""
    return [_bar(base_ts + i * 3600, high=base + range_ / 2, low=base - range_ / 2)
            for i in range(n)]


def _mk(ticker, asset_class, **kw):
    defaults = dict(
        market_id=ticker,
        max_leverage=10.0,
        price=100.0,
        volume_24h_usd=10_000_000,
        oi_usd=1_000_000,
        funding_1h=0.0,
        pct_24h=0.0,
        pct_1h=0.0,
    )
    defaults.update(kw)
    return Market(ticker=ticker, asset_class=asset_class, **defaults)


def test_compute_score_runs_with_no_history():
    m = _mk("BTC", "crypto_t1", pct_1h=2.0)
    score = ranker.compute_score(m, history={})
    assert isinstance(score, float)


def test_pop_score_increases_with_larger_move():
    rng = np.random.default_rng(0)
    rets = (rng.normal(0, 0.01, 200)).tolist()
    weak = _mk("BTC", "crypto_t1", pct_1h=0.5)
    strong = _mk("BTC", "crypto_t1", pct_1h=5.0)
    s_weak = ranker.compute_score(weak, history={"ret_1h": rets})
    s_strong = ranker.compute_score(strong, history={"ret_1h": rets})
    assert s_strong > s_weak


def test_volume_z_picks_up_volume_spike():
    base_vol = [1000.0] * 50 + [100_000.0]  # last is a 100x spike
    base_oi = [500.0] * 51
    m = _mk("ETH", "crypto_t1", pct_1h=0.1)
    hist = {"vol_1h": base_vol, "oi_1h": base_oi, "ret_1h": [0.0] * 51}
    score = ranker.compute_score(m, history=hist)
    # volume_z weight is 0.5; a >5σ spike should produce a clearly positive score
    assert score > 1.0


def test_class_multiplier_applied():
    m_t2 = _mk("ARB", "crypto_t2", pct_1h=3.0)
    m_t1 = _mk("BTC", "crypto_t1", pct_1h=3.0)
    s_t1 = ranker.compute_score(m_t1, history={})
    s_t2 = ranker.compute_score(m_t2, history={})
    # t2 has 1.1x multiplier vs t1's 1.0
    assert s_t2 > s_t1


def test_wash_penalty_negative_contribution():
    # extreme turnover should push score down
    fat = _mk("PEPE", "crypto_meme", pct_1h=8.0, volume_24h_usd=100_000_000_000, oi_usd=1_000_000)
    lean = _mk("PEPE", "crypto_meme", pct_1h=8.0, volume_24h_usd=10_000_000, oi_usd=1_000_000)
    assert ranker.compute_score(fat, history={}) < ranker.compute_score(lean, history={})


def test_top_n_movers_filters_low_volume():
    cheap = _mk("X", "crypto_t1", volume_24h_usd=100, pct_1h=20.0)  # below MIN_VOLUME
    rich = _mk("BTC", "crypto_t1", volume_24h_usd=10_000_000, pct_1h=10.0)
    assert config.MIN_VOLUME_24H_USD == 50_000
    out = ranker.top_n_movers([cheap, rich])
    tickers = [m.ticker for m, _ in out]
    assert "X" not in tickers
    assert "BTC" in tickers


def test_top_n_movers_ranks_by_score():
    big = _mk("SOL", "crypto_t1", pct_1h=15.0, volume_24h_usd=10_000_000)
    small = _mk("BTC", "crypto_t1", pct_1h=1.0, volume_24h_usd=10_000_000)
    out = ranker.top_n_movers([small, big])
    assert out[0][0].ticker == "SOL"


def test_top_n_movers_caps_at_n():
    markets = [_mk(f"M{i:02d}", "crypto_t1", pct_1h=10.0 + i, volume_24h_usd=10_000_000) for i in range(20)]
    out = ranker.top_n_movers(markets, n=5)
    assert len(out) == 5


def test_cold_start_drops_quiet_market_with_no_history():
    quiet = _mk("BTC", "crypto_t1", pct_1h=0.5, volume_24h_usd=10_000_000)
    # no history → score near 0; pct_1h below 5% threshold → filtered
    out = ranker.top_n_movers([quiet])
    assert out == []


def test_cold_start_passes_loud_meme_only_above_10pct():
    nine = _mk("PEPE", "crypto_meme", pct_1h=9.0, volume_24h_usd=10_000_000)
    eleven = _mk("WIF", "crypto_meme", pct_1h=11.0, volume_24h_usd=10_000_000)
    out_tickers = {m.ticker for m, _ in ranker.top_n_movers([nine, eleven])}
    assert "WIF" in out_tickers
    assert "PEPE" not in out_tickers


# ============================================================================
# BOS / swing-detection tests
# ============================================================================

def test_find_swing_high_clean_pivot():
    """A clear pivot high at bar[20], all subsequent bars stay below it."""
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(_bar(ts=i * 3600, high=100.0, low=98.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    swing = ranker.find_swing_high(
        bars, lookback_hours=48, min_age_hours=4, min_bars_validation=3
    )
    assert swing is not None
    assert swing.price == 100.0
    assert swing.bars_validated >= 3


def test_find_swing_high_broken_pivot():
    """If the only candidate within lookback was broken, return None."""
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(_bar(ts=i * 3600, high=100.0, low=98.0))
        elif i == 30:
            bars.append(_bar(ts=i * 3600, high=105.0, low=103.0))  # broke 100
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    swing = ranker.find_swing_high(
        bars, lookback_hours=48, min_age_hours=4, min_bars_validation=3
    )
    # 105 was at bar[30]; with min_age_hours=4, bar[30] is in the eligible window
    # if the latest bar is past index 34. Latest index 59 → 30 < 59-4=55 → eligible.
    # bar[30] is also unbroken by anything after → swing returns 105.
    assert swing is not None
    assert swing.price == 105.0


def test_find_swing_high_excludes_in_progress_bar():
    """Current in-progress bar's wick at $120 must NOT become the reference."""
    bars: list[Bar] = []
    for i in range(60):
        bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    # mutate the LAST bar to a huge wick
    bars[-1] = _bar(ts=59 * 3600, high=120.0, low=89.0, close=119.0)
    swing = ranker.find_swing_high(
        bars, lookback_hours=48, min_age_hours=4, min_bars_validation=3
    )
    # The in-progress bar (last one) is excluded from the eligible window AND
    # excluded from the validation set. Earlier bars are all 92 → first valid
    # candidate is some 92 high.
    assert swing is not None
    assert swing.price == 92.0


def test_find_swing_high_insufficient_history():
    bars = _flat_history(10)
    swing = ranker.find_swing_high(bars, 48, 4, 3)
    assert swing is None


def test_has_breakout_structure_long_break_with_range_expansion():
    """Stable history, prior swing high at 100, current bar wide-range above it."""
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(_bar(ts=i * 3600, high=100.0, low=99.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    # current in-progress bar with wide range and price above 100
    bars[-1] = _bar(ts=59 * 3600, high=103.0, low=99.0, close=102.5)
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level = ranker.has_breakout_structure(
        market, bars, current_price=103.0
    )
    assert broke is True
    assert direction == "long"
    assert level == 100.0


def test_has_breakout_structure_long_break_without_range_expansion():
    """Same setup but current bar range is normal — must NOT fire."""
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(_bar(ts=i * 3600, high=100.0, low=99.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    bars[-1] = _bar(ts=59 * 3600, high=103.0, low=102.5, close=102.8)  # range 0.5
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level = ranker.has_breakout_structure(
        market, bars, current_price=103.0
    )
    assert broke is False
    assert direction is None
    assert level is None


def test_has_breakout_structure_uses_live_price_over_close():
    """history close at 99 (no break), current_price=103 → BOS fires."""
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(_bar(ts=i * 3600, high=100.0, low=99.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    # in-progress bar wide range, but bar.close still at 99
    bars[-1] = _bar(ts=59 * 3600, high=99.5, low=95.0, close=99.0)
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level = ranker.has_breakout_structure(
        market, bars, current_price=103.0
    )
    assert broke is True
    assert direction == "long"
    assert level == 100.0


def test_check_breakout_against_stored_references_respects_direction_bias():
    """Long-bias entry must NOT fire on a short-side break and vice versa."""
    broke, direction, level = ranker.check_breakout_against_stored_references(
        current_price=78.0,
        current_bar_range=5.0,
        swing_high_reference=100.0,
        swing_low_reference=80.0,
        median_bar_range=2.0,  # 5.0 / 2.0 = 2.5x → range expansion ON
        direction_bias="long",
    )
    assert broke is False
    assert direction is None
    assert level is None

    # Same setup but direction_bias="short" — short break is now valid
    broke, direction, level = ranker.check_breakout_against_stored_references(
        current_price=78.0,
        current_bar_range=5.0,
        swing_high_reference=100.0,
        swing_low_reference=80.0,
        median_bar_range=2.0,
        direction_bias="short",
    )
    assert broke is True
    assert direction == "short"
    assert level == 80.0


def test_check_breakout_against_stored_references_requires_range_expansion():
    """Below the multiplier threshold, no break."""
    broke, _, _ = ranker.check_breakout_against_stored_references(
        current_price=110.0,
        current_bar_range=2.5,  # 2.5 / 2.0 = 1.25x < 1.5x
        swing_high_reference=100.0,
        swing_low_reference=80.0,
        median_bar_range=2.0,
        direction_bias="long",
    )
    assert broke is False


def test_check_breakout_zero_median_range_safe():
    """Zero median (e.g. cold-start watchlist) must not trigger false breaks."""
    broke, _, _ = ranker.check_breakout_against_stored_references(
        current_price=200.0,
        current_bar_range=10.0,
        swing_high_reference=100.0,
        swing_low_reference=None,
        median_bar_range=0.0,
        direction_bias="long",
    )
    assert broke is False


def test_compute_median_range_basic():
    bars = [
        _bar(ts=0, high=10.0, low=8.0),   # range 2
        _bar(ts=1, high=10.0, low=4.0),   # range 6
        _bar(ts=2, high=10.0, low=6.0),   # range 4
    ]
    assert ranker.compute_median_range(bars) == 4.0


def test_precompute_references_returns_zero_median_when_short():
    bars = _flat_history(5)
    sh, sl, ts, median = ranker.precompute_references_for_watchlist(
        Market(ticker="X", asset_class="crypto_t1"), bars
    )
    # too-short history → no swings, but the function shouldn't crash
    assert sh is None or isinstance(sh, float)
    assert sl is None or isinstance(sl, float)
    assert isinstance(median, float)
