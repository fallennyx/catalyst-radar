# Catalyst Radar — Alert Backtest: Quant Research Findings

**Analyst stance:** skeptical quant, institutional capital. Every claim below is
tied to a measured N, win rate (WR), lift vs the 40.7% +4h baseline, and a
significance read. Findings are split into **Evidence** (N ≥ 20, supported) and
**Hypothesis** (N < 20 or speculative mechanism). Reproduce all numbers with
`python scripts/ultra_research.py` (full dump in `data/ultra_research_output.txt`).

> **Dataset:** 481 usable EMIT alerts (1 dropped — all forward returns NaN).
> Replay over historical 1h bars; direction recomputed from BOS logic, so
> direction-vs-LLM disagreement is **untestable**. Entry = alert-hour bar close
> (known positive bias). No intrabar high/low, no news, no 15m bars. All edges
> below are close-to-close and survive only as *directional/conditional* effects,
> never as live PnL estimates.

---

## Headline conclusions (read first)

1. **The engine has no edge at baseline.** +4h WR 40.7%, avg −0.02%, median
   −0.39%. The median alert *loses* at every horizon ≥ +2h. The only horizon
   that is a coin flip is +1h (49.5% WR, +0.23% avg). **The engine's signal lives
   in the first hour and decays after.** This is the single most important fact.

2. **One robust positive filter survives every stress test: `|alpha_z| ≥ 3 AND
   score_pctile ≥ 75`.** N=92, WR 56.5% (lift 1.39), Mann-Whitney p=0.044,
   holds in both date halves (54.3% / 58.7%), and the **win-rate** survives
   removing the top-3 outlier tickers (FF/MYX/TON → still 54.2%). The *mean*
   return of this stack is FF-driven and is NOT robust; the **win rate is.**

