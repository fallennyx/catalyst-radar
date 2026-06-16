"""Tests for 4h-frame BOS detection (Phase 1 multi-timeframe).
nte
Covers:
  - 4h bar synthesis from UTC-aligned 1h bars
  - 4h swing detection reusing find_swing_high/_low with 4h params
  - Joint condition: 4h structural break + 1h range expansion
"""

from __future__ import annotations

from radar import config, ranker
from radar.storage import Bar
from radar.universe import Market


def _bar(ts: int, high: float, low: float, close: float | None = None,
         open_: float | None = None) -> Bar:
    return Bar(
        ticker="X", ts=ts,
        open=open_ if open_ is not None else (high + low) / 2,
        high=high, low=low,
        close=close if close is not None else (high + low) / 2,
        volume=0.0, oi=0.0, funding=0.0,
    )


# ============================================================================
# 4h synthesis
# ============================================================================

def test_synthesize_4h_bars_groups_by_utc_bucket():
    """Four hourly bars at ts 0/3600/7200/10800 should compress to one 4h bar."""
    bars_1h = [
        _bar(ts=0,    high=10.0, low=8.0, close=9.0, open_=8.5),
        _bar(ts=3600, high=12.0, low=9.0, close=11.0),
        _bar(ts=7200, high=11.5, low=7.5, close=10.0),
        _bar(ts=10800, high=10.5, low=9.5, close=10.2),
    ]
    bars_4h = ranker.synthesize_4h_bars(bars_1h)

    assert len(bars_4h) == 1
    b = bars_4h[0]
    assert b.ts == 0
    assert b.open == 8.5     # first member's open
    assert b.high == 12.0    # bucket max
    assert b.low == 7.5      # bucket min
    assert b.close == 10.2   # last member's close


def test_synthesize_4h_bars_includes_partial_bucket():
    """A 5-hour history produces two 4h bars (the second is the in-progress
    bucket with only one 1h bar inside it)."""
    bars_1h = [_bar(ts=i * 3600, high=10.0, low=9.0) for i in range(5)]
    bars_4h = ranker.synthesize_4h_bars(bars_1h)
    assert len(bars_4h) == 2
    assert bars_4h[0].ts == 0
    assert bars_4h[1].ts == 4 * 3600


def test_synthesize_4h_bars_empty_input():
    assert ranker.synthesize_4h_bars([]) == []


def test_synthesize_4h_bars_aligned_to_utc():
    """A history starting at ts=2*3600 (02:00 UTC) should still bucket
    on 00:00 UTC anchors — the first bucket is [0, 4h) and contains hours 2,3."""
    bars_1h = [
        _bar(ts=2 * 3600, high=10.0, low=9.0),
        _bar(ts=3 * 3600, high=10.5, low=9.5),
    ]
    bars_4h = ranker.synthesize_4h_bars(bars_1h)
    assert len(bars_4h) == 1
    assert bars_4h[0].ts == 0  # bucket starts at 00:00 UTC, not at 02:00


# ============================================================================
# 4h swing detection (find_swing_high/_low reused with 4h params)
# ============================================================================

def test_find_swing_high_on_4h_bars():
    """A clear pivot at 4h-bucket idx 35 with all subsequent buckets lower."""
    # 200 1h bars → 50 4h bars; pivot at hour 141 (bucket 35) — inside LB=20 window.
    bars_1h: list[Bar] = []
    for i in range(200):
        if i == 141:
            bars_1h.append(_bar(ts=i * 3600, high=100.0, low=99.0))
        else:
            bars_1h.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    bars_4h = ranker.synthesize_4h_bars(bars_1h)

    swing = ranker.find_swing_high(
        bars_4h,
        lookback_hours=config.SWING_LOOKBACK_4H_BARS,
        min_age_hours=config.SWING_MIN_AGE_4H_BARS,
        min_bars_validation=config.SWING_MIN_BARS_VALIDATION_4H,
    )
    assert swing is not None
    assert swing.price == 100.0


# ============================================================================
# joint BOS condition (4h structural break + 1h range expansion)
# ============================================================================

