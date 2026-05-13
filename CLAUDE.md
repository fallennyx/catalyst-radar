# Catalyst Radar — Claude project guide

Read this fully before doing anything in this repo. It encodes design
decisions, hard-won gotchas, and the conventions every change must follow.
Act as an expert coder. Only output code or requested data. No greetings, no explanations, no 'here is the code' filler, no summaries of changes. If I ask a question, answer in one sentence."
---

## What this is

A single-process Python engine that scans Lighter DEX perps every five minutes,
ranks unusual movers with a composite vol-normalized score, fetches news
catalysts, classifies them with Claude Haiku, applies a five-rule suppression
chain (BOS structural break is Rule 0), and pushes survivors to Telegram.

Architecture is **three cooperating asyncio loops in one process**:
- **Tier 1** (every 5 min): universe → ranker → catalysts → classifier → suppression → emit OR watchlist OR drop
- **Tier 2** (every 60 s): poll watchlist tickers for live mark-price crosses against stored swing references; promote to EMIT on confirmed cross + range expansion
- **Tier 3** (every 1 h): push heartbeat + active watchlist summary + recent top-mover candidates to Telegram. Doubles as engine-alive signal.

Single Python process, single asyncio loop. **No threading. No multiprocessing.
No microservices. No web dashboard. No ML training. No backtesting framework
beyond `radar/replay.py`.**

---

## File map (skim before searching)

```
radar/
  main.py          asyncio orchestrator: tier1_discovery_scan + tier2_trigger_watch
                   + tier3_hourly_report, plus _backfill_bars_for_universe and
                   _maybe_prune (wired into the Tier 1 loop)
  config.py        ALL tunables. Edit here to change behavior.
  lighter.py       live Lighter universe (~163 perps via mainnet API) + classify()
  universe.py      Market dataclass; sources from lighter.fetch_universe()
  storage.py       SQLite + Bar dataclass + watchlist helpers + swappable _now();
                   includes last_bar_ts, prune_old_bars, prune_old_alerts
  ranker.py        composite score + 1h/4h swing detection + has_breakout_structure
  trade_plan.py    deterministic SL/TP ladder attached to every BOS alert
  catalysts.py     asset-routed news (RSS / EDGAR / yfinance / GDELT)
  classifier.py    Anthropic Haiku tool_choice + substring evidence validator
  beta.py          BTC-beta gate (residual returns + alpha_z)
  suppression.py   5-rule chain (BOS-aware), Alert dataclass
  telegram.py      send_bos_alert (with Plan: block) + send_watchlist_notification
  replay.py        virtual-clock historical playback
  fetch_bars.py    Coinbase → Bybit → Binance → CoinGecko fallback chain for crypto;
                   yfinance for equity / commodity / forex (=X / =F overrides);
                   ROUTES dict + TICKER_ROUTE_OVERRIDES + is_fetchable() helper
  fetch_news.py    GDELT historical news
  timefmt.py       CDT/CST formatting (America/Chicago) — user-facing only
tests/             194 passing; mock all external services
data/              SQLite + sample data (bars.csv / news_archive.json gitignored)
BOS_FILTER_NOTES.md  rationale + tuning guide for the BOS filter
deploy-quickref.md   end-to-end deploy walkthrough (ssh + docker compose)
```

---

## BOS engine — multi-timeframe (v3, May 2026)

`ranker.has_breakout_structure(market, history, current_price, history_15m=None)`
fires when **both** conditions are true:

1. **4h structural break** — live price has crossed a UTC-aligned 4h-frame swing
   high (long) or swing low (short). 4h bars are synthesized on the fly via
   `synthesize_4h_bars(bars_1h)` from the most recent `BOS_BAR_HISTORY_HOURS`
   (240) of 1h history. Pivots use `find_swing_high/_low` reused with 4h params:
   `SWING_LOOKBACK_4H_BARS=30`, `SWING_MIN_AGE_4H_BARS=1`,
   `SWING_MIN_BARS_VALIDATION_4H=2` (loose: 8h validation).
2. **Range expansion confirmation on EITHER timeframe** (v3 parallel gate):
   - The in-progress **1h** bar's range exceeds
     `RANGE_EXPANSION_MULTIPLIER × median 1h range` (2.0× over 48 bars), OR
   - The in-progress **15m** bar's range exceeds
     `RANGE_EXPANSION_MULTIPLIER_15M × median 15m range` (2.5× over 96 bars).

The 15m parallel path shrinks alert latency from ~10–30 min (1h-only) to
~1–5 min on real impulses. 15m bars come from `bars_15m` — backfilled at
boot from Coinbase 15m (granularity=900) / Bybit 15m (interval=15), and
aggregated live each Tier-1 cycle via `storage.upsert_bar_15m_from_tick`.
Equity/commodity/forex tickers have no 15m route — falls back to 1h-only.

