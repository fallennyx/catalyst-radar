"""SQLite persistence layer. No ORM — sqlite3 stdlib + parametrized queries."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config


# ============ Bar dataclass (BOS filter consumers want attribute access) ============

@dataclass
class Bar:
    """Hourly OHLCV+ bar. Supports both attribute (`b.high`) and item
    (`b["high"]`) access so legacy callers that read like sqlite3.Row keep
    working."""
    ticker: str
    ts: int
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    oi: float | None = None
    funding: float | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def _row_to_bar(row: sqlite3.Row) -> Bar:
    return Bar(
        ticker=row["ticker"],
        ts=int(row["ts"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        oi=row["oi"],
        funding=row["funding"],
    )


# ============ ISO-8601 helpers (watchlist uses string timestamps) ============

def _now_iso() -> str:
    """Naive UTC ISO-8601 string. Matches `datetime.utcnow().isoformat()`
    exactly so legacy tooling that compares against that format still works."""
    return datetime.utcnow().isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets_state (
    ticker          TEXT PRIMARY KEY,
    asset_class     TEXT NOT NULL,
    market_id       TEXT,
    max_leverage    REAL,
    price           REAL,
    volume_24h_usd  REAL,
    oi_usd          REAL,
    funding_1h      REAL,
    pct_24h         REAL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bars_1h (
    ticker      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    oi          REAL,
    funding     REAL,
    PRIMARY KEY (ticker, ts)
);

CREATE INDEX IF NOT EXISTS idx_bars_ticker_ts ON bars_1h(ticker, ts DESC);

CREATE TABLE IF NOT EXISTS bars_15m (
    ticker      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    oi          REAL,
    funding     REAL,
    PRIMARY KEY (ticker, ts)
);

CREATE INDEX IF NOT EXISTS idx_bars_15m_ticker_ts ON bars_15m(ticker, ts DESC);

CREATE TABLE IF NOT EXISTS news_items (
    url_hash    TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    source      TEXT,
    title       TEXT,
    body        TEXT,
    url         TEXT,
    published   INTEGER,
    fetched_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_ticker_pub ON news_items(ticker, published DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    score           REAL,
    alpha_z         REAL,
    r_alpha_pct     REAL,
    catalyst_type   TEXT,
    direction       TEXT,
    confidence      REAL,
    summary         TEXT,
    evidence        TEXT,
    decision        TEXT NOT NULL,
    reason          TEXT,
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_ticker_time ON alerts(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_decision_time ON alerts(decision, created_at DESC);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    direction_bias TEXT NOT NULL,
    score REAL NOT NULL,
    catalyst_summary TEXT NOT NULL,
    classifier_json TEXT NOT NULL,
    swing_high_reference REAL,
    swing_low_reference REAL,
    swing_reference_timestamp TIMESTAMP,
    median_bar_range REAL NOT NULL,
    added_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    last_polled_at TIMESTAMP,
    last_polled_price REAL
);

CREATE INDEX IF NOT EXISTS idx_watchlist_expires ON watchlist(expires_at);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist(expires_at, ticker);

-- ============ EXECUTOR (EXECUTOR_SPEC.md §10 — capture raw, derive later) ============
-- Every trade-linked row carries config_version_id so a threshold change never
-- contaminates the dataset. Every table keeps raw_json for fields we didn't
-- think to parse today.

CREATE TABLE IF NOT EXISTS config_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    config_hash          TEXT NOT NULL UNIQUE,
    git_sha              TEXT,
    created_at           TEXT NOT NULL,
    enabled_tiers        TEXT,
    max_loss_per_trade   REAL,
    leverage_cap         REAL,
    time_exit_hours      REAL,
    extension_threshold_r REAL,
    max_concurrent       INTEGER,
    daily_max_loss       REAL,
    full_config_json     TEXT
);

CREATE TABLE IF NOT EXISTS signal_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_ts                INTEGER NOT NULL,
    ticker                  TEXT NOT NULL,
    asset_class             TEXT,
    tier_decision           TEXT,
    structure_type          TEXT,
    breakout_level          REAL,
    swing_high_4h           REAL,
    swing_low_4h            REAL,
    swing_ref_ts            TEXT,
    median_bar_range_1h     REAL,
    distance_past_pivot_pct REAL,
    range_ratio_1h          REAL,
    range_ratio_15m         REAL,
    score                   REAL,
    score_pctile            REAL,
    alpha_z                 REAL,
    r_alpha_pct             REAL,
    vol_ratio               REAL,
    volume_z                REAL,
    cluster_size            INTEGER,
    pop_score               REAL,
    oi_velocity_z           REAL,
    funding_z               REAL,
    wash_penalty            REAL,
    oi_usd                  REAL,
    funding_1h              REAL,
    book_bid_usd            REAL,
    book_ask_usd            REAL,
    book_ratio              REAL,
    book_sentiment          TEXT,
    vpoc_price              REAL,
    vpoc_near_breakout      INTEGER,
    btc_ret_4h              REAL,
    btc_range_expansion     REAL,
    htf_trend_align_7d      INTEGER,
    adj_direction           TEXT,
    adj_confidence          REAL,
    adj_setup_quality       REAL,
    adj_conviction_tier     TEXT,
    adj_flipped             INTEGER,
    adj_thesis              TEXT,
    clf_catalyst_type       TEXT,
    clf_direction           TEXT,
    clf_confidence          REAL,
    clf_summary             TEXT,
    clf_evidence_quotes     TEXT,
    news_items_json         TEXT,
    utc_hour                INTEGER,
    day_of_week             INTEGER,
    tier_source             INTEGER,
    watchlist_age_hours     REAL,
    config_version_id       INTEGER,
    raw_json                TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_ts ON signal_snapshots(ticker, alert_ts DESC);

CREATE TABLE IF NOT EXISTS executions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_snapshot_id    INTEGER,
    acted                 INTEGER NOT NULL,
    skip_reason           TEXT,
    conviction_tier       TEXT,
    risk_per_unit         REAL,
    intended_entry        REAL,
    intended_stop         REAL,
    intended_tp1          REAL,
    intended_tp2          REAL,
    computed_size_usd     REAL,
    computed_contracts    REAL,
    size_mult_score       REAL,
    leverage_used         REAL,
    free_margin_at_decision REAL,
    config_version_id     INTEGER,
    created_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_created ON executions(created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      INTEGER,
    ticker            TEXT,
    market_index      INTEGER,
    leg               TEXT,
    client_order_index INTEGER,
    order_type        TEXT,
    is_ask            INTEGER,
    reduce_only       INTEGER,
    trigger_price     REAL,
    limit_price       REAL,
    base_amount_int   INTEGER,
    tx_hash           TEXT,
    submit_ts         TEXT,
    ack_status        TEXT,
    terminal_status   TEXT,
    reject_reason     TEXT,
    raw_request_json  TEXT,
    raw_response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_execution ON orders(execution_id);
CREATE INDEX IF NOT EXISTS idx_orders_coi ON orders(client_order_index);

CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER,
    ticker            TEXT,
    fill_ts           TEXT,
    fill_price        REAL,
    fill_base_amount  REAL,
    fee_usd           REAL,
    is_partial        INTEGER,
    cumulative_filled REAL,
    raw_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);

CREATE TABLE IF NOT EXISTS positions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id         INTEGER,
    ticker               TEXT NOT NULL,
    direction            TEXT,
    open_ts              INTEGER,
    entry_avg_price      REAL,
    size_contracts       REAL,
    conviction_tier      TEXT,
    stop_order_id        INTEGER,
    tp_order_id          INTEGER,
    stop_price_current   REAL,
    breakeven_moved      INTEGER DEFAULT 0,
    trailing_active      INTEGER DEFAULT 0,
    exit_ts              INTEGER,
    exit_reason          TEXT,
    realized_pnl_usd     REAL,
    fees_total_usd       REAL,
    slippage_vs_alert_px_bps REAL,
    slippage_vs_bar_close_bps REAL,
    mfe_pct              REAL,
    mae_pct              REAL,
    pnl_at_1h_pct        REAL,
    pnl_at_4h_pct        REAL,
    stop_hit_before_1h   INTEGER,
    time_to_mfe_min      REAL,
    time_to_mae_min      REAL,
    plan_stop            REAL,
    plan_tp1             REAL,
    blowoff_flag         INTEGER DEFAULT 0,
    config_version_id    INTEGER,
    raw_json             TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(exit_ts, ticker);

CREATE TABLE IF NOT EXISTS position_marks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id          INTEGER NOT NULL,
    ts                   INTEGER NOT NULL,
    mark_price           REAL,
    unrealized_pnl_pct   REAL,
    minutes_since_entry  REAL
);

CREATE INDEX IF NOT EXISTS idx_marks_position_ts ON position_marks(position_id, ts);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   INTEGER NOT NULL,
    balance_usd          REAL,
    free_margin_usd      REAL,
    total_exposure_usd   REAL,
    open_position_count  INTEGER,
    daily_realized_pnl_usd REAL,
    consecutive_losses   INTEGER,
    raw_json             TEXT
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts DESC);
"""


