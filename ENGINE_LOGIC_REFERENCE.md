# Catalyst Radar — Engine Logic Reference

> A complete, code-faithful description of how this engine perceives a ticker:
> every signal it computes, every price level it derives, every gate it applies,
> and every decision branch. Written as design context for building the next
> (automated, execution-capable) engine. All constants below are quoted from
> `radar/config.py` as of the current `master`.

---

## 0. Mental model — what the engine actually does

The engine answers one question per ticker, every cycle:

> *"Did price just break a structural level (a prior swing pivot) with a
> conviction-grade expansion in volatility — and if so, which direction do I
> tell the user to trade?"*

Everything is built around that. The **structural break of structure (BOS) is
the only thing that can fire or suppress an alert.** Every other layer —
the composite "unusualness" score, the LLM news classifier, the order-book
imbalance, OI/funding, the volume profile, even the Stage-2 reasoner — is
*enrichment*. They annotate the alert or pick its direction, but **they never
block a confirmed structural break.** This is the v3 "no-suppression
invariant," and it's the single most important design decision to carry forward
or consciously reject.

The pipeline per ticker:

```
universe (Lighter perps)
   → snapshot price/volume/24h%  (lighter /exchangeStats)
   → fold tick into in-progress 1h + 15m OHLC bars (we synthesize bars ourselves)
   → composite ranker score  → top-N candidates (cheap pre-filter, NOT a gate on BOS)
   → for each candidate: has_breakout_structure()  (the real trigger)
       ├─ no BOS → DROP (or WATCHLIST if score high + LLM has a direction)
       └─ BOS    → LLM classify news (enrichment)
                 → suppression chain (dedup, BTC-beta, daily budget)
                 → direction adjudicator (LLM picks long/short/no_trade)
                 → deterministic trade plan (entry/stop/TP ladder)
                 → Telegram
```

Two background timeframes feed BOS: a **5-min discovery loop (Tier 1)** that
does the full scan, and a **60-sec watchlist poll (Tier 2)** that watches
near-miss tickers for a live cross against pre-stored levels.

---

## 1. The universe — which tickers exist at all

**Source of truth: the live Lighter DEX perp list.** Nothing outside Lighter is
visible to the engine. `radar/lighter.py:fetch_universe()` hits
`https://mainnet.zklighter.elliot.ai/api/v1/orderBooks`, keeps only rows where
`market_type == "perp"` and `status == "active"` (~163 perps), and caches the
result 60 s.

Live prices/volume/24h% come from a *separate* endpoint, `/exchangeStats`
(`fetch_market_stats()`, cached 30 s). The orderBooks endpoint has metadata
only — **without the stats merge every ticker has price = 0 and the ranker
produces nothing.** Two endpoints, two cache TTLs.

Fields the engine gets per ticker (`Market` dataclass, `universe.py`):
`ticker, asset_class, market_id, price, volume_24h_usd, pct_24h`. Note what is
**NOT** populated from Lighter: `oi_usd = 0`, `funding_1h = 0`, `pct_1h = 0`.
OI and funding are simply not exposed by Lighter's public stats; the engine
runs without them (their ranker components contribute 0). `pct_1h` is derived
downstream from the bar history we build ourselves.

### 1.1 Asset-class classification (`lighter.classify()`)

Each symbol is mapped to exactly one of six classes by ordered membership
checks (first match wins):

1. `*USD`-suffixed foreign/pre-IPO equities (`HYUNDAIUSD`, `SAMSUNGUSD`, `SKHYNIXUSD`) → **equity**
2. forex pairs (`EURUSD`, `USDJPY`, … 8 pairs) → **forex**
3. commodities (`XAU`, `XAG`, `XPT`, `XPD`, `XCU`, `BRENTOIL`, `WTI`, `NATGAS`, `PAXG`) → **commodity**
4. known US equities/ETFs (hardcoded sets) → **equity**
5. tier-1 crypto majors (`BTC, ETH, SOL, …`) → **crypto_t1**
6. `1000`-prefixed symbols or known memes → **crypto_meme**
7. **default → crypto_t2** (this is a crypto-first DEX; anything unrecognized is a mid-cap perp)

The class matters because it drives: the ranker class-multiplier, the cold-start
%-move threshold, whether the BTC-beta gate applies, the news source routing,
and the price-data source routing. **Misclassification silently changes a
ticker's entire treatment** — e.g. PAXG is commodity-classed but trades on
crypto venues, so it has a per-ticker route override (see §3.3).

