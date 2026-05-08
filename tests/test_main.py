"""Tier-2 polling tests.

Tier 1 is exercised via the existing replay harness; here we focus on the
asynchronous trigger-watch loop:

  * polls only active watchlist tickers
  * promotes on a live cross + range-expansion
  * does NOT promote without range expansion
  * promotion metadata carries `hours_on_watchlist`
  * gracefully handles a delisted ticker (snapshot returns None)
"""

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from radar import config, main as radar_main, storage
from radar.classifier import ClassifierResult
from radar.universe import Market


def _mk_market(ticker: str = "AMD", price: float = 370.0) -> Market:
    return Market(
        ticker=ticker, asset_class="equity", market_id=ticker,
        max_leverage=10.0, price=price, volume_24h_usd=50_000_000,
        oi_usd=10_000_000, funding_1h=0.0, pct_24h=8.0, pct_1h=3.0,
    )


def _seed_watchlist_entry(
    ticker: str = "AMD",
    direction: str = "long",
    swing_high: float | None = 362.0,
    swing_low: float | None = None,
    median_range: float = 5.0,
    added_minutes_ago: int = 30,
):
    """Insert a watchlist row directly, with a controlled added_at time."""
    classifier_result = ClassifierResult(
        catalyst_type="earnings", direction=direction, confidence=0.85,
        summary="AMD beat Q1 estimates", evidence_quotes=[],
        primary_catalyst="AMD beat Q1 estimates",
        conviction=0.85, horizon="swing",
        continuation_thesis="Earnings beat with raised guidance",
        kill_signal="Drop back below 362",
    )
    storage.add_to_watchlist(
        ticker=ticker, asset_class="equity", direction_bias=direction,
        score=82.0, catalyst_summary="AMD beat Q1 estimates",
        classifier_json=classifier_result.model_dump_json(),
        swing_high_reference=swing_high, swing_low_reference=swing_low,
        swing_reference_timestamp=None, median_bar_range=median_range,
        ttl_hours=72,
    )
    # Backdate added_at so hours_on_watchlist > 0 in the test
    backdated = (datetime.utcnow() - timedelta(minutes=added_minutes_ago)).isoformat()
    storage.execute(
        "UPDATE watchlist SET added_at = ? WHERE ticker = ?",
        (backdated, ticker),
    )


def _seed_recent_bars(ticker: str = "AMD", current_high: float = 372.0,
                     current_low: float = 365.0, current_close: float = 371.0):
    """Insert two hourly bars: a quiet prior bar and an in-progress wide bar."""
    import time as _t
    now = int(_t.time())
    h = now - (now % 3600)
    storage.insert_bar(ticker=ticker, ts=h - 3600,
                       open_=361.0, high=362.5, low=360.5, close=361.5,
                       volume=1_000_000, oi=5_000_000, funding=0.0)
    storage.insert_bar(ticker=ticker, ts=h,
                       open_=362.0, high=current_high, low=current_low,
                       close=current_close,
                       volume=2_000_000, oi=5_000_000, funding=0.0)


# ============================================================================
# tests
# ============================================================================

def test_tier2_polls_active_watchlist_only(tmp_db):
    _seed_watchlist_entry("AMD")
    _seed_watchlist_entry("NVDA")
    # backdate one entry past expiry
    storage.execute(
        "UPDATE watchlist SET expires_at = ? WHERE ticker = ?",
        ((datetime.utcnow() - timedelta(hours=1)).isoformat(), "NVDA"),
    )
    _seed_recent_bars("AMD")
    _seed_recent_bars("NVDA")

    polled: list[str] = []

    def fake_snapshot(ticker):
        polled.append(ticker)
        return _mk_market(ticker, price=300.0)  # below break — no promotion

    with patch.object(radar_main.universe, "get_market_snapshot", fake_snapshot):
        asyncio.run(radar_main.run_trigger_poll())

    assert "AMD" in polled
    assert "NVDA" not in polled  # expired