def _now() -> int:
    """Wall-clock seconds. Reassign this attribute (or call set_clock) to inject
    a virtual clock — the replay harness does this to drive the engine through
    historical timestamps."""
    return int(time.time())


def set_clock(fn) -> None:
    """Override the module's clock. Pass `None` to restore the default."""
    global _now
    _now = fn if fn is not None else (lambda: int(time.time()))


@contextmanager
def _conn(db_path: str | None = None):
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cx = sqlite3.connect(path, timeout=30, isolation_level=None)
    cx.row_factory = sqlite3.Row
    try:
        cx.execute("PRAGMA journal_mode=WAL;")
        cx.execute("PRAGMA synchronous=NORMAL;")
        yield cx
    finally:
        cx.close()


def init_db(db_path: str | None = None) -> None:
    with _conn(db_path) as cx:
        cx.executescript(_SCHEMA)


# ---------- markets_state ----------

def upsert_market_state(market: Any, db_path: str | None = None) -> None:
    """Accepts anything with the Market dataclass attrs."""
    with _conn(db_path) as cx:
        cx.execute(
            """
            INSERT INTO markets_state
                (ticker, asset_class, market_id, max_leverage, price,
                 volume_24h_usd, oi_usd, funding_1h, pct_24h, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                asset_class=excluded.asset_class,
                market_id=excluded.market_id,
                max_leverage=excluded.max_leverage,
                price=excluded.price,
                volume_24h_usd=excluded.volume_24h_usd,
                oi_usd=excluded.oi_usd,
                funding_1h=excluded.funding_1h,
                pct_24h=excluded.pct_24h,
                updated_at=excluded.updated_at
            """,
            (
                market.ticker,
                market.asset_class,
                getattr(market, "market_id", None),
                getattr(market, "max_leverage", None),
                getattr(market, "price", None),
                getattr(market, "volume_24h_usd", None),
                getattr(market, "oi_usd", None),
                getattr(market, "funding_1h", None),
                getattr(market, "pct_24h", None),
                _now(),
            ),
        )


