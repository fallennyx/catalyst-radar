# Catalyst Radar — Claude project guide

Read this fully before doing anything in this repo. It encodes design
decisions, hard-won gotchas, and the conventions every change must follow.

---

## What this is

A single-process Python engine that scans Lighter DEX perps every five minutes,
ranks unusual movers with a composite vol-normalized score, fetches news
catalysts, classifies them with Claude Haiku, applies a five-rule suppression
chain (BOS structural break is Rule 0), and pushes survivors to Telegram.

Architecture is **two cooperating asyncio loops in one process**:
- **Tier 1** (every 5 min): universe → ranker → catalysts → classifier → suppression → emit OR watchlist OR drop
- **Tier 2** (every 60 s): poll watchlist tickers for live mark-price crosses against stored swing references; promote to EMIT on confirmed cross + range expansion

Single Python process, single asyncio loop. **No threading. No multiprocessing.
No microservices. No web dashboard. No ML training. No backtesting framework
beyond `radar/replay.py`.**

---

## File map (skim before searching)

```
radar/
  main.py          asyncio orchestrator: tier1_discovery_scan + tier2_trigger_watch
  config.py        ALL tunables. Edit here to change behavior.
  lighter.py       live Lighter universe (161 perps via mainnet API) + classify()
  universe.py      Market dataclass; sources from lighter.fetch_universe()
  storage.py       SQLite + Bar dataclass + 6 watchlist helpers + swappable _now()
  ranker.py        composite score + swing detection + has_breakout_structure
  catalysts.py     asset-routed news (RSS / EDGAR / yfinance / GDELT)
  classifier.py    Anthropic Haiku tool_choice + substring evidence validator
  beta.py          BTC-beta gate (residual returns + alpha_z)
  suppression.py   5-rule chain (BOS-aware), Alert dataclass
  telegram.py      send_bos_alert + send_watchlist_notification
  replay.py        virtual-clock historical playback
  fetch_bars.py    Binance (preferred) → CoinGecko fallback → yfinance for equities
  fetch_news.py    GDELT historical news
  timefmt.py       CDT/CST formatting (America/Chicago) — user-facing only
tests/             93 passing; mock all external services
data/              SQLite + sample data (bars.csv / news_archive.json gitignored)
BOS_FILTER_NOTES.md  rationale + tuning guide for the BOS filter
```

---

## Five-rule suppression chain (in order — first match wins)

`suppression.evaluate(market, alert, history) -> tuple[decision, reason, metadata]`

1. **Rule 0 — Structural BOS check.**
   - BOS confirmed + direction agrees with classifier → continue to Rule 1, remove any existing watchlist entry.
   - BOS confirmed + direction conflict → DROP `structure_direction_conflict`.
   - No BOS + score ≥ `WATCHLIST_SCORE_THRESHOLD` + classifier dir ∈ {long, short} → WATCHLIST.
   - No BOS + low score → DROP `no_structure_break`.
2. **Rule 1 — Per-catalyst dedup (4 h).** Same ticker + same `catalyst_type` already EMIT'd → DROP `dedup_4h`.
3. **Rule 2 — BTC-beta gate (crypto only, OR-gated).** Drops if `|alpha_z| < 2.0` OR `|r_alpha| < 3 %`. **Bypassed when `current_bar.range > IMPULSE_BYPASS_MULTIPLIER * median_range` (2.5×)** — that's the carve-out that lets first-leg breakouts through.
4. **Rule 3 — Sector-day cluster.** Asset_class hit `SECTOR_DAY_THRESHOLD` (5) recent EMITs → only the top-score new candidate breaks through.
5. **Rule 4 — Daily budget.** Hit `DAILY_ALERT_BUDGET` (10) → only above-median-score candidates fire.

**Survivors → EMIT `ok`.**

---

## Sharp edges — read before touching code

These have all bitten this project. Don't repeat them.