def test_tier2_promotes_on_live_price_cross(tmp_db):
    """AMD-style scenario: stored swing_high $362, live $370 + range 7 vs median 5."""
    _seed_watchlist_entry("AMD", swing_high=362.0, median_range=2.0)
    _seed_recent_bars("AMD", current_high=372.0, current_low=365.0, current_close=370.0)

    sent: list[tuple] = []

    def fake_snapshot(ticker):
        return _mk_market(ticker, price=370.0)

    def fake_send(market, result, metadata, source, plan=None):
        sent.append((market.ticker, metadata, source, plan))
        return True

    with patch.object(radar_main.universe, "get_market_snapshot", fake_snapshot), \
         patch.object(radar_main.telegram, "send_bos_alert", fake_send):
        asyncio.run(radar_main.run_trigger_poll())

    assert len(sent) == 1
    ticker, metadata, source, _plan = sent[0]
    assert ticker == "AMD"
    assert source == "tier2_promoted"
    assert metadata["breakout_level"] == 362.0  # NOT the close ($370)
    # watchlist row removed after promotion
    assert storage.get_watchlist_entry("AMD") is None


def test_tier2_does_not_promote_without_range_expansion(tmp_db):
    """Same setup but the in-progress bar has a normal-sized range."""
    _seed_watchlist_entry("AMD", swing_high=362.0, median_range=2.0)
    # current bar range = 0.5, median = 2.0 → 0.25x < 1.5x threshold
    _seed_recent_bars("AMD", current_high=370.5, current_low=370.0, current_close=370.3)

    sent: list[tuple] = []

    def fake_send(market, result, metadata, source, plan=None):
        sent.append((market.ticker, metadata, source, plan))
        return True

    with patch.object(radar_main.universe, "get_market_snapshot",
                      lambda t: _mk_market(t, price=370.0)), \
         patch.object(radar_main.telegram, "send_bos_alert", fake_send):
        asyncio.run(radar_main.run_trigger_poll())

    assert sent == []
    # entry still on watchlist
    assert storage.get_watchlist_entry("AMD") is not None


def test_tier2_promotion_includes_hours_on_watchlist_in_metadata(tmp_db):
    """`hours_on_watchlist` must surface in the promotion payload."""
    _seed_watchlist_entry("AMD", swing_high=362.0, median_range=2.0,
                         added_minutes_ago=120)  # 2.0h ago
    _seed_recent_bars("AMD", current_high=372.0, current_low=365.0)

    captured: dict = {}

    def fake_send(market, result, metadata, source, plan=None):
        captured.update(metadata)
        captured["source"] = source
        captured["plan"] = plan
        return True

    with patch.object(radar_main.universe, "get_market_snapshot",
                      lambda t: _mk_market(t, price=370.0)), \
         patch.object(radar_main.telegram, "send_bos_alert", fake_send):
        asyncio.run(radar_main.run_trigger_poll())

    assert captured.get("promoted_from_watchlist") is True
    assert captured.get("hours_on_watchlist") is not None
    # ~2h ago; allow some clock slack
    assert 1.5 <= float(captured["hours_on_watchlist"]) <= 2.5
    assert captured["source"] == "tier2_promoted"


def test_tier2_handles_delisted_ticker_gracefully(tmp_db):
    """If get_market_snapshot returns None (delisted), the poll just skips."""
    _seed_watchlist_entry("DELISTED", swing_high=100.0)
    _seed_recent_bars("DELISTED")

    sent: list = []

    def none_snapshot(ticker):
        return None

    def fake_send(*args, **kwargs):
        sent.append(args)
        return True

    with patch.object(radar_main.universe, "get_market_snapshot", none_snapshot), \
         patch.object(radar_main.telegram, "send_bos_alert", fake_send):
        # No exception
        asyncio.run(radar_main.run_trigger_poll())

    # entry still on watchlist (we didn't promote)
    assert storage.get_watchlist_entry("DELISTED") is not None
    assert sent == []


# ---------- ancillary main.py helpers ----------

def test_build_alert_carries_classifier_result(tmp_db):
    market = _mk_market("AMD")
    result = ClassifierResult(
        catalyst_type="earnings", direction="long", confidence=0.9,
        summary="beat", evidence_quotes=[],
    )
    alert = radar_main.build_alert(market, result, history_dict={}, btc_rets=[], score=42.0)
    assert alert.classifier_result is result
    assert alert.score == 42.0


def test_expire_stale_watchlist_during_tier1(tmp_db):
    _seed_watchlist_entry("STALE")
    storage.execute(
        "UPDATE watchlist SET expires_at = ? WHERE ticker = ?",
        ((datetime.utcnow() - timedelta(hours=2)).isoformat(), "STALE"),
    )
    # Force the discovery cycle to do nothing useful but call expire_stale
    with patch.object(radar_main.universe, "get_leveraged_universe",
                      return_value=[]):
        asyncio.run(radar_main.run_discovery_cycle())
    assert storage.get_watchlist_entry("STALE") is None