---

## 2. The composite ranker score — "how unusual is this ticker right now"

`ranker.compute_score(market, history)`. This is a **cheap volatility-normalized
anomaly score** used only to pick the top-N candidates worth doing expensive
work on. **It does not gate BOS** — a low-scoring ticker can still EMIT if it
breaks structure; a high-scoring ticker still DROPs without a break. Think of it
as attention allocation, not signal.

```
score = ( Σ  weight_i · z_i )  ·  class_multiplier
```

### 2.1 Components (each clipped to [-10, 10] via z-score helper)

| Component | Weight | Formula |
|---|---|---|
| `pop_score` | **+1.0** | `|pct_1h| / 100 / stdev(1h returns over 30d)`, capped at 10. Cold-start fallback divides by a fixed `0.02` typical hourly sigma. |
| `oi_velocity_z` | **+0.7** | z-score of the *last* 1h ΔOI vs the prior ΔOI series. Needs ≥4 OI points; with Lighter OI=0 this is usually 0. |
| `volume_z` | **+0.5** | z-score of current 1h volume vs the rolling volume series. |
| `funding_z` | **+0.4** | z-score of current funding vs funding history. Usually 0 (no funding from Lighter). |
| `wash_penalty` | **−0.5** | `1.0` if `volume_24h_usd / oi_usd > 50` (turnover proxy for wash trading), else `0.0`. |

In practice, with Lighter not supplying OI/funding, **`pop_score` and
`volume_z` carry the score.** `pop_score` is the dominant term — it's just the
1h move expressed in standard deviations of that ticker's own recent hourly
volatility. A 5% hourly move on a normally-1%-sigma name scores ~5; the same 5%
on a meme that routinely swings 5%/hr scores ~1. That vol-normalization is the
whole point: it surfaces *abnormal-for-this-asset* movement, not just big
percentage numbers.

### 2.2 The z-score helper (`_z`) — note the constant-series carve-out

Standard z = `(value − mean) / std` clipped to ±10. But if the historical series
is **flat (std = 0)**, it instead returns `(value − mean) / |mean|` clipped — so
a spike against a dead-flat history still scores instead of dividing by zero.
`std`/`mean` use only finite values; series < 2 points → std 0.

### 2.3 Class multipliers

```
crypto_t1: 1.0   crypto_t2: 1.1   crypto_meme: 0.7   equity: 1.0   commodity: 1.0
```

Memes are *down*-weighted (0.7) because their raw moves are noisy; mid-cap
crypto (t2) is slightly *up*-weighted (1.1) as the sweet spot for catalyst
plays. (Note: `forex` has no entry, so it falls to the `1.0` default.)

### 2.4 Candidate selection (`top_n_movers`)

1. Drop anything with `volume_24h_usd < MIN_VOLUME_24H_USD` (**$50,000**).
2. **Cold-start guard:** if a ticker has < 24h of return history, the score
   alone isn't trusted — it must *also* clear an absolute |1h move| threshold:
   **5%** for `crypto_t1`/`equity`, **10%** for everything else. Otherwise it's
   dropped regardless of score.
3. Sort by score desc, return top **`TOP_N_CANDIDATES = 10`**.

Only these 10 go through BOS + the expensive LLM/book/news work each cycle.

---

## 3. Data the engine sees — bars, timeframes, and how they're built

The engine reasons over three timeframes, but **only stores two** (1h and 15m);
the 4h frame is synthesized on demand.

### 3.1 We build our own bars (critical gotcha)

Lighter gives a *current price only* — no in-progress OHLC. So every Tier-1 tick
(`main._record_market_bar`) folds the live mark price into **both** the current
1h bucket and the current 15m bucket via `upsert_bar_*_from_tick`. The bucket's
high/low/close update from successive ticks; open is set once.

**Why this matters:** if you don't aggregate ticks yourself, the in-progress
bar's high == low, range == 0, the range-expansion gate never opens, and *every
cycle returns `DROP no_structure_break`.* This is called out as the #1 silent-
failure mode. Any successor engine pulling from a price-only feed must
synthesize bars the same way.

- 1h bucket key: `floor(now/3600)*3600`
- 15m bucket key: `floor_to_15m_bucket(now)`

