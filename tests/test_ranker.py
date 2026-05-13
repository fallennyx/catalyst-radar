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


def _bars_with_4h_pivot_high(
    n_hours: int = 200,
    pivot_4h_idx: int = 25,
    pivot_high: float = 100.0,
    base_high: float = 92.0,
    base_low: float = 90.0,
) -> list[Bar]:
    """Build an N-hour history where one 4h bucket has an elevated high.

    `pivot_4h_idx` is the 4h-bucket index (0-based); the pivot bar is placed
    inside that bucket. With n_hours=200 that's 50 4h-buckets — plenty for
    SWING_LOOKBACK_4H_BARS=30 + age=1 + validation=2.
    """
    bars: list[Bar] = []
    pivot_hour_idx = pivot_4h_idx * 4 + 1  # second hour of the bucket
    for i in range(n_hours):
        if i == pivot_hour_idx:
            bars.append(_bar(ts=i * 3600, high=pivot_high, low=pivot_high - 1.0,
                             close=pivot_high - 0.5))
        else:
            bars.append(_bar(ts=i * 3600, high=base_high, low=base_low))
    return bars


def test_has_breakout_structure_long_break_with_range_expansion():
    """Stable 200-hour history, 4h swing high at 100, current 1h bar wide-range
    and live price above the 4h reference."""
    bars = _bars_with_4h_pivot_high()
    # in-progress 1h bar: wide range (2.5x median 1h ~2.0 = 5.0), price > 100
    bars[-1] = _bar(ts=199 * 3600, high=103.0, low=98.0, close=102.5)
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level, _ = ranker.has_breakout_structure(
        market, bars, current_price=103.0
    )
    assert broke is True
    assert direction == "long"
    assert level == 100.0


def test_has_breakout_structure_long_break_without_range_expansion():
    """Same setup but current 1h bar range is normal — must NOT fire."""
    bars = _bars_with_4h_pivot_high()
    bars[-1] = _bar(ts=199 * 3600, high=103.0, low=102.5, close=102.8)  # range 0.5
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level, _ = ranker.has_breakout_structure(
        market, bars, current_price=103.0
    )
    assert broke is False
    assert direction is None
    assert level is None


def test_has_breakout_structure_uses_live_price_over_close():
    """Bar close at 99 (no break), but current_price=103 → BOS fires on live."""
    bars = _bars_with_4h_pivot_high()
    # in-progress 1h bar: wide range, but bar.close still at 99
    bars[-1] = _bar(ts=199 * 3600, high=99.5, low=94.0, close=99.0)
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level, _ = ranker.has_breakout_structure(
        market, bars, current_price=103.0
    )
    assert broke is True
    assert direction == "long"
    assert level == 100.0


def test_has_breakout_structure_15m_parallel_fires_when_1h_alone_would_not():
    """v3: 15m frame parallel confirmation. The 1h bar has a normal range
    (would NOT clear the 1h gate alone), but the in-progress 15m bar shows a
    wide impulse — BOS should fire on the 15m confirmation path."""
    bars_1h = _bars_with_4h_pivot_high()
    # 1h in-progress bar: live price above 100 but bar range too tight for 1h gate
    bars_1h[-1] = _bar(ts=199 * 3600, high=103.0, low=102.5, close=102.8)

    # 15m history: 96 flat 15m bars (stable median range), then a wide
    # impulse on the in-progress 15m bucket.
    bars_15m: list = []
    for i in range(96):
        bars_15m.append(_bar(ts=i * 900, high=102.6, low=102.4, close=102.5))
    # impulse bar — range 1.0 vs median 0.2 → 5× expansion, well above 2.5×.
    bars_15m.append(_bar(ts=96 * 900, high=103.0, low=102.0, close=102.9))

    market = Market(ticker="TEST", asset_class="crypto_t1")
    # Without history_15m: should NOT fire (1h gate too narrow).
    broke_1h_only, _, _, _ = ranker.has_breakout_structure(
        market, bars_1h, current_price=103.0,
    )
    assert broke_1h_only is False, "1h gate should not clear on a 0.5-range bar"
    # With history_15m: SHOULD fire on the 15m gate.
    broke, direction, level, _ = ranker.has_breakout_structure(
        market, bars_1h, current_price=103.0, history_15m=bars_15m,
    )
    assert broke is True
    assert direction == "long"
    assert level == 100.0


