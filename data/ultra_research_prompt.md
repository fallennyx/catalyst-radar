# Quant Research Brief: Catalyst Radar Alert Backtest

You are acting as a quantitative researcher, not a trader, analyst, storyteller, or strategist.

Your objective is to discover statistically supported relationships in the data, not to produce explanations that sound plausible. Treat every hypothesis as false until supported by evidence.

---

## Dataset Description

You have `alert_backtest.csv` — 482 live EMIT alerts from the Catalyst Radar engine across two calendar windows: Feb 25–Mar 23 2026 and Apr 5–May 11 2026. Each row is one fired alert. The engine detects structural breakouts (BOS = Break of Structure) on crypto perps and commodities, fires a long or short signal, and you are measuring how often the direction was correct.

**Critical data limitations to embed in every analysis:**

- `direction`, `confidence`, `summary`, `evidence`, `catalyst_type` were **never stored** in these replays (engine ran without the LLM classifier). All direction values in the CSV are **recomputed** from re-running the BOS detection function on the same 1h bars the engine saw. This means direction is structurally guaranteed to match the BOS logic — you cannot test whether the LLM direction disagreed with BOS (that field is absent).
- `entry` = **close of the alert-hour bar**, not live mark price at fire time. Actual fire price could be anywhere within that hourly candle. This is a known positive bias (entry is approximated).
- No `news` data exists in any replay DB (0 rows). The variable the user believes is most important (catalyst type, news quality) is completely absent.
- No intrabar high/low for the forward windows (`px_+1h`, `px_+4h` are bar closes, not intrabar extremes). Maximum favorable excursion and true drawdown are not computable — only close-to-close returns.
- `bars_15m` were not stored in replay DBs. The 15m parallel gate that sometimes fires the engine early is not reconstructable from stored data.
- Sample is **not live production** — it is replay over historical bars. No slippage, no latency, no partial fills.

**Column schema:**

| Column | Description |
|---|---|
| `db` | Time window label (feb25-mar23 / apr05-may06 / may07-may09 / may09-may11) |
| `ticker` | Perp symbol (e.g. BTC, FF, USELESS) |
| `asset_class` | crypto_t1 / crypto_t2 / crypto_meme / commodity |
| `score` | Engine composite vol-normalized score at alert time |
| `score_pctile` | Score percentile rank among all same-UTC-day EMITs (0=lowest, 100=highest) |
| `alpha_z` | BTC-residual z-score: how much the ticker moved independent of BTC (standardized). |alpha_z| ≥ 2 is the engine's beta gate. |
| `r_alpha_pct` | Residual alpha in % terms (raw independent return from BTC move) |
| `alert_utc` | Alert fire time (UTC) |
| `alert_hour` | UTC hour 0–23 |
| `alert_dow` | Day of week (Mon–Sun) |
| `direction` | long / short (recomputed from BOS logic) |
| `structure_type` | 4h_and_1h / 4h / 1h — which BOS path(s) fired. 4h_and_1h = highest conviction (both timeframes confirmed), 4h = structural only, 1h = early-detection only |
| `breakout_level` | The 4h swing pivot price that was broken |
| `breakout_dist_pct` | (entry − breakout_level) / breakout_level × 100, signed by direction. How far past the pivot we entered. Low = caught early; high = chasing |
| `reproduced` | Always 1 in this dataset (direction reproduced for all 482) |
| `entry` | Close price of the alert-hour bar (approx entry) |
| `btc_ret_1h_pct` | BTC's 1h return immediately before the alert |
| `btc_ret_4h_pct` | BTC's 4h return before the alert (macro trend state) |
| `btc_range_expansion` | 1 if BTC's alert-bar range > 2× its 48-bar median (BTC itself in impulse) |
| `ticker_ret_4h_pct` | The alerted ticker's own 4h return going into the alert — was it already running? |
| `vol_ratio` | Alert-bar volume ÷ median(prior 48 bars). Median vol_ratio across dataset ≈ 6.7× |
| `cluster_size` | # tickers that EMITted in the same UTC hour. 1 = isolated signal. Max observed = 6. |
| `repeat_fire_8h` | 1 if the same ticker EMITted within the prior 8h |
| `px_+Nh` | Close price N hours after alert |
| `signed_ret_+Nh_pct` | ((px_+Nh − entry) / entry) × 100, negated for shorts. Positive = direction correct. |

