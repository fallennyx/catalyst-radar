# Catalyst Radar

A single-process Python engine that scans a leveraged crypto/equity/commodity
universe every five minutes, ranks unusual movers by a composite vol-normalized
score, fetches catalysts from asset-routed news sources, classifies them with
Claude Haiku, applies a four-rule suppression chain, and pushes the survivors
to Telegram.

## Architecture

```
universe → ranker → catalysts → classifier → suppression → telegram → sleep(300)
```

Synchronous, single-process, no async, no microservices, no message queues.
SQLite for state. ~40-line main loop. See [the spec](./CatalystRadar_Cowork_Spec.md)
for design rationale.

## Quick start

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env   # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LIGHTER_API_KEY

# 2. Run with Docker (recommended)
docker compose up -d --build
docker compose logs -f radar

# 3. Or run locally
pip install -e ".[dev]"
python -m radar.main
```

## Tests

```bash
pip install -e ".[dev]"
pytest tests/
```

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

The fetcher routes by asset class:
- *crypto* → CoinGecko `/market_chart/range` (free, no API key, ~90-day hourly window)
- *equity / commodity* → yfinance `1h` bars (free, ~30-60-day intraday window; commodity tickers are auto-mapped to futures contracts: `WTI` → `CL=F`, `XAU` → `GC=F`, …)

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
cadence, ranker weights, suppression thresholds, LLM model, RSS feeds.

## Deploy

Set `RADAR_HOST=user@host` then run `./deploy.sh`. The script rsyncs the repo
(excluding `.git/` and `data/`) to `/opt/catalyst-radar/` and rebuilds the
container.
