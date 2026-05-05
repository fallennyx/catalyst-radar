"""Tests for the GDELT news fetcher.

GDELT is never hit in tests — _session is patched.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from radar import fetch_news


# ---------- helpers ----------

def _gdelt_payload(articles):
    return {"articles": articles}


def _mock_session(payload, status=200):
    sess = MagicMock()
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    sess.get.return_value = response
    return sess


# ---------- date parsing ----------

def test_to_dt_accepts_date_only():
    dt = fetch_news._to_dt("2024-01-15")
    assert dt.tzinfo is timezone.utc
    assert dt.year == 2024 and dt.month == 1 and dt.day == 15


def test_to_dt_accepts_isoformat():
    dt = fetch_news._to_dt("2024-01-15T12:30:00Z")
    assert dt.hour == 12 and dt.minute == 30


def test_to_dt_rejects_garbage():
    with pytest.raises(ValueError):
        fetch_news._to_dt("not a date")


def test_gdelt_dt_format():
    dt = datetime(2024, 1, 15, 12, 30, 45, tzinfo=timezone.utc)
    assert fetch_news._gdelt_dt(dt) == "20240115123045"


def test_seendate_to_unix():
    ts = fetch_news._seendate_to_unix("20240115T123045Z")
    expected = int(datetime(2024, 1, 15, 12, 30, 45, tzinfo=timezone.utc).timestamp())
    assert ts == expected


def test_seendate_to_unix_handles_garbage():
    assert fetch_news._seendate_to_unix("") == 0
    assert fetch_news._seendate_to_unix("not-a-date") == 0


# ---------- query building ----------

def test_build_query_known_ticker_with_whitelist():
    q = fetch_news._build_query("BTC", ["reuters.com", "coindesk.com"])
    assert "Bitcoin" in q
    assert "domain:reuters.com" in q
    assert "domain:coindesk.com" in q


def test_build_query_unknown_ticker_returns_none():
    assert fetch_news._build_query("ZZZNOTAREAL", ["reuters.com"]) is None


def test_build_query_no_whitelist():
    q = fetch_news._build_query("BTC", None)
    assert "domain:" not in q


# ---------- fetch_ticker_news ----------

def _make_articles():
    return [
        {
            "url": "https://reuters.com/btc-1",
            "domain": "reuters.com",
            "title": "BTC tops $100k",
            "seendate": "20240115T120000Z",
        },
        {
            "url": "https://coindesk.com/btc-2",
            "domain": "coindesk.com",
            "title": "ETF inflows surge",
            "seendate": "20240115T130000Z",
        },
        {
            # duplicate URL — should be deduped
            "url": "https://reuters.com/btc-1",
            "domain": "reuters.com",
            "title": "BTC tops $100k (dup)",
            "seendate": "20240115T120100Z",
        },
    ]


def test_fetch_ticker_news_parses_articles(monkeypatch):
    monkeypatch.setattr(
        fetch_news, "_session",
        lambda: _mock_session(_gdelt_payload(_make_articles())),
    )
    items = fetch_news.fetch_ticker_news(
        "BTC",
        start=datetime(2024, 1, 15, tzinfo=timezone.utc),
        end=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )
    # 3 articles, 1 duplicate URL → 2 unique
    assert len(items) == 2
    assert items[0]["ticker"] == "BTC"
    assert items[0]["url"].startswith("https://")
    assert items[0]["published"]  # non-empty ISO string


def test_fetch_ticker_news_skips_unmapped_ticker(monkeypatch):
    sess = MagicMock()
    monkeypatch.setattr(fetch_news, "_session", lambda: sess)
    items = fetch_news.fetch_ticker_news(
        "ZZZNOTAREAL",
        start=datetime(2024, 1, 15, tzinfo=timezone.utc),
        end=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )
    assert items == []
    sess.get.assert_not_called()


def test_fetch_ticker_news_empty_on_http_error(monkeypatch):
    monkeypatch.setattr(fetch_news, "_session", lambda: _mock_session({}, status=429))
    items = fetch_news.fetch_ticker_news(
        "BTC",
        start=datetime(2024, 1, 15, tzinfo=timezone.utc),
        end=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )
    assert items == []


def test_fetch_ticker_news_passes_window_to_gdelt(monkeypatch):
    sess = _mock_session(_gdelt_payload([]))
    monkeypatch.setattr(fetch_news, "_session", lambda: sess)
    fetch_news.fetch_ticker_news(
        "BTC",
        start=datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc),
        end=datetime(2024, 1, 16, 0, 0, 0, tzinfo=timezone.utc),
    )
    _, kwargs = sess.get.call_args
    params = kwargs["params"]
    assert params["STARTDATETIME"] == "20240115000000"
    assert params["ENDDATETIME"] == "20240116000000"


# ---------- orchestration ----------

def test_fetch_news_routes_per_ticker(monkeypatch):
    calls = []

    def fake_one(ticker, start, end, max_records=75, domain_whitelist=None):
        calls.append(ticker)
        return [{"ticker": ticker, "title": f"{ticker} news",
                 "url": f"https://x/{ticker}", "published": "2024-01-15T12:00:00Z",
                 "source": "x", "body": ""}]

    monkeypatch.setattr(fetch_news, "fetch_ticker_news", fake_one)
    items = fetch_news.fetch_news(
        ["BTC", "ETH", "NOT_A_TICKER"],
        start=datetime(2024, 1, 15, tzinfo=timezone.utc),
        end=datetime(2024, 1, 16, tzinfo=timezone.utc),
        sleep_between=0,
    )
    assert calls == ["BTC", "ETH"]
    assert len(items) == 2


def test_write_json_round_trips(tmp_path):
    items = [
        {"ticker": "BTC", "title": "a", "url": "u1", "source": "x", "body": "", "published": "2024-01-15T12:00:00Z"},
        {"ticker": "ARB", "title": "b", "url": "u2", "source": "x", "body": "", "published": "2024-01-15T11:00:00Z"},
    ]
    out = tmp_path / "news.json"
    fetch_news.write_json(items, str(out))
    with open(out) as f:
        loaded = json.load(f)
    # sorted by (ticker, published) → ARB comes first alphabetically
    assert loaded[0]["ticker"] == "ARB"
    assert loaded[1]["ticker"] == "BTC"


def test_news_archive_loads_into_replay(tmp_path, monkeypatch):
    """End-to-end: fetch_news → write_json → replay.load_news_archive."""
    monkeypatch.setattr(
        fetch_news, "_session",
        lambda: _mock_session(_gdelt_payload(_make_articles())),
    )
    items = fetch_news.fetch_ticker_news(
        "BTC",
        start=datetime(2024, 1, 15, tzinfo=timezone.utc),
        end=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )
    out = tmp_path / "news.json"
    fetch_news.write_json(items, str(out))

    from radar import replay
    archive = replay.load_news_archive(str(out))
    assert "BTC" in archive
    assert all(n.ticker == "BTC" for n in archive["BTC"])
    # published was parsed from ISO into unix int
    assert all(isinstance(n.published, int) and n.published > 0 for n in archive["BTC"])