Storage time convention: **unix-int seconds** for `bars_1h.ts` / `bars_15m.ts`
and `alerts.created_at`; **ISO-8601 naive-UTC strings** for everything in the
`watchlist` table. User-facing display only is CDT (America/Chicago). Never mix.

### 3.2 Backfill at boot (so BOS isn't blind for ~5.5 days)

The 4h structural frame needs ≥ ~33 4h-bars ≈ 132 hours of 1h data before it can
fire. To avoid sitting blind on a fresh start, `_backfill_bars_for_universe()`
fetches the missing tail per ticker:

- **Target window:** `BOS_BAR_HISTORY_HOURS = 240` (10 days) for crypto.
- **Equities/commodities override:** `60*24` hours, because they trade ~6.5h/day
  — 240 *calendar* hours would yield only ~65 bars, below the 4h floor.
  yfinance caps intraday `period` at 60 days, so equities re-trigger a full
  backfill every boot (they never reach the density floor).
- **Two-stage skip logic:** (1) density — if < 50% of the 240h window is
  populated, fetch the full target; (2) freshness — if dense *and* the last bar
  is < 1h old, skip; else fetch just the gap tail.
- 15m backfill (`BOS_15M_HISTORY_BARS = 200` ≈ 50h) runs only for crypto
  (Coinbase/Bybit 15m); equity/commodity/forex have no 15m route and fall back
  to 1h-only BOS.

Backfill is idempotent (`INSERT OR REPLACE`), paced (0.6 s between tickers),
timeout-capped (30 s/ticker), and cancellable on SIGTERM.

### 3.3 Historical bar source chain (`fetch_bars.py`)

Routed by asset class, with fallbacks:

| Class | Source chain | Notes |
|---|---|---|
| crypto | **Coinbase → Bybit → Binance → CoinGecko** | Coinbase 300 candles/call; Bybit 1000; Binance memes use `1000PEPE`/`1000BONK` scaling (harmless — only relative moves matter); CoinGecko synthesizes OHLC from prev-close and needs an explicit slug map (`COINGECKO_IDS`). |
| equity / commodity / forex | **yfinance** | `=F` futures (XAU→`GC=F`), `=X` forex (`EURUSD=X`), Korean ADRs remapped to `.KS` primary listings. |

`TICKER_ROUTE_OVERRIDES` handles class/venue mismatches (PAXG classed commodity
but routed through crypto fetchers). `is_fetchable()` reports coverage.

---

## 4. The BOS engine — structural break detection (the actual trigger)

`ranker.has_breakout_structure(market, history, current_price, history_15m)`
returns a **4-tuple** `(broke, direction, breakout_level, structure_type)` where
`structure_type ∈ {"4h_and_1h", "4h", "1h", None}`.

Conceptually a break fires when **two independent conditions both hold:**

1. **Price crosses a prior swing pivot** (a level the market had been respecting).
2. **The in-progress bar shows range expansion** (a real impulse, not drift).

These are evaluated independently on a **4h frame** and a **1h frame, in
parallel.** Whichever fires triggers; both same-direction = highest conviction.

### 4.1 Swing pivot detection (`find_swing_high` / `find_swing_low`)

A swing high is the **highest bar high within an eligible window that has not
been exceeded by any subsequent bar** (and has held for ≥ `min_bars_validation`
bars). The eligible window excludes the most recent `min_age` bars so the
breakout candle's own wick can't be picked as its own reference.

Two-pass "fallback ladder":
- **Strict pass:** sort eligible bars by high desc; return the first one no later
  bar exceeded. `bars_validated` = count of confirming bars.
- **Fallback (Donchian):** if *every* candidate was later broken (i.e. a clean
  uptrend ratcheting to new highs — exactly when you most need a reference), return
  the absolute highest in the window with `bars_validated = 0` as the flag.

**Do not remove the fallback** — strict logic returns `None` in trending markets,
losing the reference precisely at breakout time. `find_swing_low` mirrors this.

### 4.2 4h frame synthesis (`synthesize_4h_bars`)

1h bars are resampled into **UTC-aligned** 4h buckets (00/04/08/12/16/20). OHLC
from first-open/extremes/last-close; volume summed; **OI uses last (it's a stock,
not a flow)**; funding averaged. The in-progress 4h bucket is included.

### 4.3 Range-expansion confirmation (`_confirm_range_expansion`)

