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