# ---------- bars_1h ----------

def insert_bar(
    ticker: str,
    ts: int,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float | None = None,
    oi: float | None = None,
    funding: float | None = None,
    db_path: str | None = None,
) -> None:
    with _conn(db_path) as cx:
        cx.execute(
            """
            INSERT OR REPLACE INTO bars_1h
                (ticker, ts, open, high, low, close, volume, oi, funding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, ts, open_, high, low, close, volume, oi, funding),
        )


def upsert_bar_from_tick(
    ticker: str,
    ts: int,
    price: float,
    volume: float | None = None,
    oi: float | None = None,
    funding: float | None = None,
    db_path: str | None = None,
) -> None:
    """Aggregate a single mark-price snapshot into the bucket's 1h bar.

    Lighter's universe API only exposes the current mark price (no OHLC for the
    in-progress bar). Tier 1 fires every FAST_CADENCE_SEC; the naive
    `insert_bar(close=price)` path overwrites the bar each tick with INSERT OR
    REPLACE, leaving open/high/low NULL forever — and that nukes BOS detection
    because ``current_bar.high - current_bar.low`` collapses to 0 so the
    range-expansion gate at ranker.py:495 never passes.

    Behavior:
      • Row missing OR existing.open is NULL → seed open=high=low=close=price.
      • Row exists with open populated (from startup backfill OR a prior tick
        this same hour) → keep open as-is, extend high=max(high, price),
        low=min(low, price), update close=price.

    volume/oi/funding are passed through and overwrite (Lighter's volume is a
    24h-rolling snapshot — the latest tick is the freshest reading).
    """
    p = float(price)
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT open, high, low FROM bars_1h WHERE ticker=? AND ts=?",
            (ticker, ts),
        ).fetchone()
        if row is not None and row["open"] is not None:
            new_open = float(row["open"])
            existing_high = float(row["high"]) if row["high"] is not None else p
            existing_low = float(row["low"]) if row["low"] is not None else p
            new_high = max(existing_high, p)
            new_low = min(existing_low, p)
        else:
            new_open = new_high = new_low = p
        cx.execute(
            """
            INSERT OR REPLACE INTO bars_1h
                (ticker, ts, open, high, low, close, volume, oi, funding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, ts, new_open, new_high, new_low, p, volume, oi, funding),
        )