For a given timeframe: take the lookback window (excluding the current bar),
require ≥10 bars, compute **median (high−low)**, and check:

```
current_bar_range  >  multiplier × median_range
```

Volume confirmation is wired but **disabled** (`REQUIRE_VOLUME_CONFIRMATION =
False`) — volume lags price on real catalyst breakouts, and blocking on it was
silently suppressing real breaks. Range is the primary signal; volume is a badge.

### 4.4 The three confirmation gates (computed once, up front)

| Gate | Multiplier | Baseline window |
|---|---|---|
| 1h-for-4h | `RANGE_EXPANSION_MULTIPLIER = 1.5×` | 48 1h-bars (`SWING_LOOKBACK_HOURS`) |
| 1h-native | `RANGE_EXPANSION_MULTIPLIER_1H_ENTRY = 1.5×` | 24 1h-bars (`SWING_LOOKBACK_1H_BOS_BARS`) |
| 15m | `RANGE_EXPANSION_MULTIPLIER_15M = 2.5×` | 96 15m-bars (= 24h) |

15m is the **latency shrinker**: it cuts alert lag from ~10–30 min (1h-only) to
~1–5 min on real impulses. Its multiplier is higher (2.5× vs 1.5×) because 15m
baselines are noisier.

### 4.5 4h path

Fires only if `(1h-for-4h confirmed) OR (15m confirmed)`, **and** there are
enough synthesized 4h bars (`SWING_LOOKBACK_4H_BARS(20) + age(1) + validation(1)`
= 22). Then:
- find 4h swing high; if `price > swing_high` → `(True, "long", level)`
- else find 4h swing low; if `price < swing_low` → `(True, "short", level)`

Lookback = **20 4h-bars ≈ 3.3 days** of structure. Validation = 1 4h-bar (loose,
fast confirmation). HTF trend alignment is wired but **disabled**
(`REQUIRE_HTF_TREND_ALIGNMENT = False`) — counter-trend breaks fire too.

### 4.6 1h path (early-detection / cold-start, `BOS_1H_ENABLED = True`)

Fires only if `(1h-native confirmed) OR (15m confirmed)`, **and** ≥ 26 1h-bars
(`24 + 1 + 1`). Symmetric to the 4h path but on native 1h pivots, lookback = **24
1h-bars = 1 day**. This is what lets a fresh ticker (only ~26 bars) fire before
it has enough history for the 4h frame.

### 4.7 Combining the two paths

```
both fire, same direction  → (True, dir, 4h_level, "4h_and_1h")   # highest conviction, wider 4h stop
4h fires (alone or conflict)→ (True, dir_4h, level_4h, "4h")       # 4h wins direction conflicts
1h fires alone             → (True, dir_1h, level_1h, "1h")
neither                    → (False, None, None, None)
```

The returned `breakout_level` is the pivot the trade plan builds its stop
against. When both fire, the **4h level is used** (wider, more structural stop).

### 4.8 Cold-start summary

- < 26 1h-bars → always `(False, …)` → DROP or WATCHLIST.
- 26+ bars → 1h path can fire.
- ~132+ hours (≥ 22 synth 4h-bars) → 4h path can fire and takes priority.

---

## 5. The suppression chain — the gates (`suppression.evaluate`)

Returns `(decision, reason, metadata)` where decision ∈ `{EMIT, WATCHLIST,
DROP}`. Rules in order, first match wins. **Remember: only Rule 0 (structure)
and mechanical throttles can stop an alert. No enrichment layer ever DROPs.**

### Rule 0 — Structural BOS / watchlist routing

- **BOS confirmed** → remove any existing watchlist entry for the ticker, fall
  through to Rules 1–4. (Direction conflict between structure and the LLM is
  *not* resolved here anymore — the downstream adjudicator owns direction.)
- **No BOS, score ≥ `WATCHLIST_SCORE_THRESHOLD (60)`, and classifier direction
  ∈ {long, short}** → add to watchlist with pre-computed 4h references, return
  `WATCHLIST`.
- **No BOS otherwise** → `DROP no_structure_break`.

> In the live Tier-1 loop there's an additional **cost gate before Rule 0**: if
> the pre-check `has_breakout_structure` is False, the candidate is dropped
> immediately *without* calling the LLM at all (`SKIP_CLASSIFIER_IF_HOPELESS`).
> This saves ~70–90% of LLM calls. The LLM only runs on confirmed breaks.

