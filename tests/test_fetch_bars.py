"""Tests for the historical bar fetcher.

We never hit the real CoinGecko / yfinance APIs in tests — both backends are
patched to return canned payloads, so this exercises only our parsing,
hourly bucketing, pct calculations, and CSV emission.
"""

import csv
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from radar import fetch_bars


# ---------- helpers ----------

def _coingecko_payload():
    """Two hours of synthetic 5-min ticks at 100 → 110 → 121 (10% per hour)."""
    base_ts_ms = 1_705_276_800_000  # 2024-01-15T00:00:00Z
    prices, volumes = [], []
    # Hour 0: closes at 100
    for i in range(12):
        prices.append([base_ts_ms + i * 5 * 60_000, 100.0])
        volumes.append([base_ts_ms + i * 5 * 60_000, 1_000_000.0])
    # Hour 1: closes at 110
    for i in range(12):
        prices.append([base_ts_ms + 3_600_000 + i * 5 * 60_000, 110.0])
        volumes.append([base_ts_ms + 3_600_000 + i * 5 * 60_000, 1_500_000.0])
    # Hour 2: closes at 121
    for i in range(12):
        prices.append([base_ts_ms + 7_200_000 + i * 5 * 60_000, 121.0])
        volumes.append([base_ts_ms + 7_200_000 + i * 5 * 60_000, 2_000_000.0])
    return {"prices": prices, "total_volumes": volumes}


def _mock_session(json_payload, status=200):
    sess = MagicMock()
    response = MagicMock()
    response.status_code = status
    response.json.return_value = json_payload
    response.text = ""
    sess.get.return_value = response
    return sess


# ---------- CoinGecko ----------

def test_fetch_crypto_returns_hourly_buckets(monkeypatch):
    monkeypatch.setattr(fetch_bars, "_session", lambda: _mock_session(_coingecko_payload()))
    rows = fetch_bars.fetch_crypto_hourly("BTC", days=1)
    assert len(rows) == 3
    assert rows[0]["ticker"] == "BTC"
    assert rows[0]["asset_class"] == "crypto_t1"
    assert rows[0]["price"] == 100.0
    assert rows[1]["price"] == 110.0


def test_fetch_crypto_computes_pct_1h(monkeypatch):
    monkeypatch.setattr(fetch_bars, "_session", lambda: _mock_session(_coingecko_payload()))
    rows = fetch_bars.fetch_crypto_hourly("BTC", days=1)
    # first bucket has no previous → 0
    assert rows[0]["pct_1h"] == 0.0
    # 100 → 110 = +10%
    assert rows[1]["pct_1h"] == pytest.approx(10.0, rel=1e-3)
    # 110 → 121 = +10%
    assert rows[2]["pct_1h"] == pytest.approx(10.0, rel=1e-3)


def test_fetch_crypto_skips_unmapped_ticker(monkeypatch):
    sess = MagicMock()
    monkeypatch.setattr(fetch_bars, "_session", lambda: sess)
    rows = fetch_bars.fetch_crypto_hourly("USELESS", days=1)
    assert rows == []
    sess.get.assert_not_called()


def test_fetch_crypto_returns_empty_on_http_error(monkeypatch):
    monkeypatch.setattr(fetch_bars, "_session", lambda: _mock_session({}, status=429))
    rows = fetch_bars.fetch_crypto_hourly("BTC", days=1)
    assert rows == []


# ---------- yfinance ----------

class _FakeIndexEntry:
    """Mimics a pandas Timestamp — what's used is just .timestamp()."""
    def __init__(self, ts_sec):
        self._ts = ts_sec

    def timestamp(self):
        return self._ts


class _FakeDataFrame:
    """Tiny stand-in for the pd.DataFrame yfinance returns. Only needs:
       - len()
       - .columns (with optional .levels)
       - .droplevel
       - .iterrows() yielding (index, row_dict-like)"""
    def __init__(self, rows):
        self._rows = rows
        self.columns = SimpleNamespace()  # no multiindex

    def __len__(self):
        return len(self._rows)

    def droplevel(self, *args, **kwargs):
        return self

    def iterrows(self):
        for ts, payload in self._rows:
            yield _FakeIndexEntry(ts), payload