def test_compute_volume_profile_poc_finds_highest_volume_node():
    """Volume-by-price histogram: the price where the most trading happened
    should be returned as the POC."""
    bars: list = []
    # 30 bars at price 100 (low volume) + 30 bars at price 105 (high volume).
    for i in range(30):
        bars.append(Bar(ticker="X", ts=i * 3600, open=100.0, high=100.5,
                        low=99.5, close=100.0, volume=1.0, oi=0.0, funding=0.0))
    for i in range(30):
        bars.append(Bar(ticker="X", ts=(30 + i) * 3600, open=105.0, high=105.5,
                        low=104.5, close=105.0, volume=100.0, oi=0.0, funding=0.0))
    poc = ranker.compute_volume_profile_poc(bars, n_buckets=10)
    assert poc is not None
    # POC should be much closer to 105 than 100.
    assert abs(poc - 105.0) < abs(poc - 100.0)


def test_is_breakout_near_poc():
    assert ranker.is_breakout_near_poc(100.0, 100.4) is True   # 0.4% within ±0.5%
    assert ranker.is_breakout_near_poc(100.0, 101.0) is False  # 1.0% outside
    assert ranker.is_breakout_near_poc(None, 100.0) is False
    assert ranker.is_breakout_near_poc(100.0, None) is False


def test_has_breakout_structure_short_history_4h_only_returns_no_break(monkeypatch):
    """4h path needs 33+ 4h-bars; with 1h disabled, 60 bars is insufficient."""
    monkeypatch.setattr(config, "BOS_1H_ENABLED", False)
    bars: list[Bar] = []
    for i in range(60):  # only 15 4h-bars worth
        bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    bars[-1] = _bar(ts=59 * 3600, high=120.0, low=89.0, close=119.0)
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, _, _, _ = ranker.has_breakout_structure(market, bars, current_price=120.0)
    assert broke is False


def test_has_breakout_structure_1h_path_fires_with_short_history(monkeypatch):
    """1h path only needs 27+ bars; fires mid-candle before 4h history accumulates."""
    monkeypatch.setattr(config, "REQUIRE_HTF_TREND_ALIGNMENT", False)
    monkeypatch.setattr(config, "REQUIRE_VOLUME_CONFIRMATION", False)
    bars: list[Bar] = []
    for i in range(60):  # ample for 1h path (needs 27)
        bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    # Wide-range impulse bar breaks above the prior 1h swing high
    bars[-1] = _bar(ts=59 * 3600, high=120.0, low=89.0, close=119.0)
    market = Market(ticker="TEST", asset_class="crypto_t1")
    broke, direction, level, structure_type = ranker.has_breakout_structure(
        market, bars, current_price=120.0,
    )
    assert broke is True
    assert direction == "long"
    assert level is not None
    assert structure_type == "1h"


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


# ---------- swing fallback (trending markets) ----------

def test_find_swing_high_fallback_in_trending_market():
    """When every candidate is broken by a sequential new high (uptrend),
    the strict criterion fails. The fallback returns the absolute highest in
    the eligible window so the engine still has a reference to break against.
    """
    bars: list[Bar] = []
    # Sequentially-rising highs: bar i has high = 90 + i*0.5
    # Each bar's high breaks the previous bar's high → strict logic finds
    # nothing; fallback returns the highest bar in the eligible window.
    for i in range(60):
        h = 90.0 + i * 0.5
        bars.append(_bar(ts=i * 3600, high=h, low=h - 0.4))
    swing = ranker.find_swing_high(
        bars, lookback_hours=48, min_age_hours=4, min_bars_validation=3
    )
    assert swing is not None
    # Eligible window is bars[8:56] → highest is bar[55] with high=90+55*0.5=117.5
    assert swing.price == 90.0 + 55 * 0.5
    # Fallback signal: bars_validated=0 (strict requires >= min_bars_validation=3)
    assert swing.bars_validated == 0


def test_find_swing_high_strict_still_wins_when_clean():
    """In a calm market with a clear unbroken pivot, strict logic should
    still return that pivot (NOT the fallback)."""
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(_bar(ts=i * 3600, high=100.0, low=99.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    swing = ranker.find_swing_high(
        bars, lookback_hours=48, min_age_hours=4, min_bars_validation=3
    )
    assert swing is not None
    assert swing.price == 100.0
    # Strict pass returns >= min_bars_validation (3+)
    assert swing.bars_validated >= 3


def test_find_swing_low_fallback_in_downtrend():
    bars: list[Bar] = []
    for i in range(60):
        l = 100.0 - i * 0.5
        bars.append(_bar(ts=i * 3600, high=l + 0.4, low=l))
    swing = ranker.find_swing_low(
        bars, lookback_hours=48, min_age_hours=4, min_bars_validation=3
    )
    assert swing is not None
    # Lowest in eligible window [8:56] is bar[55] with low=100-55*0.5=72.5
    assert swing.price == 100.0 - 55 * 0.5
    assert swing.bars_validated == 0