`metadata["breakout_level"]` is the **4h** swing reference. The trade plan
(`radar/trade_plan.py`) consumes `breakout_level` and produces stop = level
± `STOP_BUFFER_PCT (0.2%)`, TP1 = 1.5R, TP2 = next prior swing OR 3R
(whichever is closer, but never less aggressive than TP1).

`has_breakout_structure` returns a **4-tuple** `(broke, direction, level, structure_type)`
where `structure_type` is `"4h"` or `"1h"`. Suppression metadata carries this as
`metadata["structure_type"]`; Telegram renders `[1h]` badge on early-detection alerts.

**1h early-detection path** (v3.1): when price breaks a 1h swing pivot with
`RANGE_EXPANSION_MULTIPLIER_1H_ENTRY (1.5×)` confirmation, fires immediately — even
when 4h history is too short. Needs only 27 bars minimum. Config knobs:
`BOS_1H_ENABLED`, `SWING_LOOKBACK_1H_BOS_BARS=24`, `SWING_MIN_AGE_1H_BOS_BARS=1`,
`SWING_MIN_BARS_VALIDATION_1H=2`, `RANGE_EXPANSION_MULTIPLIER_1H_ENTRY=1.5`.

4h path **takes priority** — if a 4h pivot breaks with 2.0× range, it fires as `"4h"`.
Only if 4h path doesn't fire (cold-start, or no 4h pivot crossed) does the 1h path run.

Cold-start: the 4h path needs ≥ 32 4h-bars (≈ 5.5 days of 1h data) before it can
fire. The 1h path fires from the first 27 bars. Below 27 bars, `has_breakout_structure`
returns `(False, None, None, None)` and Rule 0 routes to WATCHLIST or DROP.

---

## Suppression chain (v3 — enrichment-only LLM, BOS is the only suppressor)

`suppression.evaluate(market, alert, history, history_15m=None) -> tuple[decision, reason, metadata]`

**Core invariant**: every BOS-confirmed candidate fires. LLM/predictor/
order-book/volume-profile layers attach commentary but **never block an
alert**. Alert fatigue is not a concern; missed signals are.

1. **Rule 0 — Structural BOS check.**
   - BOS confirmed + direction agrees with classifier → continue to Rule 1, remove any existing watchlist entry.
   - BOS confirmed + direction conflict → **pass-through with metadata flag** (`direction_conflict=True`, `classifier_direction=...`). Alert fires with "⚠️ LLM disagrees" badge.
   - No BOS + score ≥ `WATCHLIST_SCORE_THRESHOLD` + classifier dir ∈ {long, short} → WATCHLIST.
   - No BOS + low score → DROP `no_structure_break`.
2. **Rule 1 — Per-catalyst dedup (4 h).** Same ticker + same `catalyst_type` already EMIT'd → DROP `dedup_4h`.
3. **Rule 2 — BTC-beta gate (crypto only, OR-gated).** Drops if `|alpha_z| < 2.0` OR `|r_alpha| < 3 %`. **Bypassed when `current_bar.range > IMPULSE_BYPASS_MULTIPLIER * median_range` (2.5×).**
4. ~~**Rule 3 — Sector-day cluster.**~~ **REMOVED in v3.** `config.SECTOR_DAY_THRESHOLD` is vestigial.
5. **Rule 4 — Daily budget.** Hit `DAILY_ALERT_BUDGET` (30) → only above-median-score candidates fire.

**Survivors → EMIT `ok`.** EMITs run through Stage 1 (classifier) and Stage 2
(predictor) for **enrichment only** — never cause a DROP/DOWNGRADE post-EMIT.

---

## Sharp edges — read before touching code

> **v3 NO-SUPPRESSION INVARIANT (read first).** Enrichment layers (LLM
> classifier, Stage 2 predictor, order-book sentiment, volume profile)
> **never block an alert**. BOS is the only suppressor. If you find yourself
> adding a `return ("DROP", ...)` for a non-structural reason, stop. Flag it
> in `metadata` and let the Telegram body warn the user, or drop the check
> entirely. The dirt-cheap cost of a false-positive alert is far less than
> the cost of missing a real signal (see May 11–12 incident).
> `tests/test_suppression.py::test_bos_confirmed_direction_conflict_passes_with_metadata_flag`
> pins this.