def _hist_with_4h_pivot_and_breakout(
    pivot_high: float = 100.0,
    in_progress_high: float = 103.0,
    in_progress_low: float = 98.0,
) -> list[Bar]:
    """200-hour history; 4h pivot at bucket 35; in-progress 1h bar wide-range
    + price above pivot. Bucket 35 sits inside SWING_LOOKBACK_4H_BARS=20 window."""
    bars: list[Bar] = []
    for i in range(200):
        if i == 141:
            bars.append(_bar(ts=i * 3600, high=pivot_high, low=pivot_high - 1.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    bars[-1] = _bar(
        ts=199 * 3600, open_=99.0,
        high=in_progress_high, low=in_progress_low,
        close=in_progress_high,
    )
    return bars


def test_has_breakout_structure_long_break_uses_4h_pivot():
    bars = _hist_with_4h_pivot_and_breakout()
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, direction, level, _ = ranker.has_breakout_structure(
        market, bars, current_price=103.0,
    )
    assert broke is True
    assert direction == "long"
    assert level == 100.0  # the 4h reference


def test_has_breakout_structure_no_break_when_4h_history_too_short(monkeypatch):
    """4h path requires LB(20)+age(1)+validation(1)=22 4h-bars; with 1h path
    disabled, 60 1h-bars (=15 4h-bars) is insufficient."""
    monkeypatch.setattr(config, "BOS_1H_ENABLED", False)
    bars = [_bar(ts=i * 3600, high=92.0, low=90.0) for i in range(60)]
    bars[-1] = _bar(ts=59 * 3600, open_=90.0, high=110.0, low=88.0, close=109.0)
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, _, _, _ = ranker.has_breakout_structure(market, bars, current_price=109.0)
    assert broke is False


def test_has_breakout_structure_short_break_uses_4h_pivot_low():
    """4h pivot LOW at 100 (not high); in-progress 1h bar breaks below."""
    bars: list[Bar] = []
    for i in range(200):
        if i == 141:
            # the pivot bar has a notable LOW (inside LB=20 window)
            bars.append(_bar(ts=i * 3600, high=151.0, low=100.0))
        else:
            bars.append(_bar(ts=i * 3600, high=160.0, low=158.0))
    # in-progress bar: wide range, price below 100
    bars[-1] = _bar(ts=199 * 3600, open_=158.0, high=159.0, low=95.0, close=97.0)
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, direction, level, _ = ranker.has_breakout_structure(
        market, bars, current_price=97.0,
    )
    assert broke is True
    assert direction == "short"
    assert level == 100.0


def test_has_breakout_structure_drops_when_1h_range_doesnt_expand():
    """4h structure broken but 1h confirmation fails → no fire."""
    bars = _hist_with_4h_pivot_and_breakout(
        in_progress_high=103.0, in_progress_low=102.5,  # tiny range 0.5
    )
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, _, _, _ = ranker.has_breakout_structure(market, bars, current_price=103.0)
    assert broke is False


# ============================================================================
# Volume confirmation (improvement #1)
# ============================================================================

def _vol_bar(ts: int, high: float, low: float, close: float, volume: float,
             open_: float | None = None) -> Bar:
    return Bar(
        ticker="X", ts=ts,
        open=open_ if open_ is not None else (high + low) / 2,
        high=high, low=low, close=close,
        volume=volume, oi=0.0, funding=0.0,
    )


def _hist_with_volumes(
    in_progress_high: float,
    in_progress_low: float,
    in_progress_volume: float,
    median_volume: float,
    pivot_high: float = 100.0,
) -> list[Bar]:
    """200h history with consistent volume + a 4h pivot at bucket 35 (inside LB=20)."""
    bars: list[Bar] = []
    for i in range(200):
        if i == 141:
            bars.append(_vol_bar(ts=i * 3600, high=pivot_high, low=pivot_high - 1.0,
                                 close=pivot_high - 0.5, volume=median_volume))
        else:
            bars.append(_vol_bar(ts=i * 3600, high=92.0, low=90.0, close=91.0,
                                 volume=median_volume))
    bars[-1] = _vol_bar(
        ts=199 * 3600, open_=99.0,
        high=in_progress_high, low=in_progress_low,
        close=in_progress_high, volume=in_progress_volume,
    )
    return bars


def test_volume_gate_blocks_breakout_on_dead_volume(monkeypatch):
    """Wide range + price above pivot but volume below the multiplier → drop."""
    monkeypatch.setattr(config, "REQUIRE_VOLUME_CONFIRMATION", True)
    monkeypatch.setattr(config, "VOLUME_EXPANSION_MULTIPLIER", 1.5)
    monkeypatch.setattr(config, "REQUIRE_HTF_TREND_ALIGNMENT", False)
    bars = _hist_with_volumes(
        in_progress_high=103.0, in_progress_low=98.0,
        in_progress_volume=1000.0,    # well below 1.5x median (1500)
        median_volume=1000.0,
    )
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, _, _, _ = ranker.has_breakout_structure(market, bars, current_price=103.0)
    assert broke is False


def test_volume_gate_passes_when_volume_expands(monkeypatch):
    """Same setup but volume crosses the multiplier → BOS fires."""
    monkeypatch.setattr(config, "REQUIRE_VOLUME_CONFIRMATION", True)
    monkeypatch.setattr(config, "VOLUME_EXPANSION_MULTIPLIER", 1.5)
    monkeypatch.setattr(config, "REQUIRE_HTF_TREND_ALIGNMENT", False)
    bars = _hist_with_volumes(
        in_progress_high=103.0, in_progress_low=98.0,
        in_progress_volume=2500.0,   # 2.5x median
        median_volume=1000.0,
    )
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, direction, _, _ = ranker.has_breakout_structure(market, bars, current_price=103.0)
    assert broke is True
    assert direction == "long"


def test_volume_gate_no_block_when_volumes_unavailable(monkeypatch):
    """Sources that emit zero volumes (legacy CoinGecko) should not be blocked."""
    monkeypatch.setattr(config, "REQUIRE_VOLUME_CONFIRMATION", True)
    monkeypatch.setattr(config, "REQUIRE_HTF_TREND_ALIGNMENT", False)
    bars = _hist_with_4h_pivot_and_breakout()  # zero volumes throughout
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, _, _, _ = ranker.has_breakout_structure(market, bars, current_price=103.0)
    assert broke is True


# ============================================================================
# HTF trend alignment (improvement #2)
# ============================================================================

def test_htf_trend_blocks_long_against_downtrend(monkeypatch):
    """Long break with current price below the 7-day median close → reject."""
    monkeypatch.setattr(config, "REQUIRE_VOLUME_CONFIRMATION", False)
    monkeypatch.setattr(config, "REQUIRE_HTF_TREND_ALIGNMENT", True)
    monkeypatch.setattr(config, "HTF_TREND_LOOKBACK_HOURS", 168)
    bars: list[Bar] = []
    # Make most of the recent 7 days close at 200, well above the breakout price (~103).
    # The pivot is overwritten downstream at i=141 (bucket 35, inside LB=20).
    for i in range(200):
        if i == 141:
            bars.append(_vol_bar(ts=i * 3600, high=100.0, low=99.0, close=99.5, volume=0.0))
        elif i >= 200 - 168:
            bars.append(_vol_bar(ts=i * 3600, high=210.0, low=190.0, close=200.0, volume=0.0))
        else:
            bars.append(_vol_bar(ts=i * 3600, high=92.0, low=90.0, close=91.0, volume=0.0))
    bars[-1] = _vol_bar(ts=199 * 3600, open_=99.0,
                        high=103.0, low=98.0, close=103.0, volume=0.0)
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, _, _, _ = ranker.has_breakout_structure(market, bars, current_price=103.0)
    assert broke is False


def test_htf_trend_passes_long_with_uptrend(monkeypatch):
    """Long break with current price above the 7-day median close → pass."""
    monkeypatch.setattr(config, "REQUIRE_VOLUME_CONFIRMATION", False)
    monkeypatch.setattr(config, "REQUIRE_HTF_TREND_ALIGNMENT", True)
    bars = _hist_with_4h_pivot_and_breakout()  # closes ~91, current ~103 → above
    market = Market(ticker="X", asset_class="crypto_t1")
    broke, direction, _, _ = ranker.has_breakout_structure(market, bars, current_price=103.0)
    assert broke is True and direction == "long"


# ============================================================================
# ATR (improvement #3 helper)
# ============================================================================

def test_compute_atr_returns_none_when_history_too_short():
    bars = [_vol_bar(ts=i * 3600, high=10.0, low=9.0, close=9.5, volume=0.0)
            for i in range(5)]
    assert ranker.compute_atr(bars, period=14) is None


def test_compute_atr_basic():
    """20 bars of width-1 ranges (high-low=1, close==prev close) → ATR ≈ 1.0."""
    bars = []
    for i in range(20):
        bars.append(_vol_bar(ts=i * 3600, high=10.0, low=9.0, close=9.5, volume=0.0))
    atr = ranker.compute_atr(bars, period=14)
    assert atr is not None
    # Each TR is max(1.0, |10-prev_close|, |prev_close-9|) = 1.0 in steady state
    assert abs(atr - 1.0) < 1e-6


def test_precompute_references_returns_4h_levels():
    """Watchlist refs come from the 4h frame now."""
    # 200-hour history with 4h-pivot high at bucket 35 (=100.0) and
    # 4h-pivot low at bucket 40 (=85.0). Both inside SWING_LOOKBACK_4H_BARS=20.
    bars: list[Bar] = []
    for i in range(200):
        if i == 141:
            bars.append(_bar(ts=i * 3600, high=100.0, low=99.0))
        elif i == 161:
            bars.append(_bar(ts=i * 3600, high=87.0, low=85.0))
        else:
            bars.append(_bar(ts=i * 3600, high=92.0, low=90.0))
    market = Market(ticker="X", asset_class="crypto_t1")
    sh, sl, _ts, median = ranker.precompute_references_for_watchlist(market, bars)
    assert sh == 100.0
    assert sl == 85.0
    assert median > 0
