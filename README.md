# Catalyst Radar

A single-process Python engine that scans the live Lighter DEX perp universe
(~163 markets across crypto / equity / commodity / forex) every five minutes,
ranks unusual movers by a composite vol-normalized score, fetches catalysts
from asset-routed news sources, classifies them with Claude Haiku, applies a
five-rule suppression chain (BOS structural break is Rule 0), and pushes
survivors to Telegram with a deterministic SL/TP plan.

## Architecture

Three cooperating asyncio loops in one process:

```
Tier 1 (5 min)  universe → ranker → catalysts → classifier → suppression
                  → EMIT (BOS confirmed) or WATCHLIST (BOS pending) or DROP

Tier 2 (60 s)   poll active watchlist tickers for live mark-price crosses
                  against stored 4h swing references → promote to EMIT on
                  confirmed cross + 1h range expansion

Tier 3 (1 h)    Telegram heartbeat: active watchlist, recent top movers,
                  universe size, last-scan age (doubles as engine-alive signal)
```

On boot, before the loops start, the engine runs a **gap-aware backfill** of
hourly bars (Coinbase → Bybit → Binance → CoinGecko for crypto; yfinance for
equity/commodity/forex) so the BOS engine can fire on cycle 1 instead of
waiting ~5.5 days. Restarts on a populated DB skip in ~5 seconds. SQLite
auto-prunes to 30 days once per day from the Tier 1 loop.

Single Python process. No threading. No microservices. No web dashboard.
SQLite for state. See [`CLAUDE.md`](./CLAUDE.md) for the full design guide
and [`BOS_FILTER_NOTES.md`](./BOS_FILTER_NOTES.md) for the BOS engine rationale.

## Quick start

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env   # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 2. Verify Telegram works before deploying anywhere
python scripts/telegram_smoketest.py

# 3. Run with Docker (recommended for prod)
docker compose up -d --build
docker compose logs -f radar

# 4. Or run locally
pip install -e ".[dev]"
python -m radar.main
```

## Tests

```bash
pip install -e ".[dev]"
pytest tests/                          # 175 tests
pytest tests/test_main.py -x           # one module
```

All external services (Coinbase, Bybit, Binance, CoinGecko, yfinance, GDELT,
Anthropic, Telegram, Lighter SDK) are mocked. Tests use the `tmp_db` fixture
for SQLite isolation.

## Replay against historical data

`radar/replay.py` walks the engine through historical bars hour-by-hour without
touching Lighter, RSS, or Telegram. It uses a separate SQLite DB (`data/replay.db`
by default, wiped on each run) so production state stays untouched.

```bash
# Run against the bundled sample (one trading day, 3 tickers, 4 news items):
python -m radar.replay --bars data/sample_bars.csv \
                       --news data/sample_news_archive.json \
                       --no-classify
# → {"cycles": 14, "emitted": 2, "dropped": 0}
```

**Bars CSV format** — hourly snapshots (one row per ticker per hour):

```
ts,ticker,asset_class,max_leverage,price,volume_24h_usd,oi_usd,funding_1h,pct_24h,pct_1h
2024-01-15T11:00:00Z,ARB,crypto_t2,10,2.10,250000000,260000000,0.00045,12.5,11.70
```

`ts` accepts either ISO-8601 with `Z` or unix integers. `asset_class` must
match one of the buckets in [`config.ASSET_CLASSES`](./radar/config.py).

**News archive JSON format** — flat list:

```json
[
  {"ticker": "ARB", "source": "DefiLlama", "title": "...",
   "body": "...", "url": "...", "published": "2024-01-15T10:30:00Z"}
]
```

**Sourcing historical bars (built-in fetcher):**

```bash
# Pull the last 7 days of hourly bars for the full universe → data/bars.csv
python -m radar.fetch_bars --days 7

# Or a subset:
python -m radar.fetch_bars --tickers BTC,ETH,ARB --days 14 --out data/btc_eth_arb.csv
```

The fetcher routes by asset class via `fetch_bars.ROUTES`:
- *crypto* → `fetch_crypto`: Coinbase Exchange → Bybit v5 → Binance klines → CoinGecko fallback chain (returns on first non-empty source; real OHLC from the first three; CoinGecko synthesizes from 5-min ticks)
- *equity* → yfinance `1h` bars with the raw ticker (free, ~30–60-day intraday window)
- *commodity* → yfinance with futures-symbol overrides (`XAU` → `GC=F`, `WTI` → `CL=F`, `NATGAS` → `NG=F`, etc.)
- *forex* → yfinance with `=X` suffix (`EURUSD` → `EURUSD=X`, all 8 Lighter forex pairs auto-mapped)

`TICKER_ROUTE_OVERRIDES` lets specific tickers override their class-based
route — e.g. PAXG is commodity-classified but trades on Binance/CoinGecko, so
the override sends it through `fetch_crypto`.

Open interest and funding aren't available from these free sources — they're written as 0 in the CSV. The ranker tolerates missing OI/funding (those z-score components contribute 0).

**Sourcing historical news (built-in fetcher):**

```bash
# Pull a date range of news for selected tickers from GDELT (free, no key)
python -m radar.fetch_news --tickers BTC,ETH,ARB \
    --start 2024-01-15 --end 2024-01-22 \
    --out data/news_archive.json
```

GDELT covers ~2017-present at ~15-min resolution. The fetcher applies a small
finance-source whitelist (Reuters, Bloomberg, CNBC, CoinDesk, The Block) by
default to keep the archive lean — pass `--no-whitelist` to broaden coverage.
Per-ticker query terms are tuned in `QUERY_TERMS` in `radar/fetch_news.py`.

If you don't want to bother with news, omit `--news` from the replay invocation
and pass `--no-classify` — the ranker, BTC-beta gate, and suppression chain
are still fully validated.

**Putting it together — full historical replay in three commands:**

```bash
python -m radar.fetch_bars --tickers BTC,ETH,ARB --days 14 --out data/bars.csv
python -m radar.fetch_news --tickers BTC,ETH,ARB --start 2024-01-15 --end 2024-01-29 --out data/news.json
python -m radar.replay --bars data/bars.csv --news data/news.json --no-classify
```

**Programmatic API:**

```python
from radar.replay import replay
counts = replay(
    bars_csv="my_bars.csv",
    news_json="my_news.json",
    classify=True,           # set False to skip Anthropic API spend
    emit_fn=lambda alert, classification: print(alert.ticker, alert.score),
)
```

## Tuning

All knobs live in [`radar/config.py`](./radar/config.py): asset universe,
cadence, ranker weights, suppression thresholds, LLM model, RSS feeds. New
operational sections:

- `BACKFILL_*` — startup gap-aware backfill behavior (enabled, per-ticker
  timeout, skip-if-fresh threshold)
- `PRUNE_*` — retention (default: 30 days for bars + alerts, pruned once per day)
- `HOURLY_REPORT_*` — Tier 3 heartbeat cadence + content caps

## Deploy

See [`deploy-quickref.md`](./deploy-quickref.md) for the full walkthrough
(one-time server setup, SSH alias, env check, deploy, log greps, troubleshooting).

TL;DR after one-time setup:

```bash
RADAR_HOST=radar ./deploy.sh
```

The script rsyncs the repo (excluding `.git/` and `data/`) to
`/opt/catalyst-radar/` and runs `docker compose up -d --build`. The host's
`data/radar.db` survives redeploys via the `./data:/app/data` Docker volume
mount, so subsequent boots just top off the small gap created during rebuild.
