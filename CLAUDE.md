# Catalyst Radar — Claude project guide

Read this fully before doing anything in this repo. It encodes design
decisions, hard-won gotchas, and the conventions every change must follow.
Act as an expert coder. Only output code or requested data. No greetings, no explanations, no 'here is the code' filler, no summaries of changes. If I ask a question, answer in one sentence."
---

## What this is

A single-process Python engine that scans Lighter DEX perps every minute, ranks
unusual movers with a composite vol-normalized score, fetches news catalysts,
classifies them with an LLM (Groq Llama 3.3 70B primary; Gemini/Haiku fallback),
confirms a structural break of structure (BOS), pushes the survivor to Telegram
with a deterministic SL/TP plan — and, as of v1 of the **execution layer**,
sizes and (optionally) places a real risk-managed position on Lighter.

Architecture is **four cooperating asyncio loops in one process**:
- **Tier 1** (every `FAST_CADENCE_SEC`, 60 s): universe → ranker → catalysts → classifier → adjudicator → suppression → emit OR watchlist OR drop; on EMIT, the **executor** hook sizes/captures/places.
- **Tier 2** (every 60 s): poll watchlist tickers for live mark-price crosses against stored swing references; promote to EMIT on confirmed cross + range expansion (also runs the executor hook).
- **Tier 3** (every 1 h): push heartbeat + active watchlist summary + recent top-mover candidates to Telegram. Doubles as engine-alive signal.
- **Exit engine** (every `EXIT_POLL_INTERVAL_SEC`, 60 s): mark every open position per-minute, apply the asymmetric +1h exit rule, write the intrabar dataset. See "Execution layer" below.

Single Python process, single asyncio event loop. **No threading (except the
sync↔async bridge in `lighter_exec`). No multiprocessing. No microservices. No
web dashboard. No ML training. No backtesting framework beyond `radar/replay.py`.**

---

## File map (skim before searching)

