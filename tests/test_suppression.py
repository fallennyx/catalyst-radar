"""Suppression-chain tests under the new BOS-aware signature.

evaluate(market, alert, history) → tuple[decision, reason, metadata]

The old tests (per-ticker dedup, btc_beta, sector_day, budget_throttle) are
preserved but rewritten for the new API; the new tests cover Rule 0 (BOS +
watchlist routing) and metadata propagation.
"""

import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from radar import config, storage, suppression
from radar.classifier import ClassifierResult
from radar.suppression import Alert
from radar.universe import Market


# ---------- helpers ----------

def _market(ticker: str = "BTC", asset_class: str = "crypto_t1", price: float = 100.0):
    return Market(
        ticker=ticker,
        asset_class=asset_class,
        market_id=ticker,
        max_leverage=10.0,
        price=price,
        volume_24h_usd=10_000_000,
        oi_usd=1_000_000,
        funding_1h=0.0,
        pct_24h=2.0,
        pct_1h=1.0,
    )


def _classifier(direction: str = "long", catalyst: str = "earnings") -> ClassifierResult:
    return ClassifierResult(
        catalyst_type=catalyst,
        direction=direction,
        confidence=0.8,
        summary="Test catalyst",
        evidence_quotes=[],
        is_actionable=True,
    )


def _alert(
    ticker: str = "BTC",
    asset_class: str = "crypto_t1",
    score: float = 4.0,
    alpha_z: float = 3.0,
    r_alpha_pct: float = 5.0,
    direction: str = "long",
    catalyst: str = "earnings",
) -> Alert:
    return Alert(
        ticker=ticker,
        asset_class=asset_class,
        score=score,
        alpha_z=alpha_z,
        r_alpha_pct=r_alpha_pct,
        classifier_result=_classifier(direction=direction, catalyst=catalyst),
    )


def _bos_history_long_break(price_above: float = 105.0):
    """Return a Bar-list where a swing high at $100 has been broken at the
    in-progress bar (current price > $100 with range expansion)."""
    from radar.storage import Bar
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(Bar(ticker="X", ts=i * 3600, high=100.0, low=99.0,
                            close=99.5, volume=0, oi=0, funding=0))
        else:
            bars.append(Bar(ticker="X", ts=i * 3600, high=92.0, low=90.0,
                            close=91.0, volume=0, oi=0, funding=0))
    # in-progress bar: wide range, price wicks above 100
    bars[-1] = Bar(ticker="X", ts=59 * 3600, open=99.0, high=price_above,
                   low=99.0, close=price_above, volume=0, oi=0, funding=0)
    return bars


def _bos_history_no_break():
    """Bar-list where price stays below the swing high — no BOS."""
    from radar.storage import Bar
    bars: list[Bar] = []
    for i in range(60):
        if i == 20:
            bars.append(Bar(ticker="X", ts=i * 3600, high=100.0, low=99.0,
                            close=99.5, volume=0, oi=0, funding=0))
        else:
            bars.append(Bar(ticker="X", ts=i * 3600, high=92.0, low=90.0,
                            close=91.0, volume=0, oi=0, funding=0))
    return bars


# ============================================================================
# Rule 0 — BOS / watchlist tests (new)
# ============================================================================

def test_bos_confirmed_passes_to_remaining_rules(tmp_db):
    """A clean BOS-confirmed candidate with no other suppression triggers emits."""
    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", direction="long")
    history = _bos_history_long_break(price_above=370.0)
    market = replace(market, price=370.0)

    decision, reason, metadata = suppression.evaluate(market, alert, history)
    assert decision == "EMIT"
    assert reason == "ok"
    assert metadata["breakout_level"] == 100.0


def test_bos_not_confirmed_high_score_routes_to_watchlist(tmp_db):
    market = _market("ETH", asset_class="crypto_t1", price=91.0)
    alert = _alert("ETH", asset_class="crypto_t1", score=75.0, direction="long")
    history = _bos_history_no_break()

    decision, reason, metadata = suppression.evaluate(market, alert, history)
    assert decision == "WATCHLIST"
    assert reason == "awaiting_structure_break"
    # The watchlist row must exist with the precomputed references
    entry = storage.get_watchlist_entry("ETH")
    assert entry is not None
    assert entry["direction_bias"] == "long"
    assert entry["swing_high_reference"] == 100.0


