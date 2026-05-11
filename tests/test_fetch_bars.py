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


# ---------- Binance ----------

def _binance_klines(num: int = 3, base_ts_ms: int = 1_705_276_800_000):
    """num hourly klines: each is [open_time_ms, o, h, l, c, vol, close_time, quote_vol, ...]"""
    return [
        [
            base_ts_ms + i * 3_600_000,
            f"{100.0 + i:.2f}",       # open
            f"{105.0 + i:.2f}",       # high
            f"{99.0 + i:.2f}",        # low
            f"{102.0 + i:.2f}",       # close
            f"{1000.0 * (i+1):.2f}",  # base volume
            base_ts_ms + (i + 1) * 3_600_000 - 1,  # close_time
            f"{102_000.0 * (i+1):.2f}",  # quote volume (USDT)
            500,                       # trades
            "0", "0", "0",
        ]
        for i in range(num)
    ]


def test_fetch_crypto_binance_parses_klines(monkeypatch):
    monkeypatch.setattr(
        fetch_bars, "_session",
        lambda: _mock_session(_binance_klines(3)),
    )
    rows = fetch_bars.fetch_crypto_binance("BTC", days=1)
    assert len(rows) == 3
    # Real OHLC, not synthesized
    assert rows[0]["open"] == 100.0
    assert rows[0]["high"] == 105.0
    assert rows[0]["low"] == 99.0
    assert rows[0]["price"] == 102.0
    assert rows[0]["asset_class"] == "crypto_t1"


def test_fetch_crypto_binance_uses_symbol_override(monkeypatch):
    sess = _mock_session(_binance_klines(2))
    monkeypatch.setattr(fetch_bars, "_session", lambda: sess)
    fetch_bars.fetch_crypto_binance("PEPE", days=1)
    _, kwargs = sess.get.call_args
    assert kwargs["params"]["symbol"] == "1000PEPEUSDT"


def test_fetch_crypto_binance_default_guesses_usdt_pair(monkeypatch):
    """Tickers not in BINANCE_SYMBOLS get a `<TICKER>USDT` symbol guess."""
    sess = _mock_session(_binance_klines(2))
    monkeypatch.setattr(fetch_bars, "_session", lambda: sess)
    fetch_bars.fetch_crypto_binance("HYPE", days=1)
    _, kwargs = sess.get.call_args
    assert kwargs["params"]["symbol"] == "HYPEUSDT"


def test_fetch_crypto_router_prefers_coinbase(monkeypatch):
    """Router order: Coinbase → Bybit → Binance → CoinGecko. First win short-circuits."""
    calls = {"cb": [], "by": [], "bi": [], "cg": []}
    monkeypatch.setattr(fetch_bars, "fetch_crypto_coinbase",
                        lambda t, d, end_ts=None: calls["cb"].append(t) or [{"ticker": t}])
    monkeypatch.setattr(fetch_bars, "fetch_crypto_bybit",
                        lambda t, d, end_ts=None: calls["by"].append(t) or [{"ticker": t}])
    monkeypatch.setattr(fetch_bars, "fetch_crypto_binance",
                        lambda t, d, end_ts=None: calls["bi"].append(t) or [{"ticker": t}])
    monkeypatch.setattr(fetch_bars, "fetch_crypto_hourly",
                        lambda t, d, end_ts=None: calls["cg"].append(t) or [{"ticker": t}])

    fetch_bars.fetch_crypto("BTC", days=1)
    assert calls == {"cb": ["BTC"], "by": [], "bi": [], "cg": []}