1. **`storage.recent_bars()` returns `list[Bar]`, not sqlite3.Row.** `Bar` has `__getitem__` so `r["close"]` still works for legacy callers, but new code should use `r.close` (attribute access).
2. **`storage.insert_bar(open_=…)` — note the trailing underscore.** `open` is a Python builtin.
3. **`storage._now` is a swappable module attribute.** Replay reassigns it for virtual-clock playback. Use `storage.set_clock(fn)` to override; pass `None` to restore. Every internal time read in storage MUST go through `_now()`, never `time.time()` directly.
4. **`evaluate(market, alert, history)` returns a 3-tuple `(decision, reason, metadata)`.** The metadata dict carries `breakout_level`, `swing_high_reference`, `swing_low_reference`, `swing_reference_timestamp`, `median_bar_range` — downstream consumers (telegram, replay, alert log) read it.
5. **`Alert` carries `classifier_result`.** `Alert(ticker, asset_class, score, alpha_z, r_alpha_pct, classifier_result=ClassifierResult)`. Without `classifier_result`, watchlist routing won't engage (no direction bias) and direction-conflict checks no-op.
6. **`ClassifierResult` has legacy + extended fields.** Pydantic `model_validator(mode="after")` fills `primary_catalyst→summary`, `conviction→confidence`, `continuation_thesis→summary` when not supplied. Tests can construct with just the legacy fields.
7. **`find_swing_high` / `find_swing_low` use a fallback ladder.** Strict pass returns highest unbroken pivot; fallback (signaled by `bars_validated == 0`) returns the absolute highest in the eligible window when strict logic exhausts. This was added because strict logic returned `None` in trending markets where every candidate gets broken sequentially — losing the reference precisely at the moment a breakout occurs. Don't remove the fallback.
8. **`SWING_MIN_AGE_HOURS = 4`** excludes the most-recent 4 bars from swing detection so the breakout candle's own wick can't be picked as the reference. Lowering this risks self-reference.
9. **All datetimes:** UTC unix-int for `bars_1h.ts` and `alerts.created_at`, ISO-8601 strings (naive UTC) for `watchlist.{added_at, expires_at, …}`. **User-facing display uses CDT** via `radar/timefmt.py`. Never mix.
10. **Telegram lib v21 is async-only.** `telegram.py:_send_sync` runs the coroutine on a private event loop so the rest of the codebase can stay sync. Never block the main asyncio loop with `_send_sync`; if called from Tier 1/2 cycles it works because each cycle is `await asyncio.sleep`-bounded.
11. **No new dependencies.** `pyproject.toml` is pinned. If you genuinely need one, ask first.

---

## Data sourcing — rate limits and quirks

| Source | Rate limit | Used for | Notes |
|---|---|---|---|
| **Binance klines** | 6000 wt/min | crypto bars (preferred) | 1h interval, max 1000 candles/call. PEPE/BONK use `1000PEPE`/`1000BONK` price-scaled symbols. |
| **CoinGecko `/market_chart/range`** | ~5 call/s aggressive | crypto bars (fallback) | Returns **1 tick/hour for >1-day windows** — `fetch_crypto_hourly` synthesizes OHLC from previous-bar close. Range-expansion tests will be conservative. |
| **GDELT doc API** | 1 query / 5 s hard | historical news | Bare `OR` requires `(...)` wrap. Rate limiter is sticky after 429s — wait ≥ 20 s before retry. |
| **yfinance** | gentle | equity / commodity / forex | Intraday capped at ~30-60 days. Forex pairs need `=X` suffix (e.g. `EURUSD=X`); commodities use `=F` (`GC=F` for gold). |
| **Lighter mainnet API** | unrestricted (so far) | universe | `https://mainnet.zklighter.elliot.ai/api/v1/orderBooks` returns 161 active perps. Cached 60 s in `radar.lighter`. |
| **Anthropic Haiku** | account-tier | classifier | tool_choice forces structured output. Substring validator drops responses with fabricated quotes. |

---

## Conventions