def test_bos_not_confirmed_low_score_drops(tmp_db):
    market = _market("SOL", asset_class="crypto_t1", price=91.0)
    alert = _alert("SOL", asset_class="crypto_t1", score=30.0, direction="long")
    history = _bos_history_no_break()

    decision, reason, metadata = suppression.evaluate(market, alert, history)
    assert decision == "DROP"
    assert reason == "no_structure_break"
    assert storage.get_watchlist_entry("SOL") is None


def test_bos_confirmed_direction_conflict_drops(tmp_db):
    """Long-side BOS but classifier says short → DROP."""
    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", direction="short")
    history = _bos_history_long_break(price_above=370.0)

    decision, reason, _ = suppression.evaluate(market, alert, history)
    assert decision == "DROP"
    assert reason == "structure_direction_conflict"


def test_bos_confirmed_removes_existing_watchlist_entry(tmp_db):
    """If ticker was on watchlist and BOS confirms, the entry is removed."""
    storage.add_to_watchlist(
        ticker="AMD", asset_class="equity", direction_bias="long",
        score=80.0, catalyst_summary="prior", classifier_json="{}",
        swing_high_reference=100.0, swing_low_reference=None,
        swing_reference_timestamp=None, median_bar_range=2.0, ttl_hours=72,
    )
    assert storage.get_watchlist_entry("AMD") is not None

    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", direction="long")
    history = _bos_history_long_break(price_above=370.0)

    decision, _, _ = suppression.evaluate(market, alert, history)
    assert decision == "EMIT"
    # entry removed
    assert storage.get_watchlist_entry("AMD") is None


def test_metadata_includes_breakout_level_when_emit(tmp_db):
    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", direction="long")
    history = _bos_history_long_break(price_above=370.0)

    _, _, metadata = suppression.evaluate(market, alert, history)
    assert metadata["breakout_level"] == 100.0
    assert metadata["swing_high_reference"] == 100.0
    assert "median_bar_range" in metadata


def test_metadata_includes_references_when_watchlist(tmp_db):
    market = _market("ETH", asset_class="crypto_t1", price=91.0)
    alert = _alert("ETH", asset_class="crypto_t1", score=75.0, direction="long")
    history = _bos_history_no_break()

    _, _, metadata = suppression.evaluate(market, alert, history)
    # No BOS, so breakout_level is None — but the swing references are set
    assert metadata["breakout_level"] is None
    assert metadata["swing_high_reference"] == 100.0
    assert metadata["median_bar_range"] > 0


# ============================================================================
# Rule 1-4 — legacy rules, migrated to the new signature
# ============================================================================

def test_emit_when_no_other_suppression_triggers(tmp_db):
    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", direction="long")
    history = _bos_history_long_break(price_above=370.0)

    decision, reason, _ = suppression.evaluate(market, alert, history)
    assert decision == "EMIT"
    assert reason == "ok"


def test_dedup_4h_blocks_repeat_emit(tmp_db):
    """Recent EMIT for same ticker + same catalyst_type → DROP via Rule 1."""
    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", direction="long", catalyst="earnings")
    history = _bos_history_long_break(price_above=370.0)

    # First emit
    decision, _, _ = suppression.evaluate(market, alert, history)
    assert decision == "EMIT"
    storage.record_alert(alert, decision="EMIT", reason="ok", classifier=alert.classifier_result)

    # Same ticker + catalyst_type within 4h → DROP
    decision, reason, _ = suppression.evaluate(market, alert, history)
    assert decision == "DROP"
    assert reason == "dedup_4h"


