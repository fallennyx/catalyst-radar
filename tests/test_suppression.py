"""Cover all four suppression rules."""

import time

import pytest

from radar import config, storage, suppression
from radar.suppression import Alert


@pytest.fixture
def alert_btc():
    return Alert(
        ticker="BTC",
        asset_class="crypto_t1",
        score=5.0,
        alpha_z=3.0,
        r_alpha_pct=4.0,
    )


@pytest.fixture
def alert_equity():
    return Alert(
        ticker="NVDA",
        asset_class="equity",
        score=4.0,
        alpha_z=float("inf"),
        r_alpha_pct=6.5,
    )


def test_emit_when_no_suppression_triggers(tmp_db, alert_btc):
    decision, reason = suppression.evaluate(alert_btc)
    assert decision == "EMIT"
    assert reason == "ok"


def test_dedup_4h_blocks_repeat_emit(tmp_db, alert_btc):
    storage.record_alert(alert_btc, decision="EMIT", reason="ok")
    decision, reason = suppression.evaluate(alert_btc)
    assert decision == "DROP"
    assert "dedup_4h" in reason


def test_dedup_4h_does_not_block_after_window(tmp_db, alert_btc, monkeypatch):
    # Insert an "old" alert by manipulating created_at directly.
    storage.record_alert(alert_btc, decision="EMIT", reason="ok")
    long_ago = int(time.time()) - (config.DEDUP_HOURS * 3600 + 60)
    storage.execute(
        "UPDATE alerts SET created_at = ? WHERE ticker = ?",
        (long_ago, "BTC"),
    )
    decision, _ = suppression.evaluate(alert_btc)
    assert decision == "EMIT"


def test_btc_beta_drops_low_alpha_crypto(tmp_db):
    weak = Alert(ticker="ETH", asset_class="crypto_t1", score=4.0, alpha_z=0.5, r_alpha_pct=1.0)
    decision, reason = suppression.evaluate(weak)
    assert decision == "DROP"
    assert "btc_beta" in reason


def test_btc_beta_passes_high_alpha_crypto(tmp_db):
    strong = Alert(ticker="ETH", asset_class="crypto_t1", score=4.0, alpha_z=3.5, r_alpha_pct=5.0)
    decision, _ = suppression.evaluate(strong)
    assert decision == "EMIT"


def test_btc_beta_skipped_for_non_crypto(tmp_db, alert_equity):
    # alpha_z=inf, but the rule should still skip non-crypto entirely
    decision, _ = suppression.evaluate(alert_equity)
    assert decision == "EMIT"


def test_sector_day_throttle(tmp_db):
    # Push the sector to its threshold using distinct tickers so dedup_4h
    # does not pre-empt the sector_day rule.
    for i in range(config.SECTOR_DAY_THRESHOLD):
        seed = Alert(
            ticker=f"FILL{i:02d}",
            asset_class="crypto_t1",
            score=2.0,
            alpha_z=3.0,
            r_alpha_pct=5.0,
        )
        storage.record_alert(seed, decision="EMIT", reason="ok")

    fresh = Alert(ticker="UNIQ", asset_class="crypto_t1", score=4.0, alpha_z=3.0, r_alpha_pct=5.0)
    decision, reason = suppression.evaluate(fresh)
    assert decision == "DROP"
    assert "sector_day" in reason


def test_budget_throttle(tmp_db, monkeypatch):
    # Lower the per-class threshold so we don't trip sector_day first.
    monkeypatch.setattr(config, "SECTOR_DAY_THRESHOLD", 999)
    # Spread emits across asset classes to avoid sector_day, hit budget instead.
    classes = ["crypto_t1", "crypto_t2", "equity", "commodity", "crypto_meme"]
    for i in range(config.DAILY_ALERT_BUDGET):
        seed = Alert(
            ticker=f"X{i:02d}",
            asset_class=classes[i % len(classes)],
            score=1.0,
            alpha_z=3.0,
            r_alpha_pct=5.0,
        )
        storage.record_alert(seed, decision="EMIT", reason="ok")

    fresh = Alert(ticker="OVERFLOW", asset_class="equity", score=9.0, alpha_z=3.0, r_alpha_pct=10.0)
    decision, reason = suppression.evaluate(fresh)
    assert decision == "DROP"
    assert "budget_throttle" in reason


def test_first_match_wins_dedup_before_btc_beta(tmp_db):
    """If both dedup_4h and btc_beta would fire, dedup wins because it's first."""
    weak = Alert(ticker="ETH", asset_class="crypto_t1", score=4.0, alpha_z=0.1, r_alpha_pct=0.1)
    storage.record_alert(weak, decision="EMIT", reason="ok")
    decision, reason = suppression.evaluate(weak)
    assert decision == "DROP"
    assert "dedup_4h" in reason  # not btc_beta