**Baseline stats to benchmark every finding against:**

- Overall +4h win rate: **40.7%** (coin-flip = 50%; engine is below baseline)
- Overall +4h avg signed return: **−0.02%**
- +4h stdev: **4.74%** (fat tails — a few big winners dominate averages)
- Longs: 387 (80%) | Shorts: 95 (20%)
- Asset class mix: crypto_t2 68%, crypto_t1 21%, crypto_meme 9%, commodity <1%
- Distribution: fat-tailed. Top 10 winners at +4h range from +12% to +54%. Bulk of alerts cluster around 0%.

---

## Core Mission

Analyze the entire dataset and identify every meaningful relationship associated with future returns, with particular focus on:

- **+1h signed return** (earliest exit, captures the initial impulse)
- **+4h signed return** (primary target horizon — one trading session)
- **+1% return achievement** (compute: what % of alerts hit +1% before +4h close; proxy for binary win)
- **Maximum future return** (proxy: the highest of +1h/+2h/+4h/+8h, since we have no intrabar data)
- **Time to peak** (which horizon produced the best signed return: 1h, 2h, 4h, or 8h?)
- **Reversal** (was +1h positive but +4h negative? How often does early momentum fade?)
- **Momentum persistence** (was +4h positive AND +8h positive?)

The goal is to understand:
1. What conditions consistently precede strong signed gains?
2. What conditions consistently precede negative outcomes (failed breakouts, reversals)?
3. What conditions predict continuation vs. exhaustion?
4. What conditions predict optimal exit timing?

Do not optimize for narrative quality. Optimize for predictive power.

---

## Research Methodology

For every feature and combination:

- Measure correlation with +1h and +4h signed return
- Measure conditional win rate vs. 40.7% baseline
- Measure lift (win rate / 40.7%) — lift > 1.3 is meaningful
- Measure average return, median return, stdev
- Report sample size N and note when N < 20 (treat as suggestive only)
- Use Mann-Whitney U or equivalent nonparametric test for significance (returns are fat-tailed, not normal)

Always report: N, win rate, avg return, median return, lift vs baseline, and confidence assessment.

---

## Anti-Hallucination Rules

Do NOT:
- Invent explanations for why a pattern exists
- Reference news, fundamentals, or macro events not in the data
- Assume causation from correlation
- Call a finding "statistically significant" without actually testing it

Separate all findings into:

### Evidence
Directly supported by data with N ≥ 20.

### Hypotheses
Patterns with N < 20, or where mechanism is speculative.

Never mix these categories. Label every table row with its N.

---

## Dataset-Specific Analyses (run these explicitly)

These are required analyses unique to this dataset structure. Run all of them.

### A. BOS Structure Quality

The engine generates three `structure_type` values that reflect conviction level. Test whether they actually predict return quality:

- `4h_and_1h`: both 4h structural break AND 1h early-detection confirmed (highest conviction — n≈449)
- `4h`: only 4h path fired (n≈29)
- `1h`: only 1h early-detection path (n=1 in this dataset — flag as N too small)

Additionally: does `breakout_dist_pct` predict outcomes? The hypothesis is that entering close to the pivot (<1%) is better than chasing a break already 5%+ extended. Test all buckets: <1%, 1–3%, 3–6%, >6%.

### B. Alpha-Z Decoupling Analysis

`alpha_z` measures how independently a ticker moved from BTC. Engine's gate requires |alpha_z| ≥ 2.0 (but impulse bypass exists). Test:
- |alpha_z| buckets: <2, 2–3, 3–5, >5
- Whether `r_alpha_pct` (raw %) adds information beyond `alpha_z` (z-score)
- Whether high alpha_z on SHORT direction alerts performs differently than on LONG alerts
- The interaction: alpha_z × score_pctile (already found: n=75, WR=55.4%, avg=+1.72% at +4h — quantify more granularly)

### C. Market Regime via BTC