3. **Adding `cluster_size == 1` to that stack (the brief's "triple stack") makes
   it WORSE, not better.** Triple-stack mean (+1.12%) collapses to −0.32% once FF
   is removed, and first-half WR is only 38.9%. **Reject the triple stack.** The
   cluster=1 condition is noise here.

4. **The strongest, most robust signals are NEGATIVE (what to suppress / fast-exit),
   not positive.** `vol_ratio > 15×` (N=95, 33.7% WR), `|alpha_z| ∈ [2,3)`
   (N=90, 30.0% WR), `crypto_meme` (N=45, 31.1% WR) are all large-N, both-half-stable
   drag. Stacking them (e.g. `vol>15× & |az|<3`, N=26, 15.4% WR) gets you to
   coin-flip-inverted territory.

5. **Many "bad at +4h" signals are GOOD at +1h** — they pop then fade
   (cluster≥5: 68% WR@1h → 18% WR@4h; btc_4h>2%: 64%→20%). These are not
   suppression candidates; they are **exit-at-+1h** candidates. The dataset is
   screaming for a time-based exit rule far more than for more entry filters.

---

## Section 1 — Strongest Positive Predictors

Ranked by robustness (survives halving + outlier removal + N≥30), then lift.

| # | Filter | N | WR@4h | lift | avg@4h | med@4h | WR@1h | Confidence |
|---|---|---|---|---|---|---|---|---|
| 1 | **`\|alpha_z\| ≥ 3 AND score_pctile ≥ 75`** | 92 | **56.5%** | 1.39 | +1.41%* | +0.27% | 54.3% | **Evidence** — MW p=0.044, both halves hold, WR robust to FF removal |
| 2 | `\|alpha_z\| ≥ 3 AND score_pctile ≥ 75 AND asset=crypto_t2` | 74 | 58.1% | 1.43 | +1.83%* | +0.55% | — | Evidence — but t2 overlaps #1 heavily |
| 3 | `score_pctile ≥ 75` (alone) | 101 | 52.5% | 1.29 | +1.11%* | +0.18% | — | Evidence — both halves (50.0% / 55.1%) |
| 4 | `\|alpha_z\| ≥ 3 AND score_pctile ≥ 75 AND btc_range_expansion=0` | 64 | 56.2% | 1.38 | +2.22%* | +0.47% | — | Evidence — best median; avoid BTC-impulse hours |
| 5 | `score ≥ 10` (raw) | 45 | 55.6% | 1.37 | +0.88%* | +0.09% | — | Evidence — raw score works too, ~equiv to pctile |
| 6 | Day-of-week = Thursday | 63 | 54.0% | 1.33 | +0.73% | +0.28% | — | Hypothesis — no mechanism, likely calendar luck |

\* **All means in this table are inflated by fat right tails (FF +53.8%, MYX +40.8%).
Trust the WR and median columns; treat the mean as an upper bound.** For #1, mean
drops from +1.41% → +0.28% when FF/MYX/TON are removed, but WR only drops 56.5% →
54.2%. The *directional* edge is real; the *magnitude* edge is three lucky trades.

**Bootstrap (2000×) 95% CI on WR for filter #1: [46.7%, 66.3%].** Lower bound
(46.7%) sits above the 40.7% baseline — the WR edge clears zero, narrowly.

### Mechanics worth noting
- **`alpha_z` is non-monotone.** Buckets: `<2` → 38.7% WR, `[2,3)` → **30.0%**,
  `[3,5)` → 43.6%, `>5` → 46.3%. There is a **dead zone at |az| 2–3** (the
  marginal-gate / impulse-bypass band) that underperforms even the `<2` bucket.
  The edge requires *strong* decoupling (≥3), not *any* decoupling.
- **`score_pctile` is also non-monotone and only pays in the top quartile.**
  0–25 → 42.0%, 25–50 → 35.0%, 50–75 → 36.3%, **75–100 → 51.6%.** The middle
  is the worst place to be. Conviction only helps when it's extreme.
- **`score` predicts *magnitude*, not *direction*.** Spearman(score, signed_ret)
  ≈ −0.03 (none), but Spearman(score, |signed_ret|) ≈ +0.18. High score → bigger
  moves either way. Useful for position sizing, not for direction.

---

## Section 2 — Strongest Negative Predictors

Ranked by drag. These are the highest-value findings for a suppression chain.

| # | Filter | N | WR@4h | lift | avg@4h | med@4h | WR@1h | Confidence |
|---|---|---|---|---|---|---|---|---|
| 1 | **`vol_ratio > 15×`** | 95 | **33.7%** | 0.83 | −1.16% | −1.06% | 43.2% | **Evidence** — large N, monotone tail |
| 2 | `\|alpha_z\| ∈ [2,3)` (dead zone) | 90 | 30.0% | 0.74 | −0.77% | −0.85% | 58.9% | **Evidence** — large N, both halves |
| 3 | `crypto_meme` | 45 | 31.1% | 0.76 | −1.30% | −1.69% | 48.9% | **Evidence** — both halves (33.3% / 28.6%) |
| 4 | `crypto_t1 AND cluster_size ≥ 3` | 36 | 22.2% | 0.55 | −0.44% | −0.43% | 61.1% | **Evidence** — sharp |
| 5 | `vol_ratio > 15× AND \|alpha_z\| < 3` | 26 | 15.4% | 0.38 | −1.65% | −1.65% | 46.2% | **Evidence** — stacked drag |
| 6 | `\|alpha_z\| ∈ [2,3) AND score_pctile ≥ 50` | 24 | 16.7% | 0.41 | −1.60% | −1.61% | 50.0% | **Evidence** — "false conviction" |
| 7 | `cluster_size ≥ 5` | 22 | 18.2% | 0.45 | −0.53% | −0.49% | **68.2%** | **Evidence** — but see exit note |
| 8 | `btc_ret_4h > +2%` (overheated BTC) | 39 | 20.5% | 0.50 | −0.30% | −0.39% | **64.1%** | **Evidence** — fades hard after +1h |
| 9 | `vol_ratio > 15× AND crypto_meme` | 10 | 10.0% | 0.25 | −2.62% | −2.94% | 30.0% | Hypothesis — N<20 |

**Confirming/refuting the brief's `cluster_size ≥ 5` claim (18.2% WR):**
Confirmed at +4h (18.2% WR, N=22, holds both halves: 25%/10%). **But it is a
*timing* artifact, not a bad signal:** WR@1h is 68.2%. These fire on a synchronized
macro pump, are correct for ~1h, then mean-revert. The brief framed this as a
false-signal filter; the data says it's a **take-profit-fast** signal. Suppressing
it would forgo a 68% +1h hit rate. **Do not drop cluster≥5 — exit it at +1h.**

**The `vol_ratio` finding is the cleanest large-N negative.** Spearman(vol_ratio,
signed_ret_4h) = −0.122 (weak but consistently negative); Spearman(vol_ratio,
|ret|) = +0.173. Extreme volume = bigger, *more often adverse* moves. The 5–15×
band is actually the sweet spot (44.4% WR); both <2× and >15× underperform.

---

## Section 3 — Feature Interaction Matrix

Incremental lift per added variable (start from baseline 40.7% WR).

**Positive stacking (the only path that compounds):**

| Stack | N | WR@4h | lift | Δ vs prior |
|---|---|---|---|---|
| `score_pctile ≥ 75` | 101 | 52.5% | 1.29 | +11.8pp vs baseline |
| `+ \|alpha_z\| ≥ 3` | 92 | 56.5% | 1.39 | +4.0pp |
| `+ asset=crypto_t2` | 74 | 58.1% | 1.43 | +1.6pp (but shrinks N) |
| `+ btc_range_expansion=0` | 64 | 56.2% | 1.38 | −1.9pp (no help) |
| `+ cluster_size=1` (triple) | 42 | 50.0% | 1.23 | **−6.5pp (HURTS)** |
| `+ direction=long` | 75 | 56.0% | 1.38 | flat |

**Verdict:** the productive stack is **2 variables** (`pctile≥75 & |az|≥3`).
Variable #3 either adds nothing (`bre=0`, `long`, `t2` ~flat on WR) or actively
hurts (`cluster=1`). **Stop at two.** More conditions = smaller N = overfit.

**Negative stacking (compounds cleanly — drag is additive):**

| Stack | N | WR@4h |
|---|---|---|
| `vol > 15×` | 95 | 33.7% |
| `+ \|alpha_z\| < 3` | 26 | 15.4% |
| `\|alpha_z\| ∈ [2,3)` | 90 | 30.0% |
| `+ score_pctile ≥ 50` | 24 | 16.7% |

The "**mid-decoupling + high conviction**" cell (`|az| 2–3 & pctile ≥ 50`, 16.7%
WR) is the single most counter-intuitive Evidence finding: the engine is *most
confident* (high score) about its *weakest* structural signals (marginal alpha_z),
and those are the worst trades. This is a calibration failure worth fixing.

---

## Section 4 — Exit Intelligence

**The dataset's biggest finding is an exit finding.** Aggregate signed-return sum
across the whole book by exit horizon:

| Exit horizon | Σ signed ret | avg | WR |
|---|---|---|---|
| **+1h** | **+112.2** | **+0.23%** | **49.5%** |
| +2h | +12.7 | +0.03% | 42.4% |
| +4h | −9.3 | −0.02% | 40.5% |
| +8h | −41.4 | −0.09% | 41.4% |

**A flat "exit everything at +1h" rule turns the engine from net-negative to
net-positive.** This dominates every entry filter studied.

**Caveat for the top quartile (winners, +4h > 2%, N=79):** for the *winners*,
holding to +4h is better (Σ +501 vs +209 at +1h) — 42 of 79 peaked at +4h, only 4
peaked at +1h. So the optimal policy is **asymmetric**:
- **Default: exit at +1h** (kills the fat left tail of faders).
- **Exception: if +1h is already strongly positive (signal is "working"), hold to
  +4h** to capture the builders.

**Trajectory of top-20 winners:** 19 of 20 *built* from +1h → +4h (kept climbing);
only **FF** (the +94.7%@1h → +53.8%@4h textbook case) faded. So the "spike-and-fade"
that justifies a blanket +1h exit is concentrated in the *losers and the single
mega-outlier*, while normal winners ramp. This reinforces the asymmetric rule:
fade-risk is in flat/negative +1h trades; let positive +1h trades run.

**Profit-path frequencies (close-to-close proxy):**

| Path | n | % | avg@4h |
|---|---|---|---|
| Consistent loss (1h<0, 4h<0) | 175 | 36.4% | −2.45% |
| Early win held (1h>0, 4h>0) | 136 | 28.3% | +3.45% |
| Early win reversed (1h>0, 4h<0) | 100 | 20.8% | −1.71% |
| Early loss recovered (1h<0, 4h>0) | 57 | 11.9% | +2.10% |

**20.8% of alerts are "early win reversed"** — the painful case. No feature
cleanly predicts it (all Mann-Whitney p > 0.09 vs "early win held"); the weak
tendency is higher `vol_ratio` (15.6 vs 9.1) and higher `|alpha_z|`. This is
*more* evidence that high vol_ratio = fade risk, and that the +1h exit is the only
reliable defense against reversal.

---

## Section 5 — Regime Analysis

| Regime | Split | Result |
|---|---|---|
| **BTC impulse** (`bre=1`, N=159) vs non (N=320) | WR 39.6% vs 41.2%, MW p=0.24 | **No regime difference.** BTC breaking out neither helps nor hurts individual BOS calls. Reject the "swamped by macro" hypothesis at the aggregate level. |
| **Cluster** (≥3, N=102) vs isolated (=1, N=256) | WR@4h 36.3% vs 42.6%; WR@1h 58.8% vs 45.0% | Real divergence, but **inverted by horizon.** Clusters win early, lose late. Time-based, not a quality difference. |
| **Asset class** | t2 44.2% / t1 32.0% / meme 31.1% | **Different regimes confirmed.** crypto_t1 has *zero* fat tail (P(>5%)=0%, P(<−5%)=0%, std 1.16 — these barely move; structurally low-vol, low-edge). meme has fat tails both ways (P>5%=4.4%, P<−5%=8.9%) and negative median. **crypto_t2 is the only class carrying the book.** |
| **Calendar** | feb-mar (N=12) / apr-may windows | No window beats baseline meaningfully. Week 20 (N=13, 76.9% WR) is the "best week" but N<20 → luck. Week 18 (N=74, 32.4%) is the worst and large-N — a genuinely bad stretch, no identifiable feature explains it (just lower WR across the board). |

**crypto_t1 deserves a callout:** 32% WR, std 1.16, no tails. These alerts are
*structurally inert* — the BOS fires but the instrument doesn't move enough to be
worth a trade either direction. Strong candidate for de-prioritization.

---

## Section 6 — Robustness Assessment

| Finding | Halved? | Outlier-trimmed? | N≥30? | Single-ticker driven? | Verdict |
|---|---|---|---|---|---|
| `\|az\|≥3 & pctile≥75` (WR) | ✅ 54.3/58.7 | ✅ WR 54.2% w/o top-3 | ✅ N=92 | WR no; **mean yes (FF)** | **ROBUST (as a WR/direction filter)** |
| `\|az\|≥3 & pctile≥75` (mean +1.41%) | — | ❌ → +0.28% w/o FF/MYX/TON | — | **YES — FF/MYX/TON** | **FRAGILE — do not quote the mean** |
| Triple stack (+cluster=1) | ❌ h1 38.9% / h2 58.3% | ❌ → −0.32% w/o FF | ✅ N=42 | **YES — FF (+59.7 of total)** | **REJECT** |
| `vol_ratio > 15×` (negative) | ✅ stable | ✅ WR-based, holds | ✅ N=95 | No | **ROBUST** |
| `\|az\| 2–3` dead zone | ✅ | ✅ | ✅ N=90 | No | **ROBUST** |
| `crypto_meme` drag | ✅ 33.3/28.6 | ✅ | ✅ N=45 | No | **ROBUST** |
| `cluster≥5` @4h | ✅ 25/10 | ✅ | ✅ N=22 | No | ROBUST but is an exit, not a drop |
| `+1h exit` portfolio edge | ✅ (49.5% WR is the whole book) | ✅ median-stable | ✅ N=481 | No | **ROBUST — strongest finding** |

**Standard-error check on the flagship stack:** `|az|≥3 & pctile≥75`, mean +1.41%,
SE = std/√N = 8.94/√92 = 0.93%, **t = 1.51** → the *mean* is NOT significant at
95% (need t>1.98). But the **win rate** (56.5% vs 40.7%, two-proportion z ≈ 3.0,
and MW p=0.044) **is.** This is the crux: **the edge is in direction (win rate),
not in average magnitude.** An implementation must monetize the WR via fixed
risk:reward, not by assuming a +1.4% expected move.

---

## Section 7 — Candidate Trading Rules (for the suppression chain)

Expressed as concrete, implementable thresholds. Per the v3 no-suppression
invariant (BOS is the only hard suppressor; everything else is advisory), these
are framed as **score-boost / fast-exit / advisory-tag** rules rather than DROPs,
except where the drag is severe enough to justify a config-gated suppressor.

### Rule 1 — Conviction boost (Evidence) ⭐ highest confidence
**`IF |alpha_z| ≥ 3 AND score_pctile ≥ 75 → tag "HIGH_CONVICTION", prioritize.`**
- Effect: WR 40.7% → 56.5% (+15.8pp, lift 1.39). N=92 (19% of alerts).
- Robust to halving and outlier removal (WR). MW p=0.044.
- Implementation: add `metadata["conviction_tier"]="high"`; surface in Telegram.
- **Do NOT add cluster_size or bre conditions — they shrink N and hurt.**

### Rule 2 — Time-based exit (Evidence) ⭐ highest expected-value impact
**`Default trade-plan exit at +1h; extend to +4h ONLY if signed_ret_+1h > 0.`**
- Effect: flat +1h exit flips book from −9.3 to +112.2 cumulative signed return.
  Asymmetric version captures the +501 winner pool on the builders.
- This is a `trade_plan.py` change (tighten TP1 timing / add time-stop), not a
  suppression change. Highest-EV finding in the entire study.

### Rule 3 — Volume-blowoff fast-exit / advisory (Evidence)
**`IF vol_ratio > 15× → tag "BLOWOFF_RISK", force +1h exit (do not hold).`**
- Effect: this bucket is 33.7% WR @4h (lift 0.83, N=95) but 43.2% @1h — the
  damage is all in the hold. Don't suppress (you'd miss the +1h pop); cap the hold.