- **Type hints required**, Python 3.11 syntax (`list | None`, `dict[str, float]`).
- **Logging:** `log.info` normal flow, `log.warning` degraded, `log.exception` caught (auto-traceback). Always include the ticker.
- **Tests:** mock all external services. `tmp_db` fixture for isolated SQLite. Run `pytest tests/` from repo root.
- **Commits:** descriptive HEREDOC bodies; the conversation history shows the format. Co-Authored-By line is fine. Never amend, never `--no-verify`. Squash branches before merge.
- **Time display:** **always CDT** for the user. `from radar.timefmt import fmt_cdt; fmt_cdt(unix_ts)` — handles DST automatically (CDT in summer, CST winter).
- **Storage stays UTC internally.** Never store CDT.

---

## Common commands

```bash
# tests
pytest tests/                          # all 93+ should pass
pytest tests/test_suppression.py -x    # one file, stop on first failure

# fetch fresh data
python -m radar.fetch_bars --tickers BTC,ETH,SOL,DOGE,ARB,OP,WIF,PEPE,BONK --days 30
python -m radar.fetch_news --tickers BTC,ETH --start 2026-04-15 --end 2026-05-06 --sleep 6

# historical replay (no API costs with --no-classify)
python -m radar.replay --bars data/bars.csv --news data/news_archive.json --no-classify

# end-to-end run (needs ANTHROPIC_API_KEY + TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)
python -m radar.main

# deploy (rsyncs to RADAR_HOST and rebuilds the container)
RADAR_HOST=user@host ./deploy.sh
```

---

## How to onboard fast

When starting any non-trivial change, in order:

1. Read this file. (You're here.)
2. Check `BOS_FILTER_NOTES.md` for the engine's design rationale.
3. Read the module(s) you're touching **in full** — most are < 400 lines.
4. Read the matching test file. The test fixtures show how the API is meant to be used.
5. Search for the function name across `radar/` to find all callers. Be especially careful with `evaluate`, `record_alert`, and `recent_bars` — multiple callers, signature changes break replay.

For new features:

1. Add the smallest config knob to `radar/config.py` if behavior is tunable.
2. Implement in the module(s) the feature naturally lives in. Match neighboring style.
3. Add tests **before** committing. Mock external services.
4. Run `pytest tests/` to confirm no regression.
5. Update this file if the change adds a new sharp edge.

---

## Active work / context for the agent

- **Branch:** `feature/bos-filter-v2`. Pushed to `origin`.
- **In flight:** wiring `fetch_bars` and `replay.load_bars` to use the live Lighter universe + auto-classification (`lighter.classify`). The Lighter module exists; the consumers still use the legacy `config.SYMBOL_TO_CLASS` map. Resume by adding `lighter.classify(ticker)` to fetch_bars's CSV row construction and replacing the `config.SYMBOL_TO_CLASS.values()` validation in `replay.load_bars` with `config.VALID_ASSET_CLASSES`.
- **Forex/ETF routing in `fetch_bars`** is not yet implemented — tickers like `EURUSD`, `SPY`, `QQQ` from Lighter currently fall through `fetch_yfinance_hourly` which mostly works for equities/ETFs but doesn't handle forex symbol mapping (needs `=X` suffix).
- **Trade plan (SL/TP) not yet built.** User asked about deterministic stop-loss + take-profit ladders attached to every alert. Recommended approach: new `radar/trade_plan.py` with `compute_plan(market, history, metadata, direction) → entry/stop/tp1/tp2/risk_per_unit/r_multiple`. Wire into `telegram.send_bos_alert` payload. No second LLM call — direction comes from suppression metadata, stop = broken swing level + small buffer, tp1 = 1.5R, tp2 = next prior swing.

---

## Anti-features (do NOT add unless explicitly approved)

- Threading or multiprocessing
- A web dashboard / HTTP API
- Persistent message queues
- Per-asset-class swing-lookback tuning (single `SWING_LOOKBACK_HOURS` for all)
- Multi-timeframe BOS confirmation
- Two-bar close confirmation
- WebSocket-based live price (SDK polling is the v2 floor; WS is v3 roadmap)
- ML model training, backtesting frameworks beyond `radar/replay.py`
- Trade execution (the engine never places an order; alerts only)
- Any unlisted dependency in `pyproject.toml`