`btc_ret_4h_pct` is available for all alerts. The key question is not just direction but interaction with the alerted ticker's own direction:

- **Aligned regime**: alert is long AND btc_ret_4h > 0 — riding macro tailwind
- **Counter-regime**: alert is long AND btc_ret_4h < 0 — isolated move against macro
- **BTC impulse**: `btc_range_expansion = 1` — was BTC itself in a structural break? If yes, isolated ticker moves may be swamped
- Segment by BTC regime buckets: >+2%, +1%..+2%, 0..+1%, −1%..0, <−1%

Also test: does `btc_ret_1h_pct` (1h) predict +1h return better than `btc_ret_4h_pct` predicts +4h return? (Same-timescale predictor hypothesis.)

### D. Cluster Size as False-Signal Filter

`cluster_size` = # tickers EMITting same UTC hour. The hypothesis: when many tickers fire simultaneously, it reflects a macro pump/dump and individual BOS direction calls are noise.

- Test every cluster_size value 1 through 6 individually
- Compute win rate and avg return at +1h and +4h for each
- Specifically test: does isolated (cluster=1) beat clustered (cluster≥3) consistently across ALL horizons or just +4h?
- Cross-cluster_size with asset_class: do crypto_meme clusters behave differently from crypto_t2 clusters?

### E. Score Percentile vs. Raw Score

`score_pctile` ranks the alert within its day. `score` is the raw composite. Test both independently and together:
- Do alerts in the top-25% by score_pctile outperform? (Already found: 52.5% WR, +1.11% at +4h — verify and segment further)
- Is raw `score` threshold (e.g. score > 10) a better filter than score_pctile?
- Does score predict magnitude of return, not just direction? (Correlation between score and |signed_ret_+4h_pct|)

### F. "Already Running" vs. Fresh Breakout

`ticker_ret_4h_pct` = how much the ticker already moved in the 4h before the alert. Two competing hypotheses:
- **Momentum hypothesis**: tickers already up 3–8% have more momentum → continue
- **Exhaustion hypothesis**: tickers already up >8% are extended → mean revert

Test all buckets: negative (counter-trend), 0–3%, 3–8%, >8%.
Cross with direction: does a ticker already up 8% firing a SHORT signal have higher win rate? (Counter-trend after exhaustion.)

### G. Time-of-Day and Day-of-Week

Crypto trades 24/7. Test UTC hour buckets with N ≥ 10:
- Asia session: 00:00–08:00 UTC
- Europe/London: 07:00–14:00 UTC
- US open: 13:00–17:00 UTC
- US afternoon: 17:00–21:00 UTC
- Overnight: 21:00–00:00 UTC

Also test day-of-week. Note: weekends may have lower liquidity → different vol_ratio interpretation.

### H. Repeat-Fire Analysis

`repeat_fire_8h = 1` for 48 alerts (10%). Two hypotheses:
- **Continuation**: second break in 8h = stronger trend, higher WR
- **Chasing**: same ticker firing again = late entry, worse outcomes

At what firing-count does win rate change? (First fire, second fire within 8h.)
Cross with `breakout_dist_pct`: repeat fires likely have higher dist (further from pivot) → is that the confounder?

### I. Vol Ratio Analysis

Median vol_ratio ≈ 6.7 (alert bars have 6.7× normal volume by default due to BOS range expansion requirement). Test:
- Vol ratio buckets: 1–2×, 2–5×, 5–15×, >15×
- Is vol_ratio linearly correlated with return magnitude?
- Vol ratio + alpha_z interaction: high vol + high alpha_z vs. high vol + low alpha_z

### J. Asset Class Failure Modes

`crypto_meme` has 31% WR at +4h, −1.30% avg. `crypto_t1` is 32%, −0.31%. But meme coins have fat upside tails.
- Compute separately: median return (not mean), to strip outliers
- Compute: P(return > +5%) for each class (moonshot probability)
- Compute: P(return < −5%) for each class (blowup probability)
- Is the crypto_t2 positive edge (+0.24%) real or driven by a few outliers?

### K. Profit Path Analysis (Multi-Horizon Trajectory)

