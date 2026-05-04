"""SQLite persistence layer. No ORM — sqlite3 stdlib + parametrized queries."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

from . import config

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
"""


def _now() -> int:
    return int(time.time())


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


def recent_bars(ticker: str, hours: int, db_path: str | None = None) -> list[sqlite3.Row]:
    cutoff = _now() - hours * 3600
    with _conn(db_path) as cx:
        cur = cx.execute(
            "SELECT * FROM bars_1h WHERE ticker = ? AND ts >= ? ORDER BY ts ASC",
            (ticker, cutoff),
        )
        return cur.fetchall()


def prune_old_bars(days: int = config.ROLLING_WINDOW_DAYS, db_path: str | None = None) -> int:
    cutoff = _now() - days * 86400
    with _conn(db_path) as cx:
        cur = cx.execute("DELETE FROM bars_1h WHERE ts < ?", (cutoff,))
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
    today_start = int(time.time()) - (int(time.time()) % 86400)
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
    today_start = int(time.time()) - (int(time.time()) % 86400)
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
