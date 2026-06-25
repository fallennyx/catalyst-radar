# Catalyst Radar

A single-process Python engine that scans the live Lighter DEX perp universe
(~163 markets across crypto / equity / commodity / forex) every minute, ranks
unusual movers by a composite vol-normalized score, fetches catalysts from
asset-routed news sources, classifies them with an LLM (Groq Llama 3.3 70B;
Gemini / Haiku fallback), confirms a structural break of structure (BOS), and
pushes survivors to Telegram with a deterministic SL/TP plan — and, as of v1 of
the **execution layer**, sizes and (optionally) places a real risk-managed
position on Lighter.

> **Disclaimer.** This is a personal research project, not financial advice and
> not a product. It places real-money orders only when explicitly armed
> (`EXECUTOR_LIVE=True`), and that live path is **unverified** (see
> [Going live](#going-live--g0-verification)). Trading perps is risky and you can
> lose money. Use at your own risk; no warranty (see [LICENSE](./LICENSE)).

## Architecture

Four cooperating asyncio loops in one process:

```
Tier 1 (60 s)   universe → ranker → catalysts → classifier → adjudicator →
                  suppression → EMIT / WATCHLIST / DROP. On EMIT, the executor
                  hook sizes + captures + (optionally) places a position.

Tier 2 (60 s)   poll active watchlist tickers for live mark-price crosses
                  against stored 4h swing references → promote to EMIT on
                  confirmed cross + range expansion (also runs the executor hook)

Tier 3 (1 h)    Telegram heartbeat: active watchlist, recent top movers,
                  universe size, last-scan age (doubles as engine-alive signal)

Exit engine     mark every open position per-minute, apply the asymmetric +1h
  (60 s)          exit rule, write the intrabar dataset (simulated in shadow mode)
```

On boot, before the loops start, the engine runs a **gap-aware backfill** of
hourly bars (Coinbase → Bybit → Binance → CoinGecko for crypto; yfinance for
equity/commodity/forex) so the BOS engine can fire on cycle 1 instead of
waiting ~5.5 days. Restarts on a populated DB skip in ~5 seconds. SQLite
auto-prunes to 30 days once per day from the Tier 1 loop.

Single Python process, single asyncio loop (the only threading is the sync↔async
bridge in `lighter_exec`). No microservices. No web dashboard. SQLite for state.
See [`CLAUDE.md`](./CLAUDE.md) for the full design guide,
[`BOS_FILTER_NOTES.md`](./BOS_FILTER_NOTES.md) for the BOS engine rationale, and
[`EXECUTOR_SPEC.md`](./EXECUTOR_SPEC.md) for the execution-layer build spec.

## Execution layer

Each EMIT is turned into a sized, risk-managed position by `radar/executor.py`,
behind a **two-stage master switch** in `radar/config.py`:

- **`EXECUTOR_ENABLED`** (default `True`) — run the full decision + data-capture
  pipeline on every EMIT and let the exit engine **simulate counterfactual
  exits**. Never touches the exchange. This "shadow mode" is the v1 deliverable:
  the per-minute intrabar dataset (`position_marks`) that a close-to-close
  backtest can't produce.
- **`EXECUTOR_LIVE`** (default `False`) — additionally place **real-money** orders
  on Lighter via `radar/lighter_exec.py`. ⚠️ The live path is **unverified** —
  run the [G0 verification](#going-live--g0-verification) before enabling it.

The pipeline: **tier-gate** (§2 — backtest-validated `alpha_z` / `score_pctile`
features; v1 ships Tier A only) → **risk-defined sizing** (§3 — the dollar loss
at the stop is fixed at `MAX_LOSS_PER_TRADE_USD` regardless of stop width;
leverage never sizes) → **circuit breaker** (§6 — daily loss / trade caps,
consecutive-loss halt, kill-switch file) → **data capture** (§10 — eight SQLite
tables, every trade-linked row stamped with `config_version_id`) → place. The
exit engine (§5) marks each position per-minute and applies the asymmetric rule:
cut flat/red at +1h, let a working Tier-A runner ride to +4h on a
breakeven-then-trail. Server-side stops live on the exchange, so a VPS death
mid-trade does not unprotect the position.

## Quick start

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env   # GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
               # (live trading also needs LIGHTER_PRIVATE_KEY + LIGHTER_ACCOUNT_INDEX)

# 2. Verify Telegram works before deploying anywhere
python scripts/telegram_smoketest.py

# 3. Run with Docker (recommended for prod) — shadow mode by default
docker compose up -d --build
docker compose logs -f radar

# 4. Or run locally
pip install -e ".[dev]"
python -m radar.main
```

Out of the box the executor runs in **shadow mode** (`EXECUTOR_LIVE=False`): it
makes the full sizing decision and records the dataset, but places no orders.
See [Going live](#going-live--g0-verification) to enable real trading.

## Tests

```bash
pip install -e ".[dev]"
pytest tests/                          # 240 tests
pytest tests/test_executor.py -x       # executor: tiering / sizing / breaker / exit
pytest tests/test_main.py -x           # one module
```

All external services (Coinbase, Bybit, Binance, CoinGecko, yfinance, GDELT,
Groq/Gemini/Anthropic, Telegram, Lighter SDK) are mocked. Tests use the
`tmp_db` fixture for SQLite isolation.

## Going live — G0 verification

The live order path (`radar/lighter_exec.py`) is written against the documented
Lighter SDK but has **never placed a real order**. Before trusting it, run this
1-contract smoke test (the `EXECUTOR_SPEC.md` §9 G0 gate). Requires
`pip install -e .` so `lighter-sdk` resolves, plus `LIGHTER_PRIVATE_KEY`,
`LIGHTER_ACCOUNT_INDEX`, and `LIGHTER_API_KEY` in `.env`.

```bash
# 1. In radar/config.py, shrink risk and arm the live path:
#      MAX_LOSS_PER_TRADE_USD = 1.0     # $1 risk for the test
#      EXECUTOR_LIVE          = True
#    Keep EXECUTOR_ENABLED_TIERS = {"A"} (Tier A only).

# 2. Start the bot and wait for a BTC Tier-A EMIT → 1-contract market buy.
python -m radar.main
#    Confirm in the Lighter UI: position open AND a server-side stop resting.

# 3. Kill the bot while the position is open:
kill -TERM <pid>          # graceful shutdown
#   — or — touch the kill-switch (halts new entries; stops stay server-side):
touch /tmp/radar_halt

# 4. Restart. Boot reconciliation queries live positions/orders and logs each
#    open position + whether a tracked stop exists:
python -m radar.main      # grep the logs for "RECONCILE" / "reconciliation found"

# 5. Cancel the stop + flatten manually in the Lighter UI. Then revert:
#    EXECUTOR_LIVE = False, MAX_LOSS_PER_TRADE_USD = 5.0, rm /tmp/radar_halt
```

Only after G0 passes should you widen the aperture (Tier B → C-pop), and only
once `position_marks` confirms stops survive intrabar noise (§9 G2/G3).

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
cadence, ranker weights, suppression thresholds, LLM model, RSS feeds. Key
operational sections:

- `BACKFILL_*` — startup gap-aware backfill behavior (enabled, per-ticker
  timeout, skip-if-fresh threshold)
- `PRUNE_*` — retention (default: 30 days for bars + alerts, pruned once per day)
- `HOURLY_REPORT_*` — Tier 3 heartbeat cadence + content caps
- **`EXECUTOR` block** (bottom of the file) — `EXECUTOR_ENABLED` / `EXECUTOR_LIVE`
  master switches, `EXECUTOR_ENABLED_TIERS`, tier gates (§2), sizing +
  `MAX_LOSS_PER_TRADE_USD` (§3), exit-engine timing (§5), and circuit-breaker
  limits + `KILL_SWITCH_FILE` (§6). Changing any of these mints a new
  `config_versions` row so the captured dataset stays segmentable by rule-set.

## Deploy

See [`deploy-quickref.md`](./deploy-quickref.md) for the full walkthrough
(one-time server setup, SSH alias, env check, deploy, log greps, troubleshooting).

TL;DR after one-time setup:

```bash
RADAR_HOST=radar ./deploy.sh
```

The script rsyncs the repo (excluding `.git/` and `data/`) to
`/home/radar/catalyst-radar/` and runs `docker compose up -d --build`. The host's
`data/radar.db` survives redeploys via the `./data:/app/data` Docker volume
mount, so subsequent boots just top off the small gap created during rebuild.
