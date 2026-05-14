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

@pytest.fixture(autouse=True)
def _isolate_tier2_from_adjudicator(monkeypatch):
    """Tier 2 promotion path now calls the direction adjudicator, which would
    fetch news + hit Gemini in production. Stub both to keep these tests
    offline and deterministic. Individual tests can opt out by overriding."""
    monkeypatch.setattr(config, "DIR_ADJUDICATE_TIER_2", False)
    # Belt and suspenders — if a test re-enables DIR_ADJUDICATE_TIER_2,
    # stop the network calls from happening.
    monkeypatch.setattr(
        radar_main.catalysts, "fetch_for_market", lambda *a, **kw: [],
    )

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


# ---------- startup backfill ----------

def _fake_fetch_rows(ticker: str, hours: int = 240) -> list[dict]:
    """Build N hourly rows shaped like fetch_bars output."""
    import time as _t
    now_h = int(_t.time()) - int(_t.time()) % 3600
    rows = []
    for i in range(hours):
        ts = now_h - (hours - 1 - i) * 3600
        iso = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({
            "ts": iso, "ticker": ticker, "asset_class": "crypto_t1",
            "max_leverage": 10, "open": 100.0 + i, "high": 101.0 + i,
            "low": 99.0 + i, "price": 100.5 + i,
            "volume_24h_usd": 1e6, "oi_usd": 1e5, "funding_1h": 0.0,
            "pct_24h": 0.0, "pct_1h": 0.0,
        })
    return rows


def test_backfill_happy_path_inserts_bars(tmp_db, monkeypatch):
    """Fresh DB + 3 mocked tickers → bars_1h gets ~720 rows."""
    from radar import fetch_bars
    from radar.universe import Market

    markets = [
        Market(ticker=t, asset_class="crypto_t1", market_id=t, max_leverage=10.0)
        for t in ("BTC", "ETH", "SOL")
    ]
    monkeypatch.setattr(radar_main.universe, "get_leveraged_universe",
                        lambda *a, **kw: markets)
    monkeypatch.setattr(radar_main, "_RUNNING", True)
    # Speed up: zero pacer + tiny timeout that's still well above thread overhead.
    monkeypatch.setattr(config, "BACKFILL_SLEEP_BETWEEN_SEC", 0.0)

    routes = dict(fetch_bars.ROUTES)
    routes["crypto_t1"] = lambda ticker, days, end_ts=None: _fake_fetch_rows(ticker, 240)
    monkeypatch.setattr(fetch_bars, "ROUTES", routes)

    total = asyncio.run(radar_main._backfill_bars_for_universe())
    assert total == 720  # 3 tickers × 240 bars
    rows = storage.execute("SELECT COUNT(*) AS n FROM bars_1h")
    assert rows[0]["n"] == 720


def test_backfill_empty_universe_is_safe(tmp_db, monkeypatch):
    """Lighter API empty → log warning, return 0, don't crash."""
    monkeypatch.setattr(radar_main.universe, "get_leveraged_universe",
                        lambda *a, **kw: [])
    monkeypatch.setattr(radar_main, "_RUNNING", True)
    total = asyncio.run(radar_main._backfill_bars_for_universe())
    assert total == 0
    rows = storage.execute("SELECT COUNT(*) AS n FROM bars_1h")
    assert rows[0]["n"] == 0


def test_backfill_honors_ticker_route_override(tmp_db, monkeypatch):
    """PAXG is commodity-classed but the override routes it through fetch_crypto.

    Verified by stubbing both fetchers and asserting only the crypto route is
    called for PAXG.
    """
    from radar import fetch_bars
    from radar.universe import Market

    markets = [Market(ticker="PAXG", asset_class="commodity",
                      market_id="PAXG", max_leverage=10.0)]
    monkeypatch.setattr(radar_main.universe, "get_leveraged_universe",
                        lambda *a, **kw: markets)
    monkeypatch.setattr(radar_main, "_RUNNING", True)
    monkeypatch.setattr(config, "BACKFILL_SLEEP_BETWEEN_SEC", 0.0)

    crypto_calls: list[str] = []
    yfinance_calls: list[str] = []

    def fake_crypto(ticker, days, end_ts=None):
        crypto_calls.append(ticker)
        return _fake_fetch_rows(ticker, 240)

    def fake_yfinance(ticker, days, end_ts=None):
        yfinance_calls.append(ticker)
        return _fake_fetch_rows(ticker, 240)

    routes = dict(fetch_bars.ROUTES)
    routes["crypto_t1"] = fake_crypto
    routes["commodity"] = fake_yfinance
    monkeypatch.setattr(fetch_bars, "ROUTES", routes)

    asyncio.run(radar_main._backfill_bars_for_universe())
    assert crypto_calls == ["PAXG"]
    assert yfinance_calls == []