For each alert compute: did the trade improve or worsen over time?
- **Early win, held**: signed_ret_+1h > 0 AND signed_ret_+4h > 0
- **Early loss, recovered**: signed_ret_+1h < 0 AND signed_ret_+4h > 0
- **Early win, reversed**: signed_ret_+1h > 0 AND signed_ret_+4h < 0
- **Consistent loss**: signed_ret_+1h < 0 AND signed_ret_+4h < 0

Measure the frequency of each path. Then measure what features predict each path type.
Specifically: what predicts "early win, reversed" (the most painful outcome — took a winner and it faded)?
The dataset has proxy data only (bar closes, no intrabar), so the +1h→+4h path is the best available approximation.

### L. Outlier Anatomy (Top Movers)

The top 10 winners at +4h range from +12% to +54% (FF Apr 10 = +53.8%). These outliers dominate the average. Analyze them explicitly:
- What features do the top 10 winners share?
- What features do the top 10 losers share?
- Is there a consistent fingerprint for the extreme outliers (positive and negative)?
- Remove the top and bottom 5% of returns and recompute all key metrics on the trimmed dataset — does the edge survive? (Robustness check: is the "edge" just 5 lucky extreme outcomes?)

FF Apr 10 specifics (known textbook case): score=13.58 (95th pctile), alpha_z=8.25, cluster_size=1, breakout_dist_pct=0.88%, vol_ratio=11×, structure_type=4h_and_1h, btc_ret_4h=+1.1% (slightly up), ticker_ret_4h=+3.9% (already moving). +1h=+94.7%, +4h=+53.8%, +8h=+33.1% (peak was at +1h, then decayed — exit analysis needed).

### M. Triple-Stack Interaction

The following three-way condition is the highest-prior candidate for a real filter:
- `alpha_z >= 3` (strong BTC decoupling) AND
- `score_pctile >= 75` (top-quartile conviction) AND
- `cluster_size == 1` (isolated signal, not a macro pump)

Preliminary: n=34, WR=51.5%, avg=+1.80% at +4h. Examine this fully:
- Break down by horizon (+1h through +8h)
- Cross with asset_class
- Cross with `btc_range_expansion`
- Find the 4th variable that would further improve this stack (test: breakout_dist_pct < 2%, ticker_ret_4h_pct 3–8%, alert_hour in good UTC windows, direction=long)

---

## Positive Outcome Analysis

Find every pattern associated with:
- Highest +1h signed returns (captures the initial impulse the engine was designed for)
- Highest max return (proxy: max of the four horizon returns)
- Fastest upward moves (signed_ret_+1h > signed_ret_+4h — front-loaded)
- Highest momentum continuation (signed_ret_+4h > 0 AND signed_ret_+8h > 0)

Rank findings from strongest to weakest. For each finding: effect size, win rate, avg return, N, confidence.

Identify combinations that outperform individual features. The dataset already shows:
- alpha_z ≥ 3 alone: 45.2% WR, +0.25% at +4h
- score_pctile ≥ 75 alone: 52.5% WR, +1.11%
- Both together (n=75): 55.4% WR, +1.72%
- Both + cluster=1 (n=34): 51.5% WR, +1.80%

Continue this interaction search aggressively.

---

## Negative Outcome Analysis

Find every pattern associated with:
- Worst +1h returns
- Failed breakouts (signed_ret_+1h > 0, signed_ret_+4h < −1% — looked good then failed)
- Mean reversion (how common is the "false breakout" pattern overall?)
- Large drawdowns (proxy: most negative signed_ret value across any horizon)

Already known worst predictors:
- `cluster_size ≥ 5`: 18.2% WR, −0.53% (n=22)
- `crypto_meme`: 31.1% WR, −1.30% (n=45)
- `06:00 UTC`: 30.8% WR, −2.65% (n=13)
- `alpha_z < 3`: 34.9% WR, −0.37% (n=209)

Find what stacks these negative signals.

---

## Exit Intelligence Analysis