def test_fetch_yfinance_parses_bars(monkeypatch):
    rows_in = [
        (1_705_276_800, {"Close": 900.0, "Volume": 100_000}),
        (1_705_280_400, {"Close": 945.0, "Volume": 200_000}),
    ]
    fake_df = _FakeDataFrame(rows_in)

    fake_yf = MagicMock()
    fake_yf.download.return_value = fake_df
    monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

    rows = fetch_bars.fetch_yfinance_hourly("NVDA", days=1)
    assert len(rows) == 2
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["asset_class"] == "equity"
    assert rows[1]["pct_1h"] == pytest.approx(5.0, rel=1e-3)
    # vol_usd = shares * close
    assert rows[1]["volume_24h_usd"] == pytest.approx(200_000 * 945.0)


def test_fetch_yfinance_uses_symbol_override(monkeypatch):
    fake_yf = MagicMock()
    fake_yf.download.return_value = _FakeDataFrame([])
    monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

    fetch_bars.fetch_yfinance_hourly("XAU", days=1)
    args, kwargs = fake_yf.download.call_args
    # First positional arg is the resolved yfinance symbol
    assert args[0] == "GC=F"


# ---------- routing + write ----------

def test_fetch_universe_routes_by_class(monkeypatch):
    crypto_calls, equity_calls = [], []
    monkeypatch.setattr(fetch_bars, "fetch_crypto_hourly",
                        lambda t, d: crypto_calls.append(t) or [])
    monkeypatch.setattr(fetch_bars, "fetch_yfinance_hourly",
                        lambda t, d: equity_calls.append(t) or [])
    fetch_bars.ROUTES["crypto_t1"] = fetch_bars.fetch_crypto_hourly
    fetch_bars.ROUTES["crypto_t2"] = fetch_bars.fetch_crypto_hourly
    fetch_bars.ROUTES["crypto_meme"] = fetch_bars.fetch_crypto_hourly
    fetch_bars.ROUTES["equity"] = fetch_bars.fetch_yfinance_hourly
    fetch_bars.ROUTES["commodity"] = fetch_bars.fetch_yfinance_hourly

    fetch_bars.fetch_universe(["BTC", "NVDA", "XAU"], days=1, sleep_between=0)

    assert crypto_calls == ["BTC"]
    assert sorted(equity_calls) == ["NVDA", "XAU"]


def test_fetch_universe_skips_unknown_ticker(monkeypatch):
    monkeypatch.setattr(fetch_bars, "fetch_crypto_hourly", lambda t, d: [])
    rows = fetch_bars.fetch_universe(["NOT_A_REAL_TICKER"], days=1, sleep_between=0)
    assert rows == []


def test_write_csv_round_trips(tmp_path):
    out = tmp_path / "bars.csv"
    rows = [
        {
            "ts": "2024-01-15T01:00:00Z", "ticker": "BTC", "asset_class": "crypto_t1",
            "max_leverage": 10, "price": 42000, "volume_24h_usd": 5_000_000,
            "oi_usd": 0, "funding_1h": 0, "pct_24h": 0.5, "pct_1h": 0.1,
        },
        {
            "ts": "2024-01-15T00:00:00Z", "ticker": "ETH", "asset_class": "crypto_t1",
            "max_leverage": 10, "price": 2500, "volume_24h_usd": 2_000_000,
            "oi_usd": 0, "funding_1h": 0, "pct_24h": 0.4, "pct_1h": 0.05,
        },
    ]
    fetch_bars.write_csv(rows, str(out))

    with open(out) as f:
        reader = list(csv.DictReader(f))
    # write_csv sorts by (ts, ticker) so ETH (00:00) comes before BTC (01:00)
    assert reader[0]["ticker"] == "ETH"
    assert reader[1]["ticker"] == "BTC"
    assert reader[0]["asset_class"] == "crypto_t1"


def test_csv_output_is_loadable_by_replay(tmp_path, monkeypatch):
    """End-to-end: fetch_bars → write_csv → replay.load_bars on the same file."""
    monkeypatch.setattr(fetch_bars, "_session", lambda: _mock_session(_coingecko_payload()))
    rows = fetch_bars.fetch_crypto_hourly("BTC", days=1)
    out = tmp_path / "bars.csv"
    fetch_bars.write_csv(rows, str(out))

    from radar import replay
    bars = replay.load_bars(str(out))
    assert len(bars) == 3  # three hourly buckets from the fixture
    for ts, markets in bars.items():
        assert len(markets) == 1
        assert markets[0].ticker == "BTC"