def test_backfill_isolates_per_ticker_failures(tmp_db, monkeypatch):
    """One fetcher raises → other tickers still get backfilled."""
    from radar import fetch_bars
    from radar.universe import Market

    markets = [
        Market(ticker=t, asset_class="crypto_t1", market_id=t, max_leverage=10.0)
        for t in ("BTC", "BROKEN", "ETH")
    ]
    monkeypatch.setattr(radar_main.universe, "get_leveraged_universe",
                        lambda *a, **kw: markets)
    monkeypatch.setattr(radar_main, "_RUNNING", True)
    monkeypatch.setattr(config, "BACKFILL_SLEEP_BETWEEN_SEC", 0.0)

    def flaky_fetcher(ticker, days, end_ts=None):
        if ticker == "BROKEN":
            raise RuntimeError("simulated API meltdown")
        return _fake_fetch_rows(ticker, 240)

    routes = dict(fetch_bars.ROUTES)
    routes["crypto_t1"] = flaky_fetcher
    monkeypatch.setattr(fetch_bars, "ROUTES", routes)

    total = asyncio.run(radar_main._backfill_bars_for_universe())
    assert total == 480  # BTC + ETH only
    btc = storage.execute("SELECT COUNT(*) AS n FROM bars_1h WHERE ticker='BTC'")
    eth = storage.execute("SELECT COUNT(*) AS n FROM bars_1h WHERE ticker='ETH'")
    broken = storage.execute("SELECT COUNT(*) AS n FROM bars_1h WHERE ticker='BROKEN'")
    assert btc[0]["n"] == 240
    assert eth[0]["n"] == 240
    assert broken[0]["n"] == 0


def _seed_dense_history(ticker: str, hours: int) -> None:
    """Insert one bar per hour for the last `hours` hours, so the density
    check passes. Density threshold is currently 0.5 × 240 = 120 bars."""
    import time as _t
    now = int(_t.time())
    base = now - (now % 3600)
    for i in range(hours):
        storage.insert_bar(ticker=ticker, ts=base - i * 3600, close=100.0 + i)


def test_compute_backfill_hours_skips_when_dense_and_fresh(tmp_db):
    """Dense history (>50% of window) AND last bar < 1h old → skip."""
    _seed_dense_history("BTC", hours=200)  # well above the 120 density floor
    assert radar_main._compute_backfill_hours("BTC") is None


def test_compute_backfill_hours_full_when_empty(tmp_db):
    """No bars stored → return full BOS_BAR_HISTORY_HOURS."""
    assert radar_main._compute_backfill_hours("NEVERSEEN") == config.BOS_BAR_HISTORY_HOURS


def test_compute_backfill_hours_full_when_thin_history(tmp_db):
    """Regression: a recent bar exists but density is below the floor.

    This is the bug the May 2026 server deploy hit — Tier 1 had written 3
    bars per ticker, the freshness check saw a sub-1h-old bar and returned
    None, so backfill silently skipped everything and BOS never had data.
    """
    import time as _t
    now = int(_t.time())
    base = now - (now % 3600)
    # 3 recent bars, far below the 120-bar density floor
    for i in range(3):
        storage.insert_bar(ticker="BTC", ts=base - i * 3600, close=100.0)
    assert radar_main._compute_backfill_hours("BTC") == config.BOS_BAR_HISTORY_HOURS


