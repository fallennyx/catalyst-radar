"""End-to-end tests for the replay harness."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from radar import config, replay, storage
from radar.suppression import Alert


REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_BARS = REPO_ROOT / "data" / "sample_bars.csv"
SAMPLE_NEWS = REPO_ROOT / "data" / "sample_news_archive.json"


# ---------- loaders ----------

def test_load_bars_parses_iso_timestamps():
    bars = replay.load_bars(SAMPLE_BARS)
    assert len(bars) >= 10
    # timestamps must be hourly-floored unix integers
    for ts in bars.keys():
        assert isinstance(ts, int)
        assert ts % 3600 == 0


def test_load_bars_groups_markets_by_ts():
    bars = replay.load_bars(SAMPLE_BARS)
    first_ts = sorted(bars.keys())[0]
    tickers = {m.ticker for m in bars[first_ts]}
    assert "BTC" in tickers
    assert "ARB" in tickers


def test_load_bars_rejects_bad_columns(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("ts,foo\n1700000000,x\n")
    with pytest.raises(ValueError, match="missing columns"):
        replay.load_bars(bad)


def test_load_news_archive_parses_iso_published():
    archive = replay.load_news_archive(SAMPLE_NEWS)
    assert "ARB" in archive
    assert "BTC" in archive
    arb_items = archive["ARB"]
    # sorted ascending by published
    assert arb_items[0].published <= arb_items[-1].published
    # published times are unix ints, not strings
    assert all(isinstance(n.published, int) for n in arb_items)


# ---------- replay fetcher ----------

def test_replay_fetcher_filters_by_lookback():
    archive = replay.load_news_archive(SAMPLE_NEWS)
    arb_first_pub = sorted(n.published for n in archive["ARB"])[0]
    fetch = replay.make_replay_fetcher(archive, lambda: arb_first_pub - 3600)
    # ask for window ending an hour BEFORE the first ARB news → empty
    from radar.universe import Market
    arb = Market(ticker="ARB", asset_class="crypto_t2")
    assert fetch(arb, lookback_hours=24) == []


def test_replay_fetcher_returns_items_within_window():
    archive = replay.load_news_archive(SAMPLE_NEWS)
    latest_arb = max(n.published for n in archive["ARB"])
    fetch = replay.make_replay_fetcher(archive, lambda: latest_arb)
    from radar.universe import Market
    arb = Market(ticker="ARB", asset_class="crypto_t2")
    items = fetch(arb, lookback_hours=24)
    assert len(items) >= 1
    assert all(item.ticker == "ARB" for item in items)


# ---------- end-to-end ----------

def test_replay_runs_end_to_end_on_sample(tmp_path):
    db = tmp_path / "replay.db"
    counts = replay.replay(
        bars_csv=SAMPLE_BARS,
        news_json=SAMPLE_NEWS,
        db_path=str(db),
        classify=False,
    )
    assert counts["cycles"] >= 10
    assert counts["emitted"] + counts["dropped"] >= 1
    # the ARB pump and BTC ETF spikes both clear the cold-start threshold
    assert counts["emitted"] >= 1
    # replay DB exists and has alert rows
    rows = storage.execute("SELECT ticker, decision FROM alerts", db_path=str(db))
    assert len(rows) >= 1


def test_replay_uses_virtual_clock(tmp_path):
    """Inside the replay loop, storage._now() must return historical timestamps."""
    captured = []

    def emit(alert: Alert, classification):
        captured.append((alert.ticker, storage._now()))

    replay.replay(
        bars_csv=SAMPLE_BARS,
        news_json=SAMPLE_NEWS,
        db_path=str(tmp_path / "vc.db"),
        classify=False,
        emit_fn=emit,
    )
    assert captured, "expected at least one emitted alert"
    # all captured timestamps should fall on the sample-data day, not 'today'
    sample_day_start = 1_705_276_800  # 2024-01-15 00:00:00 UTC
    sample_day_end = sample_day_start + 86400
    for ticker, ts in captured:
        assert sample_day_start <= ts < sample_day_end, (ticker, ts)


def test_replay_restores_clock_after_run(tmp_path):
    import time
    pre = storage._now()
    replay.replay(
        bars_csv=SAMPLE_BARS,
        db_path=str(tmp_path / "r.db"),
        classify=False,
    )
    post = storage._now()
    # both should be wall-clock seconds, very close to time.time()
    assert abs(post - int(time.time())) < 5
    assert abs(pre - int(time.time())) < 5


def test_replay_does_not_pollute_production_db_path(tmp_path):
    """config.DB_PATH must be restored after replay finishes."""
    sentinel = "data/_production_sentinel.db"
    config.DB_PATH = sentinel
    try:
        replay.replay(
            bars_csv=SAMPLE_BARS,
            db_path=str(tmp_path / "isolated.db"),
            classify=False,
        )
        assert config.DB_PATH == sentinel
    finally:
        config.DB_PATH = "data/radar.db"


def test_replay_handles_empty_news(tmp_path):
    counts = replay.replay(
        bars_csv=SAMPLE_BARS,
        news_json=None,
        db_path=str(tmp_path / "no_news.db"),
        classify=False,
    )
    assert counts["cycles"] >= 10


# ---------- summary ----------

def test_summarize_returns_per_ticker_emit_counts(tmp_path):
    db = tmp_path / "summary.db"
    replay.replay(
        bars_csv=SAMPLE_BARS,
        news_json=SAMPLE_NEWS,
        db_path=str(db),
        classify=False,
    )
    summary = replay.summarize(str(db))
    tickers = {row["ticker"] for row in summary["emitted_by_ticker"]}
    # Both pumps in the sample data should appear
    assert "ARB" in tickers
    assert "BTC" in tickers


def test_summarize_groups_drop_reasons_by_rule(tmp_path):
    """Force drops by exhausting the daily budget, then check rule grouping."""
    db = tmp_path / "drops.db"
    storage.init_db(str(db))
    storage.set_clock(lambda: 1_705_276_800)
    try:
        # Seed enough EMITs to trip the budget for any subsequent alert.
        for i in range(config.DAILY_ALERT_BUDGET + 3):
            seed = Alert(
                ticker=f"X{i:02d}",
                asset_class="equity",
                score=1.0,
                alpha_z=float("inf"),
                r_alpha_pct=10.0,
            )
            decision = "EMIT" if i < config.DAILY_ALERT_BUDGET else "DROP"
            reason = "ok" if decision == "EMIT" else "budget_throttle: too many"
            storage.record_alert(seed, decision=decision, reason=reason, db_path=str(db))
    finally:
        storage.set_clock(None)

    summary = replay.summarize(str(db))
    rules = [row["rule"] for row in summary["drops_by_rule"]]
    assert "budget_throttle" in rules