### Rule 4 — Mid-decoupling de-prioritization (Evidence)
**`IF |alpha_z| ∈ [2, 3) → tag "MARGINAL_DECOUPLING", de-prioritize / size down.`**
- Effect: 30.0% WR (lift 0.74, N=90). Worse than the `|az|<2` bucket. Worst when
  combined with high score (`|az|∈[2,3) & pctile≥50` → 16.7% WR, N=24).
- This is the engine's calibration failure: marginal structure + high score.
  At minimum, suppress the *score boost* for these so they don't rank top-quartile.

### Rule 5 — Asset-class weighting (Evidence)
**`crypto_meme: tag "FAT_TAIL_RISK" + force +1h exit. crypto_t1: de-prioritize (inert).`**
- meme: 31.1% WR, −1.30% avg, fat two-sided tails — only worth it with a fast exit.
- t1: 32.0% WR, std 1.16, *no* tails — structurally not worth trading either way.
- crypto_t2 carries the book (44.2% WR, +0.24%); concentrate attention there.

**Combined estimated impact** (Rules 1+4+5 as ranking, Rules 2+3 as exits):
keeping all alerts but re-ranking by conviction tier and applying the +1h-default
exit would move blended WR toward ~50–56% on the prioritized subset while leaving
the no-suppression invariant intact. **No rule above is a hard DROP** — consistent
with the v3 invariant.