def test_compute_backfill_hours_partial_when_dense_and_gap(tmp_db):
    """Dense history (>50% of window) but last bar is several hours old →
    fetch just the gap."""
    import time as _t
    now = int(_t.time())
    base = now - (now % 3600)
    # 200 bars, with the most recent one 5h old
    for i in range(200):
        storage.insert_bar(ticker="BTC", ts=base - (5 + i) * 3600, close=100.0 + i)
    hours = radar_main._compute_backfill_hours("BTC")
    assert hours is not None
    assert 5 <= hours <= 6


def test_compute_backfill_hours_equity_uses_class_override(tmp_db):
    """Equities only trade ~6.5h/day, so the BOS-floor density check fails on
    240h of calendar time even when fully backfilled. The fetch target should
    come from ``BACKFILL_HOURS_BY_CLASS`` (60d × 24h) so yfinance returns
    enough trading bars to clear the 132-bar 4h-BOS floor.
    """
    assert (
        radar_main._compute_backfill_hours("AAPL", asset_class="equity")
        == config.BACKFILL_HOURS_BY_CLASS["equity"]
    )
    # Crypto path unchanged — falls back to BOS_BAR_HISTORY_HOURS.
    assert (
        radar_main._compute_backfill_hours("BTCNEW", asset_class="crypto_t1")
        == config.BOS_BAR_HISTORY_HOURS
    )
    # Unmapped class falls back to BOS_BAR_HISTORY_HOURS too.
    assert (
        radar_main._compute_backfill_hours("NONESUCH", asset_class=None)
        == config.BOS_BAR_HISTORY_HOURS
    )


# ---------- storage helpers ----------

def test_last_bar_ts_returns_none_when_empty(tmp_db):
    assert storage.last_bar_ts("NONE") is None


def test_last_bar_ts_returns_max(tmp_db):
    storage.insert_bar(ticker="BTC", ts=1_700_000_000, close=1.0)
    storage.insert_bar(ticker="BTC", ts=1_700_003_600, close=2.0)
    storage.insert_bar(ticker="BTC", ts=1_700_001_800, close=3.0)
    assert storage.last_bar_ts("BTC") == 1_700_003_600


def test_upsert_bar_from_tick_seeds_ohlc_on_first_tick(tmp_db):
    """First snapshot of a fresh 1h bucket: open=high=low=close=price."""
    storage.upsert_bar_from_tick(ticker="BTC", ts=1_700_000_000, price=100.0,
                                 volume=5_000.0)
    rows = storage.execute("SELECT * FROM bars_1h WHERE ticker='BTC'")
    assert len(rows) == 1
    r = rows[0]
    assert r["open"] == 100.0
    assert r["high"] == 100.0
    assert r["low"] == 100.0
    assert r["close"] == 100.0
    assert r["volume"] == 5_000.0


def test_upsert_bar_from_tick_aggregates_within_bucket(tmp_db):
    """Subsequent ticks extend high/low and update close; open is preserved."""
    ts = 1_700_000_000
    storage.upsert_bar_from_tick(ticker="BTC", ts=ts, price=100.0)
    storage.upsert_bar_from_tick(ticker="BTC", ts=ts, price=105.0)
    storage.upsert_bar_from_tick(ticker="BTC", ts=ts, price=98.0)
    storage.upsert_bar_from_tick(ticker="BTC", ts=ts, price=102.0)
    r = storage.execute("SELECT * FROM bars_1h WHERE ticker='BTC'")[0]
    assert r["open"] == 100.0  # first tick wins, unchanged
    assert r["high"] == 105.0  # extended up
    assert r["low"] == 98.0    # extended down
    assert r["close"] == 102.0 # latest tick