Since intrabar data is unavailable, use the four horizon closes as a proxy trajectory. For each major winner (top 20 by +4h return):
- Was +1h already positive? (early signal that it's working)
- Did return increase from +1h → +4h or decrease? (front-loaded vs. persistent)
- Did +8h exceed +4h? (further continuation) or decay?
- Trajectory types: linear ramp / spike-and-fade / delayed breakout / flat-then-explode

Simulate time-based exits:
- **Exit A**: take profit at +1h (captures impulse, exits early)
- **Exit B**: take profit at +4h (holds through session)
- **Exit C**: take profit at +8h (holds overnight or through full day)

For the top quartile only (+4h > 2%), compute what each exit rule would have retained.
Note: FF Apr 10 had +94.7% at +1h, +53.8% at +4h — a +1h exit strategy would have been superior for outliers.

---

## Regime Discovery

Segment the dataset into candidate regimes and test whether different rules apply:

**Regime 1: BTC impulse day** (btc_range_expansion = 1, n=159)
vs. **Non-impulse** (n=323). Do BOS alerts mean more or less when BTC itself is breaking out?

**Regime 2: High cluster** (cluster_size ≥ 3) vs. **isolated** (cluster_size = 1).
Already shows dramatically different outcomes. Quantify fully.

**Regime 3: Meme vs. structural alpha**
crypto_meme and crypto_t1 appear to be different regimes (lower WR, fatter negative tail).
Test whether entirely different feature weights apply to each.

**Regime 4: Calendar window** (feb-mar vs. apr-may)
Feb-Mar has only 12 alerts — too small. But test whether the W15/W16 (big-n weeks) results differ from W18 (worst week) vs. W20 (best week). Is week-to-week variance explainable by identifiable features?

---

## Overfitting Defense

For every major finding:
- Halve the dataset (first half vs. second half by date) and check if finding holds in both
- Remove the top and bottom 5% of return outliers and recheck
- Test: does the finding survive if you require N ≥ 30?
- Flag any finding where a single ticker (e.g. FF, USELESS, MYX) drives the result

The +4h mean return improvement from stacking conditions is +1.72% over baseline. With N=75 and stdev ~4.7%, this is borderline significant. Test it: compute standard error = 4.7 / sqrt(75) ≈ 0.54%. The edge (+1.72%) is ~3× the standard error. This is meaningful but not conclusive. Flag explicitly.

---

## Deliverables

### Section 1: Strongest Positive Predictors
Ranked table. Include: feature, threshold, N, WR, avg return, lift vs 40.7% baseline, confidence.

### Section 2: Strongest Negative Predictors
Same format. Flag the cluster_size ≥ 5 finding as the strongest (18.2% WR, lift 0.45) and confirm or refute with more granular analysis.

### Section 3: Feature Interaction Matrix
Combinations only. Show the best N-variable stacks (2-variable and 3-variable). Quantify incremental lift per added variable.

### Section 4: Exit Intelligence
Trajectory analysis for top winners. Optimal exit horizon by signal strength.

### Section 5: Regime Analysis
Test each regime above. Identify which features predict well in each regime and which fail.

### Section 6: Robustness Assessment
For each major finding: does it survive halved data? Outlier removal? Minimum N=30? Flag fragile findings explicitly.

### Section 7: Candidate Trading Rules
Only after all analysis. Propose 3–5 specific filter rules (expressed as additions to a suppression chain) with exact thresholds. For each: estimated WR improvement, estimated alert reduction, and whether it is supported by Evidence or Hypothesis.

### Section 8: Missing Variable Assessment
Based on the analysis, identify the top 3 missing variables that would most improve predictive power. Rank by estimated impact. (Hint: news/catalyst_type is absent and was suspected as the most important variable — does the data give any indirect evidence for this? Can you infer it from alpha_z or cluster_size patterns?)

---

## Final Instruction

Behave like a skeptical quantitative researcher managing institutional capital. Your job is not to find reasons to trade. Your job is to find evidence that survives scrutiny. Every conclusion must earn the right to exist through data.

When N is small, say so. When confidence is low, say so. When an apparent edge is driven by 2 outlier trades, say so. Prefer weaker but robust findings over stronger but fragile ones.

The target audience for this analysis is an engineer who will implement the findings as filter rules in production code. Findings should be expressed as specific, implementable thresholds — not directions or tendencies.