### Rule 1 — Per-catalyst dedup (4h)

If an EMIT for this **ticker + same `catalyst_type`** fired in the last
`DEDUP_HOURS = 4` → `DROP dedup_4h`. (Same ticker, *different* catalyst, still
fires.)

### Rule 2 — BTC-beta gate (crypto only, OR-gated, impulse-bypassed)

Drops a crypto alert if its move is "just BTC beta" — i.e. not idiosyncratic:

```
DROP pure_btc_beta  IF  |alpha_z| < ALPHA_Z_MIN (2.0)  OR  |r_alpha_pct| < R_ALPHA_MIN_PCT (3.0%)
```

This is **stricter than the legacy AND-gate** (either weak measure kills it).
**But it's bypassed entirely** when the current bar is a high-conviction impulse:

```
bypass IF current_bar_range > IMPULSE_BYPASS_MULTIPLIER (2.5×) × median_range
```

Rationale: a >2.5× range break is a genuine move regardless of BTC correlation;
filtering it lost first-leg breakouts (the engine's historical weakness — it only
caught continuation breaks after the 24h move had already grown past 3%).

### Rule 3 — REMOVED (v3)

Sector-day clustering used to suppress the Nth alert in a hot sector. Deleted —
it was killing leg-following alerts. `SECTOR_DAY_THRESHOLD` is vestigial.

### Rule 4 — Daily budget throttle

Once daily emits hit `DAILY_ALERT_BUDGET = 30`, **only above-median-score**
candidates break through (`DROP budget_throttle`). This is the only volume cap,
and it's soft.

Survivors → `("EMIT", "ok", metadata)`.

### 5.1 The metadata dict (threaded everywhere)

```
breakout_level, structure_direction, structure_type,
swing_high_reference (4h), swing_low_reference (4h),
swing_reference_timestamp, median_bar_range (1h)
```
Plus, added by the Tier-1 loop on EMIT: `book_sentiment`, `book_ratio`,
`vpoc_price`, `vpoc_near_breakout`, `adjudicated`, `predictor_result`.

---

## 6. The BTC-beta gate math (`beta.compute_alpha_z`)

For crypto, regress the ticker's 1h returns on BTC's 1h returns over 30 days,
isolate the idiosyncratic residual, and z-score the latest residual.

```
β        = cov(r_ticker, r_btc) / var(r_btc)        # OLS slope, no intercept, needs ≥24 pts
residual = r_ticker − β · r_btc                      # per-bar alpha
alpha_z  = (residual[-1] − mean(residual[:-1])) / std(residual[:-1])   # clipped ±50
r_alpha_pct = residual[-1] × 100                      # latest alpha in % units
```

- **Non-crypto**: gate is a no-op → `alpha_z = +inf`, `r_alpha_pct = pct_24h`.
- **No BTC history / < 24 pts**: also passes (`+inf`, `pct_24h`) — never blocks
  on missing data.

Interpretation: `alpha_z` = "how unusual is this ticker's BTC-independent move
vs its own residual history"; `r_alpha_pct` = "how big is that independent move."
Both must be meaningful (≥2σ AND ≥3%) or the move is dismissed as beta —
**unless** the impulse bypass fires.

---

## 7. Price-level derivation — the trade plan (`trade_plan.compute_plan`)

Fully **deterministic**, no LLM, advisory only (the engine never trades). Built
from the adjudicated direction + the BOS `breakout_level` + bar history.

```
entry = current price at alert time
buffer = STOP_BUFFER_PCT = 0.2%

LONG:   stop = breakout_level × (1 − 0.002)
SHORT:  stop = breakout_level × (1 + 0.002)

risk_per_unit = |entry − stop|
# Sanity gate: if risk < MIN_RISK_PCT_OF_ENTRY (0.05%) of entry → return None
#   (the break is too tight to be a real trade)

TP1 = entry ± TP1_R_MULTIPLE (1.5) × risk          # 1.5R
TP2 = the NEXT prior swing beyond TP1, OR TP2_FALLBACK_R_MULTIPLE (3.0)R,
        whichever is CLOSER (but never worse than TP1)
```

`TP2`'s "next swing" is the nearest historical bar high above TP1 (long) / low
below TP1 (short), excluding the in-progress bar. Capping at the structural
target *or* 3R keeps TP2 realistic.