```
radar/
  main.py          asyncio orchestrator: tier1_discovery_scan + tier2_trigger_watch
                   + tier3_hourly_report + exit_engine.exit_loop, plus backfill,
                   _maybe_prune, and the two EMIT-branch executor hooks
  config.py        ALL tunables. Edit here to change behavior. EXECUTOR section
                   at the bottom (§2/§3/§5/§6 knobs, EXECUTOR_ENABLED/LIVE).
  lighter.py       live Lighter universe (~163 perps via mainnet API) — READ-ONLY
  lighter_exec.py  the WRITE layer: SignerClient adapter, order placement, native
                   server-side stops/TPs, fat-finger guard, COI idempotency,
                   stop-mandatory invariant, boot reconciliation. ⚠️ UNVERIFIED LIVE.
  universe.py      Market dataclass; sources from lighter.fetch_universe()
  storage.py       SQLite + Bar dataclass + watchlist helpers + swappable _now();
                   + the executor §10 tables and generic insert_row/update_row +
                   positions-lifecycle / circuit-breaker queries
  ranker.py        composite score + 1h/4h swing detection + has_breakout_structure
                   + percentile_rank (score_pctile for the executor)
  trade_plan.py    deterministic SL/TP ladder attached to every BOS alert
  executor.py      EMIT → tier-gate (§2) → size (§3) → circuit-breaker (§6) →
                   data-capture (§10) → (optionally) place. Fail-open, never blocks
                   the alert. classify_tier / compute_sizing / breaker_status pure.
  exit_engine.py   per-minute position marks + asymmetric +1h exit rule (§5);
                   simulates counterfactual exits in shadow mode
  catalysts.py     asset-routed news (RSS / EDGAR / yfinance / GDELT)
  classifier.py    LLM tool_choice (Groq primary) + substring evidence validator
  direction_adjudicator.py  LLM is the direction authority; conviction tiering
  predictor.py     Stage-2 full-context reasoner (enrichment only)
  beta.py          BTC-beta gate (residual returns + alpha_z)
  suppression.py   BOS-aware chain (enrichment-only; BOS is the only suppressor)
  telegram.py      send_bos_alert (with Plan: block) + send_watchlist_notification
  replay.py        virtual-clock historical playback
  fetch_bars.py    Coinbase → Bybit → Binance → CoinGecko fallback chain for crypto;
                   yfinance for equity / commodity / forex (=X / =F overrides);
                   ROUTES dict + TICKER_ROUTE_OVERRIDES + is_fetchable() helper
  fetch_news.py    GDELT historical news
  timefmt.py       CDT/CST formatting (America/Chicago) — user-facing only
tests/             240 passing; mock all external services (test_executor.py covers
                   tiering / sizing / breaker / exit logic / shadow capture)
data/              SQLite + sample data (bars.csv / news_archive.json gitignored)
BOS_FILTER_NOTES.md     rationale + tuning guide for the BOS filter
ENGINE_LOGIC_REFERENCE.md  full signal-path walkthrough (ranker → emit)
EXECUTOR_SPEC.md        the execution-layer build spec (v1.1) this code implements
deploy-quickref.md      end-to-end deploy walkthrough (ssh + docker compose)
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

1. **Rule 0 — Structural BOS check.** (v3.2: structure is the TRIGGER, not the
   direction — the `direction_adjudicator` is the authority on long/short/no_trade.)
   - BOS confirmed → remove any existing watchlist entry, continue to Rule 1.
   - No BOS + score ≥ `WATCHLIST_SCORE_THRESHOLD` + classifier dir ∈ {long, short} → WATCHLIST.
   - No BOS + low score → DROP `no_structure_break`.
2. **Rule 1 — Per-catalyst dedup (4 h).** Same ticker + same `catalyst_type` already EMIT'd → DROP `dedup_4h`.
3. **Rule 2 — BTC-beta gate (crypto only, OR-gated).** Drops if `|alpha_z| < 2.0` OR `|r_alpha| < 3 %`. **Bypassed when `current_bar.range > IMPULSE_BYPASS_MULTIPLIER * median_range` (2.5×).**
4. ~~**Rule 3 — Sector-day cluster.**~~ **REMOVED in v3.** `config.SECTOR_DAY_THRESHOLD` is vestigial.
5. **Rule 4 — Daily budget.** Hit `DAILY_ALERT_BUDGET` (30) → only above-median-score candidates fire.

**Survivors → EMIT `ok`.** EMITs run through Stage 1 (classifier) and Stage 2
(predictor) for **enrichment only** — never cause a DROP/DOWNGRADE post-EMIT.

---

## Execution layer (v1 — June 2026)

Implements `EXECUTOR_SPEC.md`. Turns each EMIT into a sized, risk-managed
position. The build order was schema → client → sizing → breaker → tiering →
exit; the runtime path is the reverse, hung off the two EMIT branches in
`main.py` via `executor.maybe_execute(market, plan, metadata, adjudicated, tier)`.

**Two-stage master switch (`config.py`).**
- `EXECUTOR_ENABLED` (default `True`) — run the full decision + **data-capture**
  pipeline on every EMIT and let the exit engine **simulate counterfactual
  exits**. Never touches the exchange. This is "shadow mode" and is the actual
  v1 deliverable (the intrabar dataset the close-to-close backtest never had).
- `EXECUTOR_LIVE` (default `False`) — additionally place **real-money** orders on
  Lighter. Requires the `lighter-sdk` importable + `LIGHTER_PRIVATE_KEY` /
  `LIGHTER_ACCOUNT_INDEX` in `.env`. ⚠️ **The live path in `lighter_exec.py` is
  UNVERIFIED.** Do not flip this on until the G0 runbook (below) passes.

**The pipeline (`executor.py`, all fail-open — a bug here never blocks the alert):**
1. **§2 tier-gate** — `classify_tier(alpha_z, score_pctile, cluster_size,
   btc_ret_4h, vol_ratio, asset_class)`. SKIP-traps (inert `crypto_t1`, blowoff
   `vol_ratio>15 & |alpha_z|<3`, the `|alpha_z|∈[2,3) & pctile≥50` trap) are
   checked **before** tier assignment so a high score with marginal alpha_z is
   never mistaken for Tier B. **v1 ships Tier A only** (`EXECUTOR_ENABLED_TIERS={"A"}`).
2. **§3 sizing** — `compute_sizing`: `size_usd = MAX_LOSS_PER_TRADE_USD /
   (risk_per_unit/entry)`. The dollar loss at the stop is **fixed regardless of
   stop width**. Leverage never determines size (the 50× liquidation lesson);
   it only caps margin posted. Score sizes *up within Tier A* (`×clamp(pctile/75,
   1.0, 1.5)`), never overrides the gate.
3. **§6 circuit breaker** — `breaker_status`: kill-switch file, daily max loss,
   daily max trades, consecutive-loss halt. Trips → Telegram ping + skip new
   entries (existing server-side stops stay live).
4. Concurrency + total-exposure caps, then **§10 capture** (always, even on
   skip) → `signal_snapshots` + `executions` rows, stamped with `config_version_id`.
5. Place: shadow → insert a simulated `positions` row; live → `lighter_exec.open_position`.

**§4 order client (`lighter_exec.py`).** SignerClient adapter. Native server-side
`STOP_LOSS` / `TAKE_PROFIT` (stops live on the exchange — a VPS death does not
unprotect). Invariants: **fat-finger guard** (reject >`FATFINGER_PCT` from mark),
**COI idempotency** (`uint48(hash(alert_ts, ticker, leg))` so a restart dedupes),
**stop-mandatory** (if the protective stop fails to post after an entry fills,
immediately market-close the entry), **boot reconciliation** (never start blind).

**§5 exit engine (`exit_engine.py`).** 60 s loop. Per-minute `position_marks`
(the #1 missing backtest variable) + MFE/MAE/pnl-at-1h. Asymmetric rule: cut
flat/red at +1h; let a Tier-A runner that has cleared `EXTENSION_THRESHOLD_R`
ride to +4h on a breakeven-then-trail; blowoff/meme/cluster force a +1h close.
In shadow it marks the position closed in the DB at the trigger price with the
counterfactual PnL; in live it routes the close/BE-move through `lighter_exec`.

**§10 schema (`storage.py`).** Eight tables — `config_versions`,
`signal_snapshots`, `executions`, `orders`, `fills`, `positions`,
`position_marks`, `equity_snapshots`. Principle: **capture raw, derive later**;
every trade-linked row carries `config_version_id` so a threshold change never
contaminates the dataset.

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
12. **No new dependencies.** `pyproject.toml` is pinned (`lighter-sdk` is already in it via a git URL — it just isn't installed in every venv). Ask first before adding anything.
13. **`main.py` module state powers the hourly report.** `_LAST_SCAN_TS`, `_LAST_TOP_CANDIDATES`, `_LAST_PRUNE_TS` are set at end of Tier 1. Keep those writes if refactoring — Tier 3 reads them via `_format_hourly_report()`.
14. **`fetch_bars` row schema uses ISO `ts`, storage uses unix int.** Backfill goes through `_parse_iso_to_unix` (`%Y-%m-%dT%H:%M:%SZ`). Don't change `_iso()` without updating that parser.
15. **`TICKER_ROUTE_OVERRIDES` lives in `fetch_bars.py`.** Use when a ticker's Lighter `asset_class` doesn't match where it trades (e.g. PAXG is "commodity" but routes through Binance).
16. **Telegram failures NEVER raise out of the hourly-report send.** `_send_hourly_report` wraps in try/except. Don't bubble exceptions out of Tier 3.
17. **`executor.maybe_execute` is fail-open — it must NEVER raise.** The alert has already been sent when it runs; a bug here cannot block trading transparency. Both EMIT-branch hooks in `main.py` also wrap it in their own try/except. Keep both layers.
18. **`EXECUTOR_LIVE` defaults `False`; the live path is UNVERIFIED.** `lighter_exec.py` is written against the documented SDK surface but has never placed a real order. Pass the G0 runbook (1-contract place→stop→kill→reconcile→cancel→flatten) before flipping it on. Shadow mode (`EXECUTOR_ENABLED` only) is safe and is what runs by default.
19. **`classify_tier` / `compute_sizing` / `breaker_status` / `evaluate_exit` are PURE** (no I/O) so they're unit-tested directly in `tests/test_executor.py`. Keep them pure — DB writes live in `maybe_execute` / the exit loop.
20. **SKIP-traps are checked before positive tiers in `classify_tier`.** Order matters: a `pctile≥75` candidate with `|alpha_z|∈[2,3)` is the 16.7%-WR trap, NOT Tier B. Don't reorder.
21. **Sizing fixes the dollar loss at the stop; leverage never sizes.** `MAX_LOSS_PER_TRADE_USD` is the only size lever. `LEVERAGE_CAP` caps margin posted, nothing else. A 10% adverse move can't liquidate a position whose stop is −$N.
22. **`storage.insert_row` / `update_row` interpolate the table + column names** (code-controlled, never user input) but parametrize all *values*. Use them for the executor tables; don't hand-roll SQL per table. `positions.exit_ts IS NULL` ⇔ open.
23. **The exit loop has its own `_RUNNING` flag.** `exit_engine.stop()` is called from `main._graceful_shutdown` alongside the Tier loops' flag. If you add another long-lived loop, wire its shutdown the same way.

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
| **Lighter mainnet API** | unrestricted (so far) | universe (read) + orders (write) | ~163 active perps as of May 2026. Read cached 60 s in `radar.lighter`; writes go through `radar.lighter_exec` (SignerClient, `.env` key). |
| **Groq (Llama 3.3 70B)** | account-tier | classifier + Stage-2 predictor | OpenAI-compatible `json_object`. Primary LLM. Gemini / Anthropic Haiku are fallbacks. Substring validator drops fabricated quotes. |

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
pytest tests/                          # all 240 should pass
pytest tests/test_executor.py -x       # executor: tiering / sizing / breaker / exit
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

# end-to-end run — shadow mode by default (EXECUTOR_LIVE=False)
# needs GROQ_API_KEY (or ANTHROPIC/GEMINI) + TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
python -m radar.main

# Telegram smoke test
python scripts/telegram_smoketest.py

# deploy — full walkthrough in deploy-quickref.md
RADAR_HOST=user@host ./deploy.sh
```