def test_dedup_4h_does_not_block_different_catalyst_type(tmp_db):
    """A new catalyst_type for the same ticker is NOT blocked by dedup."""
    market = _market("AMD", asset_class="equity", price=370.0)
    history = _bos_history_long_break(price_above=370.0)

    a1 = _alert("AMD", asset_class="equity", direction="long", catalyst="earnings")
    storage.record_alert(a1, decision="EMIT", reason="ok", classifier=a1.classifier_result)

    a2 = _alert("AMD", asset_class="equity", direction="long", catalyst="partnership")
    decision, reason, _ = suppression.evaluate(market, a2, history)
    assert decision == "EMIT"
    assert reason == "ok"


def test_btc_beta_drops_low_alpha_crypto(tmp_db):
    """Crypto with weak alpha_z → DROP under the new OR gate."""
    market = _market("ETH", asset_class="crypto_t1", price=370.0)
    alert = _alert("ETH", asset_class="crypto_t1",
                   alpha_z=0.5, r_alpha_pct=1.0, direction="long")
    history = _bos_history_long_break(price_above=370.0)

    # Patch beta.compute_alpha_z so we don't depend on real returns
    with patch("radar.suppression.beta.compute_alpha_z", return_value=(0.5, 1.0)):
        decision, reason, _ = suppression.evaluate(market, alert, history)
    assert decision == "DROP"
    assert reason == "pure_btc_beta"


def test_btc_beta_skipped_for_non_crypto(tmp_db):
    """Equities don't go through the BTC beta gate."""
    market = _market("AMD", asset_class="equity", price=370.0)
    alert = _alert("AMD", asset_class="equity", alpha_z=0.0, r_alpha_pct=0.0,
                   direction="long")
    history = _bos_history_long_break(price_above=370.0)

    decision, reason, _ = suppression.evaluate(market, alert, history)
    assert decision == "EMIT"
    assert reason == "ok"


def test_sector_day_clustering(tmp_db):
    """Once `SECTOR_DAY_THRESHOLD` EMITs land in the asset_class, lower-scoring
    new candidates DROP. Higher-scoring ones break through."""
    history = _bos_history_long_break(price_above=370.0)

    for i in range(config.SECTOR_DAY_THRESHOLD):
        seed = _alert(
            ticker=f"FILL{i:02d}", asset_class="equity",
            score=1.0, direction="long",
        )
        storage.record_alert(seed, decision="EMIT", reason="ok",
                             classifier=seed.classifier_result)

    # Lower score → DROP via sector_day
    market = _market("AMD", asset_class="equity", price=370.0)
    low = _alert("AMD", asset_class="equity", score=0.5, direction="long")
    decision, reason, _ = suppression.evaluate(market, low, history)
    assert decision == "DROP"
    assert reason == "sector_day_member"

    # Higher score breaks through
    high = _alert("NVDA", asset_class="equity", score=99.0, direction="long")
    market_nvda = _market("NVDA", asset_class="equity", price=370.0)
    decision, reason, _ = suppression.evaluate(market_nvda, high, history)
    assert decision == "EMIT"


def test_budget_throttle(tmp_db, monkeypatch):
    """Once daily budget hits, only above-median-score candidates emit."""
    # Disable the sector_day rule for this test
    monkeypatch.setattr(config, "SECTOR_DAY_THRESHOLD", 999)
    history = _bos_history_long_break(price_above=370.0)

    # Seed enough EMITs across asset classes to hit the budget
    classes = ["equity", "crypto_t1", "crypto_t2", "crypto_meme", "commodity"]
    for i in range(config.DAILY_ALERT_BUDGET):
        seed = _alert(
            ticker=f"X{i:02d}", asset_class=classes[i % len(classes)],
            score=10.0, direction="long",
        )
        storage.record_alert(seed, decision="EMIT", reason="ok",
                             classifier=seed.classifier_result)

    # Below median → DROP
    low = _alert("AMD", asset_class="equity", score=1.0, direction="long")
    market = _market("AMD", asset_class="equity", price=370.0)
    decision, reason, _ = suppression.evaluate(market, low, history)
    assert decision == "DROP"
    assert reason == "budget_throttle"
