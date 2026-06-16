# Catalyst Radar — Execution Layer Build Spec (v1.1)

> v1.1 changes: Lighter native server-side SL/TP confirmed (blocker cleared);
> live real money from fill #1 (paper skipped, $5/trade risk); data capture
> promoted to a first-class system (§10) per "store everything" directive.

> Hand-off spec for Claude Code. Turns the read-only alert engine into a live,
> always-on, risk-managed executor on Lighter. Every rule below is tagged
> **[VALIDATED]** (survives the 481-alert backtest + stress tests),
> **[HYPOTHESIS]** (mechanism plausible, untested live), or **[SAFETY]**
> (non-negotiable capital protection, independent of edge).
>
> **v1 mandate is NOT profit. It is generating the intrabar dataset that the
> close-to-close backtest could not.** Size tiny. Validate stops survive. Then widen.

---

## 0. The model the data implies (build to this, not to "directional alpha")

The engine is a **short-horizon momentum-burst detector.** Edge = freshness of an
idiosyncratic impulse. It decays inside ~1h. Therefore:
- Enter as fresh as possible (15m frame is the primary trigger for crypto).
- Exit at +1h by default; only extend when the trade is already working.
- Direction-call (LLM/Grok) adds nothing — do not gate on it. **[VALIDATED: Grok p=0.93@4h]**

---

## 1. Hook point

`radar/main.py` EMIT branch (~line 648) and the Tier-2 promotion EMIT
(~line 836). After `telegram.send_bos_alert(...)`, add:

```python
if plan is not None:
    executor.maybe_execute(
        market=market, plan=plan, metadata=metadata,
        adjudicated=adjudicated, tier=1,  # or 2 at the trigger-poll site
    )
```

Execution is **parallel to** the Telegram send, never replaces it. If the
executor raises, the alert still goes out (fail-open, matches engine philosophy).

---

## 2. Tiered entry — direction-gate, then size

Compute `conviction_tier` from backtest-validated features already in `metadata`
(`alpha_z` from beta.py, `score_pctile` from ranker). **[VALIDATED]**

| Tier | Condition | Action | Backtest WR | Size |
|---|---|---|---|---|
| **A** | `\|alpha_z\| ≥ 3 AND score_pctile ≥ 75` | trade, may extend past +1h | 56.5% @4h, 54.3% @1h | full |
| **B** | `score_pctile ≥ 75` (alone) | trade, **hard +1h exit** | 52.5% @4h | 2/3 |
| **C-pop** | `cluster_size ≥ 5` OR `btc_ret_4h > +2%` | trade, **mandatory +1h exit, no extension** | 68% / 64% @1h, collapses @4h | 1/2 |
| **SKIP** | `\|alpha_z\| ∈ [2,3) AND score_pctile ≥ 50` | **do not trade** (the trap: 16.7% WR) | — | 0 |
| **SKIP** | `crypto_t1` (structurally inert, no tails) | do not trade | 32% | 0 |
| **SKIP** | `vol_ratio > 15× AND \|alpha_z\| < 3` (blowoff) | do not trade | 15.4% | 0 |

**v1 ships Tier A ONLY.** B / C-pop are config-flagged OFF until intrabar data
confirms their stops survive. Widen the aperture by data, not by feeling.

```python
EXECUTOR_ENABLED_TIERS = {"A"}        # widen later: {"A","B","C_pop"}
```

---

## 3. Position sizing — the missing layer (`risk_per_unit` → actual size)

The §7 trade plan gives `risk_per_unit = |entry − stop|`. Sizing is just:

```
size_usd      = MAX_LOSS_PER_TRADE_USD / (risk_per_unit / entry)
contracts     = size_usd / entry
tier_size     = contracts × TIER_SIZE_MULT[tier]   # A=1.0, B=0.66, C_pop=0.5
```

This makes the **dollar loss at stop fixed and identical** regardless of how wide
the stop is. **This is the stop-loss discipline you don't do manually — enforced
in math.** **[SAFETY]**

Score sizes *up within a tier*, never overrides the gate (score is orthogonal to
direction — high score + marginal alpha_z = the 16.7% trap): **[VALIDATED]**

```python
score_mult = clamp(score_pctile / 75, 1.0, 1.5)   # only applies to Tier A
final_size = tier_size × score_mult
```