def recent_bars(
    ticker: str,
    hours: int | None = None,
    days: int | None = None,
    db_path: str | None = None,
) -> list[Bar]:
    """Return hourly bars for `ticker` over the last `hours` (or `days*24`)
    sorted oldest → newest.

    Returns `list[Bar]` (attribute access). Bar also supports ``b["close"]``
    indexing so callers that previously read sqlite3.Row keep working.
    """
    if days is not None:
        hours = days * 24
    if hours is None:
        hours = config.ROLLING_WINDOW_DAYS * 24
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM bars_1h WHERE ticker = ? AND ts >= ? ORDER BY ts ASC",
            (ticker, cutoff),
        )
        return [_row_to_bar(r) for r in cur.fetchall()]


def prune_old_bars(days: int = config.ROLLING_WINDOW_DAYS, db_path: str | None = None) -> int:
    cutoff = _now() - days * 86400
    with _conn(db_path) as cx:
        cur = cx.execute("DELETE FROM bars_1h WHERE ts < ?", (cutoff,))
        return cur.rowcount


# ============ bars_15m (sub-hourly confirmation timeframe) ============
#
# Mirrors bars_1h but bucketed to 15-minute intervals (UTC-aligned :00/:15/
# :30/:45). Used by `ranker.has_breakout_structure` as the **parallel**
# fast-confirmation gate — the 4h structural break stays sourced from 1h
# history, only the in-progress range-expansion check runs against the 15m
# bucket when 15m bars are available. With Tier 1 polling every 60s, the
# 15m gate clears 5–15 min sooner than the 1h gate on real impulses.

_FIFTEEN_MIN_SECS = 900


def floor_to_15m_bucket(ts: int) -> int:
    """Truncate a unix timestamp down to the start of its 15m bucket."""
    return int(ts) // _FIFTEEN_MIN_SECS * _FIFTEEN_MIN_SECS


def insert_bar_15m(
    ticker: str,
    ts: int,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float | None = None,
    oi: float | None = None,
    funding: float | None = None,
    db_path: str | None = None,
) -> None:
    """Insert/replace a 15m bar. Used by the backfill path with real OHLC
    from external sources. The live-tick aggregation path uses
    upsert_bar_15m_from_tick instead, which preserves OHL across writes."""
    with _conn(db_path) as cx:
        cx.execute(
            """
            INSERT OR REPLACE INTO bars_15m
                (ticker, ts, open, high, low, close, volume, oi, funding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, ts, open_, high, low, close, volume, oi, funding),
        )


def upsert_bar_15m_from_tick(
    ticker: str,
    ts: int,
    price: float,
    volume: float | None = None,
    oi: float | None = None,
    funding: float | None = None,
    db_path: str | None = None,
) -> None:
    """Aggregate a live mark-price snapshot into the current 15m bucket's
    OHLC. Same read-modify-write pattern as upsert_bar_from_tick — first
    tick of a bucket seeds open=high=low=close=price; subsequent ticks
    extend high/low and update close. ``ts`` should already be floored
    via ``floor_to_15m_bucket``."""
    p = float(price)
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT open, high, low FROM bars_15m WHERE ticker=? AND ts=?",
            (ticker, ts),
        ).fetchone()
        if row is not None and row["open"] is not None:
            new_open = float(row["open"])
            existing_high = float(row["high"]) if row["high"] is not None else p
            existing_low = float(row["low"]) if row["low"] is not None else p
            new_high = max(existing_high, p)
            new_low = min(existing_low, p)
        else:
            new_open = new_high = new_low = p
        cx.execute(
            """
            INSERT OR REPLACE INTO bars_15m
                (ticker, ts, open, high, low, close, volume, oi, funding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, ts, new_open, new_high, new_low, p, volume, oi, funding),
        )