1. **`storage.recent_bars()` returns `list[Bar]`, not sqlite3.Row.** `Bar` has `__getitem__` so `r["close"]` still works for legacy callers, but new code should use `r.close`.
2. **`storage.insert_bar(open_=…)` — note the trailing underscore.** `open` is a Python builtin.
3. **`storage._now` is a swappable module attribute.** Replay reassigns it for virtual-clock playback. Use `storage.set_clock(fn)` to override; pass `None` to restore. Every internal time read in storage MUST go through `_now()`, never `time.time()` directly.
4. **`evaluate(market, alert, history)` returns a 3-tuple `(decision, reason, metadata)`.** The metadata dict carries `breakout_level`, `structure_direction`, `structure_type` (`"4h"` or `"1h"`), `swing_high_reference` (4h), `swing_low_reference` (4h), `swing_reference_timestamp`, `median_bar_range` (1h). **`has_breakout_structure` returns a 4-tuple** `(broke, direction, level, structure_type)` — update all callers when the signature changes.
5. **`Alert` carries `classifier_result`.** `Alert(ticker, asset_class, score, alpha_z, r_alpha_pct, classifier_result=ClassifierResult)`. Without it, watchlist routing won't engage and direction-conflict checks no-op.
6. **`ClassifierResult` has legacy + extended fields.** Pydantic `model_validator(mode="after")` fills `primary_catalyst→summary`, `conviction→confidence`, `continuation_thesis→summary` when not supplied. Tests can construct with just the legacy fields.
7. **`find_swing_high` / `find_swing_low` use a fallback ladder.** Strict pass returns highest unbroken pivot; fallback (signaled by `bars_validated == 0`) returns the absolute highest in the eligible window. Don't remove — strict logic returns `None` in trending markets, losing the reference exactly when a breakout occurs.
8. **`SWING_MIN_AGE_HOURS = 4`** excludes the most-recent 4 bars so the breakout candle's own wick can't be picked as the reference. Lowering risks self-reference.
9. **All datetimes:** UTC unix-int for `bars_1h.ts` and `alerts.created_at`, ISO-8601 strings (naive UTC) for `watchlist.{added_at, expires_at, …}`. **User-facing display uses CDT** via `radar/timefmt.py`. Never mix.
10. **Telegram sends are synchronous HTTP via `requests`.** `telegram.py:_send_sync` POSTs to the Bot API directly; Tier 3 wraps in `asyncio.to_thread(...)`. Don't reintroduce `python-telegram-bot` — its httpx client binds to the first asyncio loop it sees, causing `RuntimeError: Event loop is closed` on the second send (May 11 incident).
11. **`BOS_BAR_HISTORY_HOURS = 240` (10 days) is the minimum history for BOS evaluation.** 4h frame needs ≥ 33 4h-bars (132 1h hours). Don't downsize.
12. **No new dependencies.** `pyproject.toml` is pinned. Ask first.
13. **`main.py` module state powers the hourly report.** `_LAST_SCAN_TS`, `_LAST_TOP_CANDIDATES`, `_LAST_PRUNE_TS` are set at end of Tier 1. Keep those writes if refactoring — Tier 3 reads them via `_format_hourly_report()`.
14. **`fetch_bars` row schema uses ISO `ts`, storage uses unix int.** Backfill goes through `_parse_iso_to_unix` (`%Y-%m-%dT%H:%M:%SZ`). Don't change `_iso()` without updating that parser.
15. **`TICKER_ROUTE_OVERRIDES` lives in `fetch_bars.py`.** Use when a ticker's Lighter `asset_class` doesn't match where it trades (e.g. PAXG is "commodity" but routes through Binance).
16. **Telegram failures NEVER raise out of the hourly-report send.** `_send_hourly_report` wraps in try/except. Don't bubble exceptions out of Tier 3.

---

## Data sourcing — rate limits and quirks

| Source | Rate limit | Used for | Notes |
|---|---|---|---|
| **Coinbase Exchange** | ~10 req/s public | crypto bars (1st choice) | Max 300 candles/call; product IDs like `BTC-USD`. US-accessible. |
| **Bybit v5** | ~20 req/s public | crypto bars (2nd choice) | Max 1000 candles/call. Good fallback for tokens missing on Coinbase. |
| **Binance klines** | 6000 wt/min | crypto bars (3rd choice) | 1h interval, max 1000 candles/call. PEPE/BONK use `1000PEPE`/`1000BONK`. Geo-blocked from some US regions. |
| **CoinGecko `/market_chart/range`** | ~5 call/s aggressive | crypto bars (final fallback) | 1 tick/hour for >1-day windows — synthesizes OHLC from previous-bar close. Requires explicit `COINGECKO_IDS` mapping. |
| **GDELT doc API** | 1 query / 5 s hard | historical news | Bare `OR` requires `(...)` wrap. Wait ≥ 20 s after 429. |
| **yfinance** | gentle | equity / commodity / forex | Intraday capped at ~30-60 days. Forex: `=X` suffix; commodities: `=F`. All 8 Lighter forex pairs auto-mapped in `YFINANCE_SYMBOLS`. |
| **Lighter mainnet API** | unrestricted (so far) | universe | ~163 active perps as of May 2026. Cached 60 s in `radar.lighter`. |
| **Anthropic Haiku** | account-tier | classifier | tool_choice forces structured output. Substring validator drops fabricated quotes. |