Config:
```python
MAX_LOSS_PER_TRADE_USD = 5.0      # your stated max. de minimis on purpose.
MAX_CONCURRENT_POSITIONS = 3
MAX_TOTAL_EXPOSURE_USD = 200.0
LEVERAGE_CAP = 10                 # margin efficiency only; size is risk-defined, NOT leverage-defined
```

> You got liquidated at 50x on a 10% move. Under this model leverage never
> determines size — `MAX_LOSS_PER_TRADE_USD` does. Leverage is capped low and
> only affects margin posted. A 10% adverse move can never liquidate a position
> whose stop is set at −$5. **[SAFETY]**

---

## 4. Lighter order client (the write layer — does not exist yet)

`radar/lighter.py` is read-only. Build `radar/lighter_exec.py` on the official
SDK (`pip install lighter-sdk`; `import lighter` — verify package name resolves,
else install from `github.com/elliottech/lighter-python`). Key is in `.env`.

### 4.1 Signer init
```python
client = lighter.SignerClient(
    url="https://mainnet.zklighter.elliot.ai",
    api_private_keys={API_KEY_INDEX: PRIVATE_KEY},   # from .env
    account_index=ACCOUNT_INDEX,                     # resolve via accounts_by_l1_address
)
```
SDK manages nonces automatically (per API key). Auth token for read endpoints via
`create_auth_token_with_expiry`.

### 4.2 Native order types (confirmed in Lighter docs — server-side, the blocker is cleared)
```
ORDER_TYPE_LIMIT (0)  MARKET (1)  STOP_LOSS (2)  STOP_LOSS_LIMIT (3)
TAKE_PROFIT (4)  TAKE_PROFIT_LIMIT (5)  TWAP (6)
```
**Stops/TPs live on the exchange.** A VPS death mid-trade does not unprotect the
position. **[SAFETY]**

### 4.3 Price/size scaling — hard-validate or you fat-finger by 10,000×
Query `orderBookDetails` per market at startup; cache `price_decimals`,
`size_decimals`, `min_base_amount`, `min_quote_amount`.
```
base_amount_int = round(units    × 10**size_decimals)
price_int       = round(usd_price × 10**price_decimals)
```
Reject any order whose `price_int` deviates > FATFINGER_PCT from live mark. **[SAFETY]**

### 4.4 TP/SL need BOTH a trigger and a slippage-limit price
Per docs: a STOP_LOSS/TAKE_PROFIT order takes a `trigger_price` AND a `price`
(worst acceptable fill). Set the limit price generously past the trigger
(`STOP_SLIPPAGE_PCT`, e.g. 0.5%) so a fast stop actually fills instead of resting.

### 4.5 Order sequence (correctness over atomicity)
Market IOC can partial-fill, so **do not** pre-size protective orders to intended
size. Sequence per entry:
1. `create_order(..., MARKET, IOC, reduce_only=False, client_order_index=COI_entry)`.
2. Await fill via WS account channel → read **actual** filled `base_amount` + avg price.
3. Immediately post, sized to the *filled* amount:
   - `STOP_LOSS reduce_only=True trigger=plan.stop price=stop±slippage COI_stop`
   - `TAKE_PROFIT reduce_only=True trigger=plan.tp1 price=tp1∓slippage COI_tp`
4. **Stop-mandatory invariant:** if step 3 stop-post fails → immediately
   `MARKET reduce_only` close the entry. Never hold an unprotected position. **[SAFETY]**

### 4.6 Idempotency + reconciliation **[SAFETY]**
- `client_order_index = uint48(hash(alert_ts, ticker, leg))`. Persist the mapping.
  A loop restart re-deriving the same COI lets Lighter dedupe; never double-enters.
- **On boot:** query open positions + open orders from Lighter, reconcile against
  the local `positions` table. Any live position without a tracked stop →
  post a stop immediately or flatten. Never start blind.

### 4.7 Fill/position tracking
WS account channel (event-driven) is the source of truth for fills, partials, and
position state. Poll `account` as a fallback only. Every event → write to `fills`
/ `orders` (§10) with the raw payload retained.

Verify with 1-contract live orders (place → stop posts → cancel → flatten) before
wiring to the EMIT hook.

---

## 5. Exit engine — the highest-EV lever

A lightweight async exit loop (reuse the Tier-2 60s cadence) tracks every open
position: **[VALIDATED — flips book Σ −9.3 → +112.2]**