def recent_bars_15m(
    ticker: str,
    hours: int | None = None,
    bars: int | None = None,
    db_path: str | None = None,
) -> list[Bar]:
    """Return 15m bars for ``ticker`` over the last ``hours`` (or ``bars``×15min)
    sorted oldest → newest."""
    if bars is not None:
        hours = max(1, (bars * 15) // 60 + 1)
    if hours is None:
        hours = 50  # default 50h ≈ 200 15m bars (matches BOS_15M_HISTORY_BARS)
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM bars_15m WHERE ticker = ? AND ts >= ? ORDER BY ts ASC",
            (ticker, cutoff),
        )
        return [_row_to_bar(r) for r in cur.fetchall()]


def count_recent_bars_15m(ticker: str, hours: int, db_path: str | None = None) -> int:
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT COUNT(*) FROM bars_15m WHERE ticker = ? AND ts >= ?",
            (ticker, cutoff),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def last_bar_15m_ts(ticker: str, db_path: str | None = None) -> int | None:
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT MAX(ts) FROM bars_15m WHERE ticker = ?", (ticker,)
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def prune_old_bars_15m(days: int = config.ROLLING_WINDOW_DAYS, db_path: str | None = None) -> int:
    cutoff = _now() - days * 86400
    with _conn(db_path) as cx:
        cur = cx.execute("DELETE FROM bars_15m WHERE ts < ?", (cutoff,))
        return cur.rowcount


def last_bar_ts(ticker: str, db_path: str | None = None) -> int | None:
    """Most recent bar timestamp for ticker, or None if no bars stored.

    Used by the startup backfill to decide whether to fetch the full 240h
    history or just the missing tail.
    """
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT MAX(ts) FROM bars_1h WHERE ticker = ?", (ticker,)
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def count_recent_bars(ticker: str, hours: int, db_path: str | None = None) -> int:
    """Count bars in the last ``hours`` for ticker. Cheap COUNT(*) query.

    Used by the startup backfill's density check — having a recent bar isn't
    enough; BOS needs ~240 bars in the window to fire. The freshness check
    alone is fooled by a thin Tier-1-only sliver of bars.
    """
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT COUNT(*) FROM bars_1h WHERE ticker = ? AND ts >= ?",
            (ticker, cutoff),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def prune_old_alerts(days: int = 30, db_path: str | None = None) -> int:
    """Delete alerts older than ``days``. Returns rowcount."""
    cutoff = _now() - days * 86400
    with _conn(db_path) as cx:
        cur = cx.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
        return cur.rowcount


# ---------- news_items ----------