def test_fetch_crypto_router_falls_back_through_chain(monkeypatch):
    """Empty-result sources are skipped in order until something returns rows."""
    calls = {"cb": [], "by": [], "bi": [], "cg": []}
    monkeypatch.setattr(fetch_bars, "fetch_crypto_coinbase",
                        lambda t, d, end_ts=None: calls["cb"].append(t) or [])
    monkeypatch.setattr(fetch_bars, "fetch_crypto_bybit",
                        lambda t, d, end_ts=None: calls["by"].append(t) or [])
    monkeypatch.setattr(fetch_bars, "fetch_crypto_binance",
                        lambda t, d, end_ts=None: calls["bi"].append(t) or [])
    monkeypatch.setattr(fetch_bars, "fetch_crypto_hourly",
                        lambda t, d, end_ts=None: calls["cg"].append(t) or [{"ticker": t}])

    fetch_bars.fetch_crypto("HYPE", days=1)
    assert calls == {"cb": ["HYPE"], "by": ["HYPE"], "bi": ["HYPE"], "cg": ["HYPE"]}


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
                        lambda t, d, end_ts=None: crypto_calls.append(t) or [])
    monkeypatch.setattr(fetch_bars, "fetch_yfinance_hourly",
                        lambda t, d, end_ts=None: equity_calls.append(t) or [])
    fetch_bars.ROUTES["crypto_t1"] = fetch_bars.fetch_crypto_hourly
    fetch_bars.ROUTES["crypto_t2"] = fetch_bars.fetch_crypto_hourly
    fetch_bars.ROUTES["crypto_meme"] = fetch_bars.fetch_crypto_hourly
    fetch_bars.ROUTES["equity"] = fetch_bars.fetch_yfinance_hourly
    fetch_bars.ROUTES["commodity"] = fetch_bars.fetch_yfinance_hourly

    fetch_bars.fetch_universe(["BTC", "NVDA", "XAU"], days=1, sleep_between=0)

    assert crypto_calls == ["BTC"]
    assert sorted(equity_calls) == ["NVDA", "XAU"]


def test_fetch_universe_skips_unknown_ticker(monkeypatch):
    monkeypatch.setattr(fetch_bars, "fetch_crypto_hourly", lambda t, d, end_ts=None: [])
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


# ============================================================================
# is_fetchable + TICKER_ROUTE_OVERRIDES
# ============================================================================

def test_is_fetchable_returns_true_for_any_crypto():
    """Unmapped crypto tickers report fetchable — fetch_crypto's fallback
    chain handles them via {TICKER}USDT defaults."""
    assert fetch_bars.is_fetchable("BTC", "crypto_t1") is True
    assert fetch_bars.is_fetchable("ETH", "crypto_t1") is True
    # Unmapped t2 names that DO trade on Binance/Coinbase
    assert fetch_bars.is_fetchable("UNI", "crypto_t2") is True
    assert fetch_bars.is_fetchable("PYTH", "crypto_t2") is True
    assert fetch_bars.is_fetchable("WLD", "crypto_t2") is True
    # Even exotic ones return True; per-ticker failure isolation handles misses
    assert fetch_bars.is_fetchable("FARTCOIN", "crypto_meme") is True


def test_is_fetchable_for_1000_prefix_memes():
    """Lighter's 1000PEPE / 1000BONK ticker symbols map to Binance directly."""
    assert fetch_bars.BINANCE_SYMBOLS["1000PEPE"] == "1000PEPEUSDT"
    assert "1000BONK" in fetch_bars.BINANCE_SYMBOLS
    assert "1000FLOKI" in fetch_bars.BINANCE_SYMBOLS
    assert "1000SHIB" in fetch_bars.BINANCE_SYMBOLS
    assert fetch_bars.is_fetchable("1000PEPE", "crypto_meme") is True
    assert fetch_bars.is_fetchable("1000FLOKI", "crypto_meme") is True


def test_is_fetchable_paxg_override():
    """PAXG is commodity-classed but routes via crypto thanks to the override."""
    assert fetch_bars.TICKER_ROUTE_OVERRIDES["PAXG"] == "crypto_t1"
    assert fetch_bars.is_fetchable("PAXG", "commodity") is True


def test_is_fetchable_equity_passthrough():
    """Equities always fetchable — yfinance accepts raw symbols."""
    assert fetch_bars.is_fetchable("AAPL", "equity") is True
    assert fetch_bars.is_fetchable("HYUNDAIUSD", "equity") is True


def test_is_fetchable_forex_requires_mapping():
    """Forex pairs need an explicit YFINANCE_SYMBOLS entry (the =X suffix)."""
    assert fetch_bars.is_fetchable("EURUSD", "forex") is True
    assert fetch_bars.is_fetchable("USDJPY", "forex") is True


def test_is_fetchable_commodity_without_mapping_is_unfetchable():
    """Commodities not in YFINANCE_SYMBOLS or overrides remain unfetchable."""
    assert fetch_bars.is_fetchable("XAU", "commodity") is True  # mapped to GC=F
    assert fetch_bars.is_fetchable("NATGAS", "commodity") is True  # mapped to NG=F
    assert fetch_bars.is_fetchable("FAKEMETAL", "commodity") is False  # no mapping