### 7.1 Multi-stage exit ladder

Static cut at 1.5R was throwing away the breakout edge (winners run 5–15R). So:

```
scale TP1_FRACTION (33%) at TP1
move stop → entry once price reaches BREAKEVEN_TRIGGER_R (1.0R)
scale TP2_FRACTION (33%) at TP2
trail remaining ~34% (runner) by TRAIL_ATR_MULT (1.5) × ATR(ATR_PERIOD_HOURS = 14h)
```

ATR is Wilder true-range mean over 14 bars (`ranker.compute_atr`); needs ≥15
usable bars or the trail collapses (TP1/TP2 still fire as static targets).

### 7.2 Volume-profile POC (advisory badge only)

`compute_volume_profile_poc` buckets close-prices into 30 volume bins and
returns the highest-volume price (point of control). `is_breakout_near_poc`
flags when the breakout level is within ±0.5% of the POC — a break aligned with
the real volume node is structurally bigger than a break above a random recent
high. **Badge only, never a gate.**

---

## 8. Direction adjudication — which way to call it (`direction_adjudicator` + `predictor`)

The structural cross is the *trigger*; it does **not** decide direction. A break
above a swing high is a fact, but the next move could be a liquidity sweep that
reverses. So the final long/short/no_trade call is made by an LLM (Groq + Llama
3.3 70B) handed a structured signal bundle. On any LLM failure it **falls back to
the structural direction** with `OK` conviction — the alert always sends.

### 8.1 The signal bundle the LLM weighs (`_build_signal_bundle`)

All computed from in-memory data + one live order-book fetch:

- **Structural prior**: frame (`4h`/`1h`/`4h_and_1h`), structural direction,
  pivot level, current price, **distance past pivot %**, range ratios (1h & 15m).
- **Sweep evidence (`_wick_analysis`)**: did the wick cross the pivot but the
  body close back inside? Computed for the current 1h *and* 15m bar. A sweep is
  the strongest reason to flip or no_trade. (Returns labels like "⚠️ SWEEP",
  "~ MARGINAL", "✓ CLEAN".)
- **Order-book imbalance**: live top-10-level bid/ask USD from Lighter
  (`orderBookOrders`), ratio → label (`imbalance_sentiment`): >2 strongly
  bullish, >1.3 bullish, 0.77–1.3 neutral, <0.77 bearish, <0.5 strongly bearish.
- **Positioning**: OI now, 1h OI delta %, funding %, current-bar volume vs 30d
  median (ratio + z).
- **HTF/macro**: 7d trend alignment (median close), BTC 24h move %.
- **Timing**: tier (1 vs 2), watchlist age, UTC hour (Asia-hours moves fade; NY
  open/close carry weight).

### 8.2 The decision rules (system prompt, `predictor.py`)

- Default: **confirm** structural direction at high confidence when 3+ signals agree.
- **Flip** only when sweep evidence is clear AND ≥2 other signals point opposite.
- **no_trade** when signals are genuinely split (structure long, catalyst short,
  book neutral, OI mixed). Encouraged liberally — "a missed setup costs nothing;
  a wrong-way alert loses money."
- Confidence is an honest 0–100 integer (the user sizes off it).

Output normalized to `PredictorResult(final_direction, direction_confidence ∈
[0,1], setup_quality, thesis, …)`. Guards: bad direction → structural fallback;
`no_trade` blocked if `DIR_ALLOW_NO_TRADE=False`; flip blocked if
`DIR_ALLOW_FLIP=False`.

### 8.3 Conviction tiering (drives Telegram rendering)

```
score = sqrt(direction_confidence × setup_quality)     # geometric mean; one near 0 → collapses
STRONG    if score ≥ DIR_CONVICTION_STRONG (0.75)
OK        if score ≥ DIR_CONVICTION_OK (0.50)
TENTATIVE if score ≥ DIR_CONVICTION_TENTATIVE (0.30)
NO_TRADE  otherwise (or final_direction == "no_trade")
```

The adjudicator runs at **both** Tier-1 fire and Tier-2 promotion (a stale
watchlist bias gets re-voted against live state). `no_trade` still emits a ⏸
alert (thesis, no entry/stop/TP block).

---

## 9. News catalysts + LLM classifier (Stage 1 — pure enrichment)