def upsert_news_item(
    url_hash: str,
    ticker: str,
    source: str | None,
    title: str | None,
    body: str | None,
    url: str | None,
    published: int | None,
    db_path: str | None = None,
) -> None:
    with _conn(db_path) as cx:
        cx.execute(
            """
            INSERT OR IGNORE INTO news_items
                (url_hash, ticker, source, title, body, url, published, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url_hash, ticker, source, title, body, url, published, _now()),
        )


# ---------- alerts ----------

def recent_alerts_for_ticker(
    ticker: str,
    hours: int,
    db_path: str | None = None,
) -> list[sqlite3.Row]:
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM alerts WHERE ticker = ? AND created_at >= ? "
            "ORDER BY created_at DESC",
            (ticker, cutoff),
        )
        return cur.fetchall()


def alerts_today_count(
    decision: str = "EMIT",
    db_path: str | None = None,
) -> int:
    """Count alerts emitted since the start of the current UTC day."""
    now = _now()
    today_start = now - (now % 86400)
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT COUNT(*) AS c FROM alerts "
            "WHERE decision = ? AND created_at >= ?",
            (decision, today_start),
        )
        return int(cur.fetchone()["c"])


def asset_class_alerts_today(
    asset_class: str,
    decision: str = "EMIT",
    db_path: str | None = None,
) -> int:
    now = _now()
    today_start = now - (now % 86400)
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT COUNT(*) AS c FROM alerts "
            "WHERE asset_class = ? AND decision = ? AND created_at >= ?",
            (asset_class, decision, today_start),
        )
        return int(cur.fetchone()["c"])


def record_alert(
    alert: Any,
    decision: str,
    reason: str = "",
    classifier: Any | None = None,
    db_path: str | None = None,
) -> int:
    """Persist an alert + decision. Returns inserted row id.

    `alert` is duck-typed — we read attributes if present.
    """
    catalyst_type = direction = summary = None
    confidence = None
    evidence_blob = None
    if classifier is not None:
        catalyst_type = getattr(classifier, "catalyst_type", None)
        direction = getattr(classifier, "direction", None)
        confidence = getattr(classifier, "confidence", None)
        summary = getattr(classifier, "summary", None)
        ev = getattr(classifier, "evidence_quotes", None)
        if ev is not None:
            try:
                evidence_blob = json.dumps(list(ev))
            except (TypeError, ValueError):
                evidence_blob = None

    with _conn(db_path) as cx:
        cur = cx.execute(
            """
            INSERT INTO alerts
                (ticker, asset_class, score, alpha_z, r_alpha_pct,
                 catalyst_type, direction, confidence, summary, evidence,
                 decision, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                getattr(alert, "ticker", None),
                getattr(alert, "asset_class", None),
                getattr(alert, "score", None),
                getattr(alert, "alpha_z", None),
                getattr(alert, "r_alpha_pct", None),
                catalyst_type,
                direction,
                confidence,
                summary,
                evidence_blob,
                decision,
                reason,
                _now(),
            ),
        )
        return int(cur.lastrowid or 0)


# ---------- introspection helpers ----------

def all_alerts(limit: int = 100, db_path: str | None = None) -> list[sqlite3.Row]:
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()


def execute(query: str, params: Iterable[Any] = (), db_path: str | None = None) -> list[sqlite3.Row]:
    """Escape hatch for one-off introspection queries."""
    with _conn(db_path) as cx:
        cur = cx.execute(query, tuple(params))
        return cur.fetchall()


# ============ BOS suppression helpers ============

def recent_alert_exists(
    ticker: str,
    catalyst_type: str | None,
    hours: int,
    db_path: str | None = None,
) -> bool:
    """True if an EMITTED alert for this ticker + catalyst_type fired within
    the last `hours`. Used by Rule 1 (per-catalyst dedup)."""
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        if catalyst_type is None:
            cur = cx.execute(
                "SELECT 1 FROM alerts WHERE ticker = ? AND decision = 'EMIT' "
                "AND created_at >= ? LIMIT 1",
                (ticker, cutoff),
            )
        else:
            cur = cx.execute(
                "SELECT 1 FROM alerts WHERE ticker = ? AND catalyst_type = ? "
                "AND decision = 'EMIT' AND created_at >= ? LIMIT 1",
                (ticker, catalyst_type, cutoff),
            )
        return cur.fetchone() is not None


def count_same_sector_alerts(
    asset_class: str,
    hours: int,
    db_path: str | None = None,
) -> int:
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT COUNT(*) AS c FROM alerts "
            "WHERE asset_class = ? AND decision = 'EMIT' AND created_at >= ?",
            (asset_class, cutoff),
        )
        return int(cur.fetchone()["c"])


def is_top_score_in_sector(
    market: Any,
    score: float,
    hours: int = 4,
    db_path: str | None = None,
) -> bool:
    """True if `score` strictly exceeds every recent EMIT score in the same
    asset_class. Used as the carve-out for Rule 3."""
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT MAX(score) AS m FROM alerts "
            "WHERE asset_class = ? AND decision = 'EMIT' AND created_at >= ?",
            (market.asset_class, cutoff),
        )
        row = cur.fetchone()
    if row is None or row["m"] is None:
        return True
    return float(score) > float(row["m"])


def count_alerts_today(db_path: str | None = None) -> int:
    """Convenience alias matching the spec; counts EMITs since UTC midnight."""
    return alerts_today_count(decision="EMIT", db_path=db_path)