---

## Section 8 — Missing Variable Assessment

Ranked by estimated impact on predictive power.

1. **Intrabar high/low (MFE / MAE within each forward bar).** *Highest impact.*
   The entire Section-4 exit finding is built on a 4-point close-to-close proxy.
   With true intrabar extremes we could (a) measure real max-favorable-excursion to
   set TP, (b) measure real drawdown to set SL, (c) test whether the +1h "win"
   actually held or just closed green after a deep wick. **Every exit rule above is
   a hypothesis until validated on intrabar data.** This is the #1 gap.

2. **News / catalyst_type (the variable the user suspected).** Absent (0 news rows).
   **Indirect evidence it matters:** `|alpha_z| ≥ 3` (strong idiosyncratic move
   independent of BTC) is the single best positive filter, and `cluster_size`
   (many tickers moving together = macro, no idiosyncratic catalyst) is the
   clearest "fades after +1h" signal. **alpha_z is a proxy for "is there a
   ticker-specific catalyst."** High alpha_z + isolated = likely real news;
   high cluster = macro beta, no news. So the data *does* indirectly support the
   news hypothesis: the features that proxy "idiosyncratic catalyst present"
   (high alpha_z, low cluster) are exactly the ones with edge. A real
   `catalyst_type` field would likely subsume and sharpen both.