### G0 — first live order verification (do this before trusting `EXECUTOR_LIVE`)

The live path in `lighter_exec.py` is unverified. Before relying on it, run the
manual smoke test below with a tiny risk budget. Requires `pip install -e .`
(so `lighter-sdk` resolves) and `LIGHTER_PRIVATE_KEY` + `LIGHTER_ACCOUNT_INDEX`
+ `LIGHTER_API_KEY` in `.env`.

```bash
# 1. Shrink risk to $1 and narrow the universe to BTC so only a BTC EMIT can fire.
#    In radar/config.py:  MAX_LOSS_PER_TRADE_USD = 1.0
#                         EXECUTOR_LIVE = True
#    (leave EXECUTOR_ENABLED_TIERS = {"A"})

# 2. Start the bot, wait for a BTC Tier-A EMIT to place a 1-contract market buy.
python -m radar.main
#    → confirm in the Lighter UI: position open AND a server-side stop is resting.

# 3. Kill the bot mid-position (either signal works):
kill -TERM <pid>           # graceful: finishes the cycle, stops the loops
#   — or — touch the kill-switch file (halts new entries; stops stay live):
touch /tmp/radar_halt

# 4. Restart. Boot reconciliation (§4.6) queries live positions/orders and logs
#    each open position + whether a tracked stop exists. Watch the logs:
python -m radar.main       # grep "RECONCILE" / "reconciliation found"

# 5. Cancel the stop + flatten manually in the Lighter UI when done. Remove
#    /tmp/radar_halt and revert config (EXECUTOR_LIVE=False, MAX_LOSS back to 5.0).
```

Passing G0 satisfies the `EXECUTOR_SPEC.md` §9 G0 gate. Only then widen aperture
(Tier B, then C-pop) per §9 G3 — by data, not by feeling.

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
- **Execution layer is built and running in shadow mode** (`EXECUTOR_ENABLED=True`,
  `EXECUTOR_LIVE=False`). It captures the full §10 dataset and simulates exits on
  every EMIT; it has not placed a real order yet.
- **Next milestone: G0** — the first live 1-contract BTC order at $1 risk, with
  the kill → restart → reconcile → cancel → flatten loop (runbook in "Common
  commands" above). Until G0 passes, treat every backtest win-rate as a
  hypothesis (`EXECUTOR_SPEC.md` §7) and keep `EXECUTOR_LIVE` off.
- After G0: widen to Tier B then C-pop only when `position_marks` confirms stops
  survive intrabar noise (§9 G2/G3).

---

(Multi-timeframe BOS confirmation was previously listed as an anti-feature;
it's now Phase 1 of the engine — see "BOS engine" above.)