### 9.1 News sourcing (`catalysts.fetch_for_market`, routed by class)

| Class | Sources |
|---|---|
| crypto | RSS (CoinDesk/Decrypt/TheBlock, substring-matched on ticker) + Coinalyze funding surge (key-gated) + DeFiLlama unlocks (14-day forward window) |
| equity | yfinance news + EDGAR 8-K Atom feed |
| commodity | EIA weekly stocks (WTI/Brent, key-gated) + GDELT (`{gold/silver/crude} prices`) |
| forex | none (no route) |

Every fetcher is defensive (exception → `[]`). Output deduped by URL hash,
sorted newest-first, capped at `NEWS_MAX_ITEMS = 8`, lookback `NEWS_LOOKBACK_HOURS
= 24`.

### 9.2 Classifier (`classifier.classify`)

Default provider **Groq / Llama 3.3 70B** (JSON mode); Gemini and Anthropic
Haiku kept as fallbacks. Forces a structured `ClassifierResult`:
`catalyst_type` (13-value enum), `direction` (long/short/neutral), `confidence`,
`summary`, `evidence_quotes`, `is_actionable`.

**Anti-hallucination substring validator:** every `evidence_quote` must be a
verbatim substring of the news corpus, or the whole classification is dropped.
Empty quotes are allowed (model said "none").

**v3 behavior:** the classifier runs even with no news (`"(no news items
found)"`), and its output **never blocks an alert.** On `None` (LLM error /
fabrication), the Tier-1 loop substitutes a neutral placeholder and proceeds.
The catalyst feeds the watchlist direction-bias and is one input the adjudicator
weighs — that's all.

---

## 10. Orchestration & the watchlist lifecycle

Three asyncio loops, one process, no threading (Telegram sends use `requests`
synchronously wrapped in `to_thread` — never reintroduce `python-telegram-bot`,
its httpx client binds to the first event loop and dies on the second send).

### Tier 1 — discovery (`FAST_CADENCE_SEC = 60`s loop, conceptually the 5-min scan)
universe → snapshot+bar-fold every ticker → rank → for each top-10 candidate:
BOS pre-check → (no BOS: record DROP, skip LLM) → book + news + classify →
suppression → on EMIT: VPOC badge + adjudicator + trade plan + Telegram; on
WATCHLIST: store refs + notify. Auto-prune piggybacks here (once/day).

### Tier 2 — trigger watch (`TRIGGER_POLL_INTERVAL_SEC = 60`s)
Polls each active watchlist entry. Uses the **lightweight stored-reference check**
(`check_breakout_against_stored_references`) — never re-runs full swing detection.
Fires only when current bar range > 1.5× stored median **and** live price crosses
the stored 4h reference *in the entry's bias direction*. On fire: re-vote
direction via adjudicator, build plan, EMIT, remove from watchlist.

### Watchlist
- Entry condition: no BOS yet, score ≥ 60, classifier has a direction.
- Stores 4h swing refs + 1h median range + serialized classifier result.
- TTL `WATCHLIST_TTL_HOURS = 72` (long, to capture multi-day catalyst arcs).
- BOS confirmation supersedes & removes the entry.

### Tier 3 — hourly heartbeat (`HOURLY_REPORT_INTERVAL_SEC = 3600`, offset :05 past the hour)
Watchlist summary + recent top movers + "engine alive" signal. Aligned to :05 so
the just-closed 1h bar is already scored, and to land near 4h-bar closes
(00/04/08/12/16/20 UTC + 5m) — the actionable slots. Never raises out.

---

## 11. Complete numeric reference