3. **LLM `direction` / `confidence` (recomputed-away in replay).** Direction here
   is structurally tied to BOS, so we literally cannot test the engine's core v3
   design question: *does the LLM disagreeing with BOS predict failure?* The
   `direction_conflict` pass-through is the heart of the suppression chain and is
   **completely untested by this dataset.** Re-run replays with the classifier on
   (accepting Haiku cost) to get this. Second-highest *design* priority after
   intrabar data.

**Honorable mention:** funding rate / open-interest at alert time — would
disambiguate "real breakout" from "leverage-driven squeeze that mean-reverts,"
which is likely what the `vol_ratio > 15×` failures are.

---

## Appendix — what the brief got wrong (corrections)

- **Triple stack (`az≥3 & pctile≥75 & cluster=1`, claimed n=34, 51.5%, +1.80%):**
  reproduced at N=42, WR 50.0%, +1.12% — but **non-robust** (FF drives the mean;
  h1 WR 38.9%). The cluster=1 condition *removes* the edge present in the 2-var
  stack. **Drop it.**
- **The brief's emphasis on mean +4h returns (+1.72% etc.) is misleading.** Means
  are FF/MYX/TON-dominated and fail the t-test (t=1.51). The real, defensible edge
  is **win rate**, which passes. Reframe all targets around WR + fixed R:R.
- **`cluster_size ≥ 5` is not a false-signal filter** (18.2% WR@4h) — it's a
  fast-exit signal (68.2% WR@1h). Suppressing it loses a good +1h trade.
- **The brief under-weights the exit finding.** The largest, most robust lever in
  the entire dataset is "exit at +1h," not any entry filter.
