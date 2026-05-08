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
  ranker.py        composite score + 1h/4h swing detection + has_breakout_structure
  trade_plan.py    deterministic SL/TP ladder attached to every BOS alert
  catalysts.py     asset-routed news (RSS / EDGAR / yfinance / GDELT)
  classifier.py    Anthropic Haiku tool_choice + substring evidence validator
  beta.py          BTC-beta gate (residual returns + alpha_z)
  suppression.py   5-rule chain (BOS-aware), Alert dataclass
  telegram.py      send_bos_alert (with Plan: block) + send_watchlist_notification
  replay.py        virtual-clock historical playback
  fetch_bars.py    Binance (preferred) → CoinGecko fallback → yfinance for equities
  fetch_news.py    GDELT historical news
  timefmt.py       CDT/CST formatting (America/Chicago) — user-facing only
tests/             ~120 passing; mock all external services
data/              SQLite + sample data (bars.csv / news_archive.json gitignored)
BOS_FILTER_NOTES.md  rationale + tuning guide for the BOS filter
```

---

## BOS engine — multi-timeframe (Phase 1, May 2026)

`ranker.has_breakout_structure(market, history, current_price)` fires only
when **both** conditions are true:

1. **4h structural break** — live price has crossed a UTC-aligned 4h-frame swing
   high (long) or swing low (short). 4h bars are synthesized on the fly via
   `synthesize_4h_bars(bars_1h)` from the most recent `BOS_BAR_HISTORY_HOURS`
   (240) of 1h history. Pivots use `find_swing_high/_low` reused with 4h params:
   `SWING_LOOKBACK_4H_BARS=30`, `SWING_MIN_AGE_4H_BARS=1`,
   `SWING_MIN_BARS_VALIDATION_4H=2` (loose: 8h validation).
2. **1h range expansion confirmation** — the in-progress 1h bar's range
   exceeds `RANGE_EXPANSION_MULTIPLIER × median 1h range` over the lookback
   window (current threshold: 2.0×, was 1.5× pre-MTF).

`metadata["breakout_level"]` is the **4h** swing reference. The watchlist also
stores 4h levels (Tier 2 polls live price vs. those references with the 1h
range-expansion check on the in-progress bar). The trade plan
(`radar/trade_plan.py`) consumes `breakout_level` and produces stop = level
± `STOP_BUFFER_PCT (0.2%)`, TP1 = 1.5R, TP2 = next prior swing OR 3R
(whichever is closer, but never less aggressive than TP1).

Cold-start: needs ≥ 33 4h-bars (≈ 5.5 days of 1h data) before the 4h frame
can fire. Below that, `has_breakout_structure` returns `(False, None, None)`
and Rule 0 routes to WATCHLIST (if score is high enough) or DROP.

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
4. **`evaluate(market, alert, history)` returns a 3-tuple `(decision, reason, metadata)`.** The metadata dict carries `breakout_level` (4h swing), `structure_direction`, `swing_high_reference` (4h), `swing_low_reference` (4h), `swing_reference_timestamp`, `median_bar_range` (1h) — downstream consumers (telegram, replay, alert log, trade_plan) read it. The `structure_direction` field exists so replay-without-classifier can still build a trade plan.
5. **`Alert` carries `classifier_result`.** `Alert(ticker, asset_class, score, alpha_z, r_alpha_pct, classifier_result=ClassifierResult)`. Without `classifier_result`, watchlist routing won't engage (no direction bias) and direction-conflict checks no-op.
6. **`ClassifierResult` has legacy + extended fields.** Pydantic `model_validator(mode="after")` fills `primary_catalyst→summary`, `conviction→confidence`, `continuation_thesis→summary` when not supplied. Tests can construct with just the legacy fields.
7. **`find_swing_high` / `find_swing_low` use a fallback ladder.** Strict pass returns highest unbroken pivot; fallback (signaled by `bars_validated == 0`) returns the absolute highest in the eligible window when strict logic exhausts. This was added because strict logic returned `None` in trending markets where every candidate gets broken sequentially — losing the reference precisely at the moment a breakout occurs. Don't remove the fallback.
8. **`SWING_MIN_AGE_HOURS = 4`** excludes the most-recent 4 bars from swing detection so the breakout candle's own wick can't be picked as the reference. Lowering this risks self-reference.
9. **All datetimes:** UTC unix-int for `bars_1h.ts` and `alerts.created_at`, ISO-8601 strings (naive UTC) for `watchlist.{added_at, expires_at, …}`. **User-facing display uses CDT** via `radar/timefmt.py`. Never mix.
10. **Telegram lib v21 is async-only.** `telegram.py:_send_sync` runs the coroutine on a private event loop so the rest of the codebase can stay sync. Never block the main asyncio loop with `_send_sync`; if called from Tier 1/2 cycles it works because each cycle is `await asyncio.sleep`-bounded.
11. **`BOS_BAR_HISTORY_HOURS = 240` (10 days) is the minimum history fetch for BOS evaluation.** The 4h frame needs ≥ 33 4h-bars (132 1h hours) to fire; main.py and replay.py both pull this much. Don't downsize without checking that 4h synthesis still has enough bars.
12. **No new dependencies.** `pyproject.toml` is pinned. If you genuinely need one, ask first.

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

- **Branch:** `master` (working tree dirty — user commits manually; never auto-commit, branch, or push).
- **Recently landed (May 2026):**
  - **Trade plan** (`radar/trade_plan.py`) — every BOS EMIT carries a deterministic
    SL/TP ladder. Plan block is rendered in the Telegram payload via
    `telegram._format_plan`. TP2 = `min(next_prior_swing, entry + 3R)` for longs,
    mirrored for shorts; pivots inside TP1 are skipped so the ladder always
    progresses outward.
  - **MTF BOS Phase 1** — 4h structural break + 1h range-expansion confirmation
    (see "BOS engine" section above). RANGE_EXPANSION 1.5→2.0,
    SWING_MIN_BARS_VALIDATION 3→6 on the 1h frame. New 4h knobs added.
  - **`structure_direction` in suppression metadata** — replay-without-classifier
    can now build trade plans by falling back to the structural direction.
  - **`BOS_BAR_HISTORY_HOURS = 240`** — main.py and replay.py both pull this
    much history before invoking `has_breakout_structure` (4h synthesis needs
    ≥ 33 4h-bars).
- **In flight (older, unfinished):** wiring `fetch_bars` and `replay.load_bars`
  to use the live Lighter universe + auto-classification (`lighter.classify`).
  Lighter module exists; consumers still use the legacy `config.SYMBOL_TO_CLASS`
  map. Resume by adding `lighter.classify(ticker)` to fetch_bars's CSV row
  construction and replacing `config.SYMBOL_TO_CLASS.values()` validation in
  `replay.load_bars` with `config.VALID_ASSET_CLASSES`.
- **Forex/ETF routing in `fetch_bars`** still not implemented — `EURUSD`,
  `SPY`, `QQQ` from Lighter fall through `fetch_yfinance_hourly` which doesn't
  handle the `=X` forex suffix.
- **MTF Phase 2 deferred (15-min trigger frame):** would solve the
  "alert at 03:15 not 04:00" intra-bar latency. Needs Binance access (currently
  geo-blocked) or paid CoinGecko tier. Hold off until live trading confirms
  Phase 1 is producing the right alert mix.

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