```
on entry:        post server-side stop at plan.stop (SAFETY backstop)
at +1h mark:
    if tier in {B, C_pop}:           close (reduce-only market)
    if tier == A:
        if pnl_at_1h > EXTENSION_THRESHOLD:   # "working" → let it run
            move stop → breakeven, trail by plan ATR to +4h
        else:                                  close at +1h
force +1h close (override extension) if metadata flags blowoff:
    vol_ratio > 15×  OR  crypto_meme  OR  cluster_size ≥ 5
```

```python
TIME_EXIT_HOURS = 1.0
EXTENSION_THRESHOLD_R = 0.5     # +1h PnL must clear +0.5R to hold; else cut
MAX_HOLD_HOURS = 4.0
```

> The asymmetric rule is the edge: 19 of top-20 winners *built* from +1h→+4h;
> only faders and the FF outlier spiked-and-died. Let green run, cut flat/red at
> +1h. **[VALIDATED §4]**

---

## 6. Circuit breaker — the anti-blowup (independent of edge)

**[SAFETY] — this is why the bot beats Manny-manual.**

```python
DAILY_MAX_LOSS_USD      = 50.0    # hit → halt all new entries until 00:00 UTC
DAILY_MAX_TRADES        = 30      # matches engine DAILY_ALERT_BUDGET
CONSECUTIVE_LOSS_HALT   = 5       # 5 losses in a row → halt + Telegram ping
KILL_SWITCH_FILE        = "/tmp/radar_halt"   # touch file = immediate flat + stop
```

On any breach: cancel open orders, optionally flatten, send Telegram, stop
opening new positions. Existing stops stay server-side regardless.

---

## 7. Data capture — the actual deliverable of v1

The full schema is **§10**. The principle: **capture raw, derive later.** Store
per-minute marks and raw API payloads, not just summary stats, so any future
metric is recomputable without re-running live. This closes the two gaps that
crippled the backtest (no intrabar high/low, no news rows) permanently. Until
`position_marks` confirms stops survive intrabar noise, every WR above is a
hypothesis. **[HYPOTHESIS until logged]**

---

## 8. Explicitly NOT in v1 (novelty traps — rejected by data)

- ❌ **News-reading execution layer.** Grok adds no direction edge. The overnight
  catalyst (Trump-Iran type) is already caught by the price impulse (alpha_z
  spike) — no news layer needed. **[VALIDATED: Grok p=0.93]**
- ❌ **Self-learning / online ML.** Fixed rules first. Adaptation is a v2 problem
  you earn only after v1 prints green on real fills.
- ❌ **LLM direction gating.** Advisory only, per v3 invariant; the backtest
  confirms gating on it would destroy signal for nothing.

---

## 9. Scoreboard (what, scored how, by when)

| Gate | Metric | Pass condition |
|---|---|---|
| **G0 — plumbing** | testnet/1-contract orders place + stop + cancel cleanly | by **day 3** |
| **G1 — first live fill** | Tier-A only, $5 risk, server-side stops, +1h exit, circuit breaker live | by **day 10** |
| **G2 — intrabar validation** | ≥ 30 live fills logged; `stop_hit_before_1h` rate + live WR vs backtest 54.3%@1h | by **~3 weeks** |
| **G3 — go/no-go** | live Tier-A WR within ~5pp of backtest AND net of fees/slippage > 0 | decision gate |

If G2 shows real WR << backtest (stops getting wicked) → the fix is stop
placement (wider structural stop, or volatility-scaled buffer), not more
filters. If G3 is green → widen to Tier B, then C-pop, re-validating each.

**v1 is dead if nothing places a live order by day 10.** Conditions are right
(novelty, live feedback, spec already written, your own money on the line). Build
order: §10 schema → §4 client → §3 sizing → §6 breaker → §2 tiering at hook →
§5 exit. Schema first so no early trade is lost to a missing column.

---

## 10. Data schema — store everything useful and potentially useful

SQLite, same DB as the engine (`storage.py` convention: unix-int seconds for
`ts`, ISO-8601 naive-UTC strings elsewhere — do not mix). Every table keeps a
`raw_json` TEXT column holding the unparsed source payload: **the point is that a
field you didn't think to parse today is still recoverable for analysis later.**
All trade-linked rows carry `config_version_id` so changing a threshold never
contaminates the dataset.