def median_score_today(db_path: str | None = None) -> float:
    """Median score among today's EMITted alerts. Used as the carve-out for
    Rule 4 (budget throttle)."""
    now = _now()
    today_start = now - (now % 86400)
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT score FROM alerts WHERE decision = 'EMIT' "
            "AND created_at >= ? AND score IS NOT NULL ORDER BY score",
            (today_start,),
        )
        scores = [float(r["score"]) for r in cur.fetchall()]
    if not scores:
        return 0.0
    return scores[len(scores) // 2]


# ============ watchlist ============

def add_to_watchlist(
    ticker: str,
    asset_class: str,
    direction_bias: str,
    score: float,
    catalyst_summary: str,
    classifier_json: str,
    swing_high_reference: float | None,
    swing_low_reference: float | None,
    swing_reference_timestamp: datetime | None,
    median_bar_range: float,
    ttl_hours: int,
    db_path: str | None = None,
) -> None:
    """Upsert a watchlist entry. If `ticker` already exists, replaces the row.
    Datetimes are stored as ISO-8601 strings (naive UTC)."""
    added_at = _now_iso()
    expires_dt = datetime.utcnow().replace(microsecond=0) + _hours(ttl_hours)
    expires_at = expires_dt.isoformat()
    swing_ts_iso = (
        swing_reference_timestamp.isoformat()
        if isinstance(swing_reference_timestamp, datetime)
        else None
    )
    with _conn(db_path) as cx:
        cx.execute(
            """
            INSERT INTO watchlist (
                ticker, asset_class, direction_bias, score, catalyst_summary,
                classifier_json, swing_high_reference, swing_low_reference,
                swing_reference_timestamp, median_bar_range,
                added_at, expires_at, last_polled_at, last_polled_price
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(ticker) DO UPDATE SET
                asset_class               = excluded.asset_class,
                direction_bias            = excluded.direction_bias,
                score                     = excluded.score,
                catalyst_summary          = excluded.catalyst_summary,
                classifier_json           = excluded.classifier_json,
                swing_high_reference      = excluded.swing_high_reference,
                swing_low_reference       = excluded.swing_low_reference,
                swing_reference_timestamp = excluded.swing_reference_timestamp,
                median_bar_range          = excluded.median_bar_range,
                added_at                  = excluded.added_at,
                expires_at                = excluded.expires_at,
                last_polled_at            = NULL,
                last_polled_price         = NULL
            """,
            (
                ticker, asset_class, direction_bias, float(score),
                catalyst_summary or "", classifier_json or "{}",
                swing_high_reference, swing_low_reference,
                swing_ts_iso, float(median_bar_range),
                added_at, expires_at,
            ),
        )


def get_watchlist_entry(ticker: str, db_path: str | None = None) -> dict | None:
    with _conn(db_path) as cx:
        cur = cx.execute("SELECT * FROM watchlist WHERE ticker = ?", (ticker,))
        row = cur.fetchone()
        return dict(row) if row else None


def remove_from_watchlist(ticker: str, db_path: str | None = None) -> None:
    """Delete watchlist row for ticker. Idempotent."""
    with _conn(db_path) as cx:
        cx.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))


def list_active_watchlist(db_path: str | None = None) -> list[dict]:
    """All watchlist rows with expires_at > now()."""
    now_iso = _now_iso()
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM watchlist WHERE expires_at > ? ORDER BY added_at ASC",
            (now_iso,),
        )
        return [dict(r) for r in cur.fetchall()]


def expire_stale_watchlist(db_path: str | None = None) -> int:
    """Delete watchlist rows whose expires_at has passed. Returns the count."""
    now_iso = _now_iso()
    with _conn(db_path) as cx:
        cur = cx.execute(
            "DELETE FROM watchlist WHERE expires_at <= ?",
            (now_iso,),
        )
        return int(cur.rowcount or 0)


def update_watchlist_poll(
    ticker: str,
    polled_price: float,
    db_path: str | None = None,
) -> None:
    with _conn(db_path) as cx:
        cx.execute(
            "UPDATE watchlist SET last_polled_at = ?, last_polled_price = ? "
            "WHERE ticker = ?",
            (_now_iso(), float(polled_price), ticker),
        )


def _hours(n: int):
    """Tiny helper so add_to_watchlist doesn't need to import timedelta."""
    from datetime import timedelta
    return timedelta(hours=int(n))


# ============ executor tables (EXECUTOR_SPEC.md §10) ============
#
# Generic insert/update used by the executor + exit engine. Column names are
# code-controlled (never user-supplied) so the f-string identifier interpolation
# is safe; all *values* are parametrized.