---

## Conventions

- **Type hints required**, Python 3.11 syntax (`list | None`, `dict[str, float]`).
- **Logging:** `log.info` normal flow, `log.warning` degraded, `log.exception` caught (auto-traceback). Always include the ticker.
- **Tests:** mock all external services. `tmp_db` fixture for isolated SQLite. Run `pytest tests/` from repo root.
- **Commits:** descriptive HEREDOC bodies. Co-Authored-By line is fine. Never amend, never `--no-verify`. Squash branches before merge.
- **Time display:** **always CDT** for the user. `from radar.timefmt import fmt_cdt; fmt_cdt(unix_ts)`.
- **Storage stays UTC internally.** Never store CDT.

---

## Common commands

```bash
# tests
pytest tests/                          # all 175 should pass
pytest tests/test_suppression.py -x    # one file, stop on first failure

# fetch fresh data
python -m radar.fetch_bars --tickers BTC,ETH,SOL,DOGE,ARB,OP,WIF,PEPE,BONK --days 30
python -m radar.fetch_news --tickers BTC,ETH --start 2026-04-15 --end 2026-05-06 --sleep 6

# coverage audit
python -c "
from collections import defaultdict
from radar import lighter, fetch_bars
m = lighter.fetch_universe()
ok, miss = defaultdict(list), defaultdict(list)
for x in m:
    (ok if fetch_bars.is_fetchable(x.symbol, x.asset_class) else miss)[x.asset_class].append(x.symbol)
print(f'fetchable: {sum(len(v) for v in ok.values())}/{len(m)}')
for c, syms in sorted(miss.items()): print(c, sorted(syms))
"

# historical replay (no API costs with --no-classify)
python -m radar.replay --bars data/bars.csv --news data/news_archive.json --no-classify

# end-to-end run (needs ANTHROPIC_API_KEY + TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)
python -m radar.main

# Telegram smoke test
python scripts/telegram_smoketest.py

# deploy — full walkthrough in deploy-quickref.md
RADAR_HOST=user@host ./deploy.sh
```

---

## How to onboard fast

When starting any non-trivial change, in order:

1. Read this file.
2. Check `BOS_FILTER_NOTES.md` for engine design rationale.
3. Read the module(s) you're touching **in full** — most are < 400 lines.
4. Read the matching test file. Fixtures show how the API is meant to be used.
5. Search for the function name across `radar/`. Be especially careful with `evaluate`, `record_alert`, and `recent_bars` — multiple callers, signature changes break replay.

---

## Active work / context for the agent

- **Branch:** `master` (user commits manually; never auto-commit, branch, or push).
- **In flight:** wiring `fetch_bars` CLI and `replay.load_bars` to use the live Lighter universe + auto-classification (`lighter.classify`). The CLI still uses `config.SYMBOL_TO_CLASS`; the startup backfill (runtime path) uses the live universe directly. Resume by adding `lighter.classify(ticker)` to `fetch_bars.fetch_universe`'s CSV row construction and replacing `config.SYMBOL_TO_CLASS.values()` validation in `replay.load_bars` with `config.VALID_ASSET_CLASSES`.
- **MTF Phase 2 deferred (15-min trigger frame):** needs Binance access (geo-blocked) or paid CoinGecko. Hold until Phase 1 alert mix confirmed.
- **Stooq evaluated, rejected** — only returns hourly data for forex/indices, not stocks/ETFs. Don't re-explore for stock coverage.

---

## Anti-features (do NOT add unless explicitly approved)

- Threading or multiprocessing
- A web dashboard / HTTP API
- Persistent message queues
- Per-asset-class swing-lookback tuning (single `SWING_LOOKBACK_HOURS` for all)
- Two-bar close confirmation
- WebSocket-based live price (SDK polling is the v2 floor; WS is v3 roadmap)
- ML model training, backtesting frameworks beyond `radar/replay.py`
- Trade execution (the engine never places an order; alerts only)
- Position sizing or account-fraction risk math (the trade plan is advisory only)
- Any unlisted dependency in `pyproject.toml`

(Multi-timeframe BOS confirmation was previously listed as an anti-feature;
it's now Phase 1 of the engine — see "BOS engine" above.)