```
# Universe / ranker
MIN_VOLUME_24H_USD        = 50_000
TOP_N_CANDIDATES          = 10
RANKER_WEIGHTS            = pop 1.0, oi_vel 0.7, vol 0.5, funding 0.4, wash −0.5
CLASS_MULTIPLIER          = t1 1.0, t2 1.1, meme 0.7, equity 1.0, commodity 1.0
cold_start |1h| threshold = 5% (t1/equity) / 10% (others); applies when <24 ret pts

# BOS — 4h frame (structural)
SWING_LOOKBACK_4H_BARS    = 20   (≈3.3d)
SWING_MIN_AGE_4H_BARS     = 1
SWING_MIN_BARS_VALIDATION_4H = 1
RANGE_EXPANSION_MULTIPLIER = 1.5×  (1h baseline = 48 bars)

# BOS — 1h frame (early/cold-start)
BOS_1H_ENABLED            = True
SWING_LOOKBACK_1H_BOS_BARS= 24   (1d)
SWING_MIN_AGE_1H_BOS_BARS = 1
SWING_MIN_BARS_VALIDATION_1H = 1
RANGE_EXPANSION_MULTIPLIER_1H_ENTRY = 1.5×

# BOS — 15m frame (latency shrinker)
RANGE_EXPANSION_MULTIPLIER_15M = 2.5×
SWING_LOOKBACK_15M_BARS   = 96   (24h)
BOS_15M_HISTORY_BARS      = 200  (~50h)

# BOS — legacy 1h confirmation + history floor
SWING_LOOKBACK_HOURS      = 48
SWING_MIN_AGE_HOURS       = 4
BOS_BAR_HISTORY_HOURS     = 240  (10d; minimum for 4h frame)
REQUIRE_VOLUME_CONFIRMATION   = False   (disabled — volume lags)
REQUIRE_HTF_TREND_ALIGNMENT   = False   (disabled — counter-trend allowed)

# Suppression
DEDUP_HOURS               = 4
ALPHA_Z_MIN               = 2.0
R_ALPHA_MIN_PCT           = 3.0
IMPULSE_BYPASS_MULTIPLIER = 2.5×   (bypasses BTC-beta gate)
DAILY_ALERT_BUDGET        = 30
WATCHLIST_SCORE_THRESHOLD = 60
WATCHLIST_TTL_HOURS       = 72
BTC_BETA_LOOKBACK_DAYS    = 30

# Trade plan
STOP_BUFFER_PCT           = 0.2%
TP1_R_MULTIPLE            = 1.5
TP2_FALLBACK_R_MULTIPLE   = 3.0
MIN_RISK_PCT_OF_ENTRY     = 0.05%
TP1_FRACTION / TP2_FRACTION = 0.33 / 0.33  (runner ≈ 0.34)
BREAKEVEN_TRIGGER_R       = 1.0
ATR_PERIOD_HOURS          = 14
TRAIL_ATR_MULT            = 1.5

# Direction adjudicator
DIR_CONVICTION_STRONG/OK/TENTATIVE = 0.75 / 0.50 / 0.30
DIR_ALLOW_NO_TRADE / DIR_ALLOW_FLIP = True / True

# Cadence
FAST_CADENCE_SEC          = 60
TRIGGER_POLL_INTERVAL_SEC = 60
HOURLY_REPORT_INTERVAL_SEC= 3600  (offset 300s)
```

---

## 12. Design philosophy baked into the logic (carry forward or reject deliberately)

1. **Structure is the only suppressor.** Every "smart" layer (LLM, predictor,
   order book, volume profile, BTC-beta even) is advisory. The cost of a
   false-positive alert ≪ the cost of a missed real signal. This was a hard-won
   reversal after enrichment layers were silently eating real breaks.
2. **Trigger ≠ direction.** The break fires the event; a separate authority
   picks the side. A clean structural break can still be a sweep. Keep these
   concerns separate.
3. **Vol-normalize everything.** Raw % moves are meaningless across a universe
   spanning BTC and a meme perp. Score in units of each asset's own sigma.
4. **Multi-timeframe in parallel, not serial.** 4h for structure quality, 1h for
   cold-start coverage, 15m for latency. Either-or gating, not all-must-agree.
5. **Synthesize your own bars from a price-only feed**, or the range gate never
   opens and you DROP everything silently.
6. **Fail open, never crash the loop.** Every external call is wrapped; missing
   data passes gates rather than blocking; the alert always sends.
7. **Fallback ladders over strict logic.** Strict swing detection returns None
   exactly when a breakout happens (trending market). Always have a Donchian-
   style fallback.
8. **The engine never trades.** All levels are advisory. *This is the boundary
   the next engine is meant to cross — so the trade plan, conviction tiering, and
   no_trade semantics are the pieces most directly reusable for automated
   execution.*
```

This file is the reference; key things to decide for an automated successor:
position sizing (absent here — `risk_per_unit` is per-unit only), execution
venue & slippage, how `no_trade`/conviction tier maps to size, and whether to
keep the no-suppression invariant when real capital (not just attention) is at
stake.