def insert_row(table: str, row: dict[str, Any], db_path: str | None = None) -> int:
    """Insert a dict as a row into ``table``; return the new rowid."""
    cols = list(row.keys())
    collist = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    with _conn(db_path) as cx:
        cur = cx.execute(
            f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
            tuple(row[c] for c in cols),
        )
        return int(cur.lastrowid or 0)


def update_row(table: str, row_id: int, fields: dict[str, Any], db_path: str | None = None) -> None:
    """Patch the named columns of one row by id. No-op for an empty patch."""
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _conn(db_path) as cx:
        cx.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?",
            (*fields.values(), row_id),
        )


def get_row(table: str, row_id: int, db_path: str | None = None) -> dict | None:
    with _conn(db_path) as cx:
        row = cx.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None


# ---- config_versions: get-or-create by config hash ----

def get_or_create_config_version(
    config_hash: str,
    fields: dict[str, Any],
    db_path: str | None = None,
) -> int:
    """Return the id of the config_versions row for ``config_hash``, inserting
    it (with ``fields``) the first time a given hash is seen. Idempotent — a
    threshold change yields a new hash and a new row; unchanged config reuses."""
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT id FROM config_versions WHERE config_hash = ?", (config_hash,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        payload = {"config_hash": config_hash, **fields}
        cols = ", ".join(payload.keys())
        ph = ", ".join("?" for _ in payload)
        cur = cx.execute(
            f"INSERT INTO config_versions ({cols}) VALUES ({ph})",
            tuple(payload.values()),
        )
        return int(cur.lastrowid or 0)


# ---- positions lifecycle queries (exit engine + circuit breaker) ----

def open_positions(db_path: str | None = None) -> list[dict]:
    """Positions that have not yet exited (exit_ts IS NULL)."""
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM positions WHERE exit_ts IS NULL ORDER BY open_ts ASC"
        )
        return [dict(r) for r in cur.fetchall()]


def open_position_count(db_path: str | None = None) -> int:
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE exit_ts IS NULL"
        ).fetchone()
    return int(row["c"]) if row else 0


def total_open_exposure_usd(db_path: str | None = None) -> float:
    """Sum of size_contracts × entry_avg_price across open positions."""
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT COALESCE(SUM(size_contracts * entry_avg_price), 0.0) AS e "
            "FROM positions WHERE exit_ts IS NULL"
        ).fetchone()
    return float(row["e"]) if row and row["e"] is not None else 0.0


def daily_realized_pnl_usd(db_path: str | None = None) -> float:
    """Realized PnL summed over positions that exited since UTC midnight."""
    now = _now()
    today_start = now - (now % 86400)
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd), 0.0) AS p FROM positions "
            "WHERE exit_ts IS NOT NULL AND exit_ts >= ?",
            (today_start,),
        ).fetchone()
    return float(row["p"]) if row and row["p"] is not None else 0.0


def trades_opened_today(db_path: str | None = None) -> int:
    now = _now()
    today_start = now - (now % 86400)
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE open_ts >= ?",
            (today_start,),
        ).fetchone()
    return int(row["c"]) if row else 0


def consecutive_losses(db_path: str | None = None) -> int:
    """Count of consecutive losing closes at the tail of the exit history.
    Resets to 0 the moment a non-loss (>=0 PnL) close is encountered."""
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT realized_pnl_usd FROM positions "
            "WHERE exit_ts IS NOT NULL ORDER BY exit_ts DESC LIMIT 100"
        )
        rows = cur.fetchall()
    streak = 0
    for r in rows:
        pnl = r["realized_pnl_usd"]
        if pnl is not None and float(pnl) < 0:
            streak += 1
        else:
            break
    return streak


def position_by_coi(client_order_index: int, db_path: str | None = None) -> dict | None:
    """Look up a position via the entry order's client_order_index — used by
    boot reconciliation to dedupe against re-derived COIs."""
    with _conn(db_path) as cx:
        row = cx.execute(
            "SELECT p.* FROM positions p JOIN orders o ON o.execution_id = p.execution_id "
            "WHERE o.client_order_index = ? AND o.leg = 'entry' LIMIT 1",
            (client_order_index,),
        ).fetchone()
        return dict(row) if row else None
