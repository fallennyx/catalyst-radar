"""Synthetic-data tests for the ranker."""

import numpy as np

from radar import config, ranker
from radar.universe import Market


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
