# Alert Direction Backtest

- Total EMIT alerts pulled: **482**
- Direction reproduced via BOS recompute: **482** (100%)
- Longs: 387 | Shorts: 95

Signed return: positive = engine direction was correct.
Entry = close of the alert-hour bar. No fees/slippage.

### All reproduced alerts (n=482)

| Horizon | n | Win rate | Avg signed return | Median |
|---|---|---|---|---|
| +1h | 481 | 49.5% | +0.23% | -0.00% |
| +2h | 481 | 42.4% | +0.03% | -0.28% |
| +4h | 479 | 40.7% | -0.02% | -0.39% |
| +8h | 474 | 42.0% | -0.09% | -0.46% |

### Longs only (n=387)

| Horizon | n | Win rate | Avg signed return | Median |
|---|---|---|---|---|
| +1h | 386 | 50.0% | +0.33% | +0.00% |
| +2h | 386 | 43.3% | +0.13% | -0.32% |
| +4h | 384 | 39.8% | +0.02% | -0.43% |
| +8h | 379 | 41.2% | -0.04% | -0.55% |

### Shorts only (n=95)

| Horizon | n | Win rate | Avg signed return | Median |
|---|---|---|---|---|
| +1h | 95 | 47.4% | -0.14% | -0.05% |
| +2h | 95 | 38.9% | -0.40% | -0.15% |
| +4h | 95 | 44.2% | -0.18% | -0.14% |
| +8h | 95 | 45.3% | -0.27% | -0.15% |

## BTC regime at alert time (+4h horizon)

BTC 4h return before alert bucketed: >+1% = Up, -1%..+1% = Flat, <-1% = Down

| BTC regime | n | Win rate | Avg signed return |
|---|---|---|---|
| Up (>+1%) | 105 | 36.2% | +0.72% |
| Flat | 341 | 42.8% | -0.25% |
| Down (<-1%) | 33 | 33.3% | +0.05% |

## Score percentile (+4h horizon)

| Score bucket | n | Win rate | Avg signed return |
|---|---|---|---|
| Top 25% score | 101 | 52.5% | +1.11% |
| Bottom 25% score | 142 | 41.5% | -0.13% |

## Alpha-Z strength (+4h horizon)

| |alpha_z| bucket | n | Win rate | Avg signed return |
|---|---|---|---|
| ≥3 (strong decoupling) | 270 | 45.2% | +0.25% |
| <3 (weak) | 209 | 34.9% | -0.37% |

## Volume ratio at alert (+4h horizon)

| Vol ratio | n | Win rate | Avg signed return |
|---|---|---|---|
| ≥2× median volume | 397 | 40.8% | -0.05% |
| <2× median volume | 82 | 40.2% | +0.14% |

## Alert cluster size (+4h horizon)

Cluster size = # tickers that EMITted in the same UTC hour.

| Cluster | n | Win rate | Avg signed return |
|---|---|---|---|
| Isolated (1 ticker/hr) | 256 | 42.6% | -0.02% |
| Clustered (≥5/hr) | 22 | 18.2% | -0.53% |

## Repeat-fire (+4h horizon)

repeat_fire_8h = same ticker EMITted within prior 8h.

| | n | Win rate | Avg signed return |
|---|---|---|---|
| First fire | 431 | 39.9% | -0.03% |
| Repeat within 8h | 48 | 47.9% | +0.11% |

## By UTC hour (+4h horizon, top/bottom 5)

| UTC hour | n | Win rate | Avg signed return |
|---|---|---|---|
| 20:00 UTC | 15 | 60.0% | +0.87% |
| 12:00 UTC | 21 | 57.1% | -0.07% |
| 11:00 UTC | 15 | 53.3% | +0.59% |
| 02:00 UTC | 26 | 50.0% | +0.59% |
| 14:00 UTC | 20 | 50.0% | +0.12% |
| ... | | | |
| 13:00 UTC | 30 | 33.3% | -0.93% |
| 16:00 UTC | 33 | 33.3% | +0.17% |
| 06:00 UTC | 13 | 30.8% | -2.65% |
| 23:00 UTC | 14 | 28.6% | +1.36% |
| 19:00 UTC | 21 | 23.8% | -0.59% |

## By asset class (+4h horizon)

| Asset class | n | Win rate | Avg signed return |
|---|---|---|---|
| commodity | 3 | 100.0% | +0.74% |
| crypto_meme | 45 | 31.1% | -1.30% |
| crypto_t1 | 103 | 32.0% | -0.31% |
| crypto_t2 | 328 | 44.2% | +0.24% |

## By structure type (+4h horizon)

| Structure | n | Win rate | Avg signed return |
|---|---|---|---|
| 1h | 1 | 100.0% | +3.57% |
| 4h | 29 | 44.8% | +0.12% |
| 4h_and_1h | 449 | 40.3% | -0.04% |

## By ISO week (+4h horizon)

| ISO week | n | Win rate | Avg signed return |
|---|---|---|---|
| 2026-W09 | 2 | 50.0% | +1.13% |
| 2026-W10 | 2 | 0.0% | -0.24% |
| 2026-W11 | 2 | 50.0% | -0.88% |
| 2026-W12 | 5 | 40.0% | +0.09% |
| 2026-W13 | 1 | 0.0% | -0.48% |
| 2026-W14 | 6 | 66.7% | +0.35% |
| 2026-W15 | 105 | 41.9% | -0.04% |
| 2026-W16 | 128 | 40.6% | +0.05% |
| 2026-W17 | 57 | 40.4% | -0.50% |
| 2026-W18 | 74 | 32.4% | -0.44% |
| 2026-W19 | 84 | 40.5% | +0.27% |
| 2026-W20 | 13 | 76.9% | +1.86% |

## Column glossary
- **signed_ret_+Nh_pct**: ((price at alert+Nh - entry) / entry) × 100, negated for short alerts. Positive = direction correct.
- **btc_ret_4h_pct**: BTC's close-over-close return over the 4h before the alert fired.
- **btc_range_expansion**: 1 if BTC's alert-bar range > 2× its own 48-bar median (BTC itself in impulse).
- **ticker_ret_4h_pct**: the alerted ticker's own 4h return going into the alert — was it already running?
- **vol_ratio**: alert-bar volume ÷ median volume of prior 48 bars.
- **score_pctile**: alert's composite score rank among all same-day EMITs (0=lowest, 100=highest).
- **cluster_size**: # tickers that EMITted in the same UTC hour. High = broad market move; 1 = isolated signal.
- **repeat_fire_8h**: 1 if the same ticker EMITted within the prior 8h (continuation vs fresh break).
- **breakout_dist_pct**: how far price had already moved past the BOS pivot at entry, in %.
- **alpha_z / r_alpha_pct**: BTC-decoupling metrics from the beta module.
- **Win rate**: fraction of alerts where signed return > 0 at that horizon. Coin-flip = 50%.