### `signal_snapshots` — the full feature vector at alert time (one row per EMIT)
The backtest was blind to news and intrabar; this table is where that ends. Store
**everything the engine computed**, not just what the executor used:
```
id, alert_ts, ticker, asset_class, tier_decision, structure_type(4h/1h/4h_and_1h),
breakout_level, swing_high_4h, swing_low_4h, swing_ref_ts, median_bar_range_1h,
distance_past_pivot_pct, range_ratio_1h, range_ratio_15m,
-- ranker / beta features (the validated predictors)
score, score_pctile, alpha_z, r_alpha_pct, vol_ratio, volume_z, cluster_size,
pop_score, oi_velocity_z, funding_z, wash_penalty,
-- positioning / book / macro
oi_usd, funding_1h, book_bid_usd, book_ask_usd, book_ratio, book_sentiment,
vpoc_price, vpoc_near_breakout, btc_ret_4h, btc_range_expansion, htf_trend_align_7d,
-- adjudicator + classifier (advisory, store to re-test the v3 question live)
adj_direction, adj_confidence, adj_setup_quality, adj_conviction_tier, adj_flipped, adj_thesis,
clf_catalyst_type, clf_direction, clf_confidence, clf_summary, clf_evidence_quotes,
-- the missing variable: raw news corpus the classifier saw
news_items_json,
-- timing
utc_hour, day_of_week, tier_source(1/2), watchlist_age_hours,
config_version_id, raw_json
```

### `executions` — the decision record (one row per EMIT, even skips)
```
id, signal_snapshot_id, acted(bool), skip_reason, conviction_tier,
risk_per_unit, intended_entry, intended_stop, intended_tp1, intended_tp2,
computed_size_usd, computed_contracts, size_mult_score, leverage_used,
free_margin_at_decision, config_version_id, created_at
```

### `orders` — every leg sent (entry, stop, tp, time-exit, breakeven-modify, cancel)
```
id, execution_id, ticker, market_index, leg(entry/stop/tp/time_exit/be_modify),
client_order_index, order_type, is_ask, reduce_only, trigger_price, limit_price,
base_amount_int, tx_hash, submit_ts, ack_status, terminal_status(filled/cancelled/rejected),
reject_reason, raw_request_json, raw_response_json
```

### `fills` — every fill / partial (WS account events)
```
id, order_id, ticker, fill_ts, fill_price, fill_base_amount, fee_usd,
is_partial, cumulative_filled, raw_json
```

### `positions` — lifecycle (one row per position, mutated through its life)
```
id, execution_id, ticker, direction, open_ts, entry_avg_price, size_contracts,
stop_order_id, tp_order_id, stop_price_current, breakeven_moved(bool), trailing_active(bool),
exit_ts, exit_reason(stop/tp/time_exit/breaker/manual/reconcile),
realized_pnl_usd, fees_total_usd,
slippage_vs_alert_px_bps, slippage_vs_bar_close_bps,
mfe_pct, mae_pct, pnl_at_1h_pct, pnl_at_4h_pct, stop_hit_before_1h(bool),
time_to_mfe_min, time_to_mae_min, config_version_id, raw_json
```

### `position_marks` — **the #1 missing variable.** Per-minute mark, entry→+4h
```
id, position_id, ts, mark_price, unrealized_pnl_pct, minutes_since_entry
```
Cheap at your scale (tens of trades × 240 rows). This is what lets you answer
"did the +1h win actually hold or just close green after a wick that stopped me
out," which no close-to-close backtest can. Sample every 60s while any position
is open; keep marking to +4h even after exit (for counterfactual exit research).

### `equity_snapshots` — account state over time (for drawdown + breaker audit)
```
id, ts, balance_usd, free_margin_usd, total_exposure_usd, open_position_count,
daily_realized_pnl_usd, consecutive_losses, raw_json
```
Snapshot every cycle + on every fill/exit.

### `config_versions` — so threshold changes don't poison the dataset
```
id, git_sha, created_at,
enabled_tiers, max_loss_per_trade, leverage_cap, time_exit_hours,
extension_threshold_r, max_concurrent, daily_max_loss, full_config_json
```
Write a new row whenever any executor config changes; stamp its id on every
trade. Future analysis segments by `config_version_id` — you never have to wonder
"was this trade under the old or new rules."

**Analysis payoff:** with this schema you can later run, without any new live
capture — true intrabar stop-survival rates, optimal time-exit re-fitting on real
marks, slippage decomposition (alert→fill vs fill→close), the live v3 LLM-conflict
re-test (`adj_direction` vs structure across far more than N=10), news→outcome
mining on the raw `news_items_json`, and per-config-version WR comparison. The
backtest's two fatal gaps are closed permanently.