def test_upsert_bar_from_tick_extends_backfilled_bar(tmp_db):
    """Regression: the live-engine "no_structure_break" bug.

    Backfill writes a 17:00 bar with full OHLC. Tier 1's first tick at 17:29
    must NOT wipe open/high/low (the old insert_bar(close=price) path did this
    via INSERT OR REPLACE), or has_breakout_structure sees range=0 and BOS
    never fires. The new path must extend, not overwrite.
    """
    ts = 1_700_000_000
    storage.insert_bar(ticker="FF", ts=ts, open_=0.065, high=0.0729,
                       low=0.0641, close=0.0723, volume=257_594.0)
    # Live tick at 0.0710 — inside the existing range. open/high/low unchanged.
    storage.upsert_bar_from_tick(ticker="FF", ts=ts, price=0.0710,
                                 volume=260_000.0)
    r = storage.execute("SELECT * FROM bars_1h WHERE ticker='FF'")[0]
    assert r["open"] == 0.065
    assert r["high"] == 0.0729
    assert r["low"] == 0.0641
    assert r["close"] == 0.0710
    # Live tick at 0.0800 — extends the high.
    storage.upsert_bar_from_tick(ticker="FF", ts=ts, price=0.0800)
    r = storage.execute("SELECT * FROM bars_1h WHERE ticker='FF'")[0]
    assert r["high"] == 0.0800
    assert r["low"] == 0.0641
    assert r["close"] == 0.0800


def test_record_market_bar_produces_nonzero_range_for_bos(tmp_db):
    """End-to-end: ranker.has_breakout_structure's range check is gated on
    current_bar.high - current_bar.low. Verify that after a few Tier-1 ticks
    we have a non-zero range available."""
    market = _mk_market("AMD", price=370.0)
    radar_main._record_market_bar(market)
    market = _mk_market("AMD", price=375.0)
    radar_main._record_market_bar(market)
    market = _mk_market("AMD", price=365.0)
    radar_main._record_market_bar(market)
    bars = storage.recent_bars("AMD", hours=2)
    assert bars, "expected at least one bar persisted"
    last = bars[-1]
    assert last.high is not None and last.low is not None
    assert (last.high - last.low) > 0.0  # would be 0 under the old code


def test_prune_old_alerts_deletes_old_keeps_fresh(tmp_db):
    import time as _t
    now = int(_t.time())
    # Seed two alerts directly (record_alert sets created_at to _now())
    from radar.suppression import Alert
    from radar.classifier import ClassifierResult
    cr = ClassifierResult(catalyst_type="none", direction="neutral",
                         confidence=0.5, summary="x")
    storage.record_alert(
        Alert(ticker="FRESH", asset_class="equity", score=1.0,
              alpha_z=0.0, r_alpha_pct=0.0, classifier_result=cr),
        decision="DROP", reason="x", classifier=cr,
    )
    # Backdate one alert past the prune window
    storage.execute(
        "UPDATE alerts SET created_at = ? WHERE ticker = ?",
        (now - 40 * 86400, "FRESH"),
    )
    storage.record_alert(
        Alert(ticker="KEEPME", asset_class="equity", score=1.0,
              alpha_z=0.0, r_alpha_pct=0.0, classifier_result=cr),
        decision="DROP", reason="x", classifier=cr,
    )
    removed = storage.prune_old_alerts(days=30)
    assert removed == 1
    remaining = storage.execute("SELECT ticker FROM alerts")
    assert [r["ticker"] for r in remaining] == ["KEEPME"]


# ---------- hourly report ----------

def test_hourly_report_renders_with_empty_state(tmp_db):
    """No watchlist / no recent candidates → still produces a heartbeat body."""
    radar_main._LAST_SCAN_TS = 0.0
    radar_main._LAST_TOP_CANDIDATES = []
    with patch.object(radar_main.universe, "get_leveraged_universe",
                      return_value=[]):
        body = radar_main._format_hourly_report()
    assert "RADAR HOURLY" in body
    assert "Watchlist empty" in body or "Watchlist: 0" in body


def test_hourly_report_lists_watchlist_and_movers(tmp_db):
    import time as _t
    _seed_watchlist_entry("AMD", swing_high=362.0)
    radar_main._LAST_SCAN_TS = _t.time() - 60  # 1 minute ago
    radar_main._LAST_TOP_CANDIDATES = [("BTC", 8.4, 5.2), ("ETH", 7.1, 3.4)]
    with patch.object(radar_main.universe, "get_leveraged_universe",
                      return_value=[]):
        body = radar_main._format_hourly_report()
    assert "AMD" in body
    assert "BTC" in body and "ETH" in body
    assert "Active watchlist" in body
    assert "Recent top movers" in body
