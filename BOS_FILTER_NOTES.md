# Break of Structure (BOS) filter — release notes

## Summary

The legacy engine alerted on closed-bar Donchian breaks, which fires after the
breakout candle closes — typically 4 hours late on a 4h timeframe, by which
point the move is 60–80% complete (e.g. AMD May-2026 earnings: alert at $415
instead of $370). This release replaces the rolling Donchian channel with
**structural swing-high / swing-low references** (the actual prior pivots the
market was respecting) and adds a **real-time polling tier** that fires the
moment live mark price crosses a stored level with range expansion confirmed
on the in-progress bar. The engine is now a two-tier asyncio loop in a single
process.

## Architecture

```
                       ┌────────────────────────────────────────┐
                       │            single Python process       │
                       │            asyncio event loop          │
                       └────────────────────────────────────────┘
                                          │
                ┌─────────────────────────┴────────────────────┐
                ▼                                              ▼
   ┌─────────────────────────┐                   ┌─────────────────────────┐
   │  TIER 1 — Discovery     │                   │  TIER 2 — Trigger Watch │
   │  every FAST_CADENCE_SEC │                   │  every 60s              │
   │  (default 300s)         │                   │                         │
   ├─────────────────────────┤                   ├─────────────────────────┤
   │ universe                │                   │ list_active_watchlist() │
   │  → ranker (top N)       │                   │  → for each entry:      │
   │  → catalysts (news)     │                   │     get_market_snapshot │
   │  → classifier (Haiku)   │                   │     check vs stored refs│
   │  → suppression.evaluate │                   │     if cross + expand:  │
   │                         │                   │       send_bos_alert    │
   │  decision ∈ {           │                   │       remove from list  │
   │     EMIT,               │                   │     update_poll(price)  │
   │     WATCHLIST,          │                   │                         │
   │     DROP                │                   │ expire_stale_watchlist  │
   │  }                      │                   │   runs first in Tier 1  │
   └─────────────────────────┘                   └─────────────────────────┘
            │                                              │
            ├──── EMIT ──────┐               ┌── PROMOTE ──┤
            │                ▼               ▼             │
            │        ┌─────────────────────────────┐       │
            │        │  telegram.send_bos_alert     │      │
            │        │  (source = "tier1_immediate"  │      │
            │        │       or "tier2_promoted")    │      │
            │        └─────────────────────────────┘       │
            │                                              │
            └─── WATCHLIST ──┐                             │
                             ▼                             │
                    ┌────────────────────────┐             │
                    │  watchlist (SQLite)    │ ◄───────────┘
                    │  + send_watchlist_     │
                    │    notification        │
                    └────────────────────────┘
```

The engine becomes a discovery + confirmation pair. Tier 1 finds *what's
interesting now* (catalyst-driven score above threshold). Tier 2 watches *when
the chart confirms* (live cross of the structural level). An alert only fires
when both align — which is precisely the moment a discretionary trader would.

## How to read WATCHLIST → EMIT promotions in Telegram

A Tier 1 cycle finds AMD with a strong earnings catalyst but price is still
$355, below the prior swing high at $362. Telegram receives:

> 👀 **WATCHLIST — AMD** +4.21%
> equity
>
> **Catalyst:** AMD beat Q1 estimates
> **Awaiting:** break above $362.0000 for LONG confirmation
>
> Vol $50.0M | OI $10.0M | Score 82

47 minutes later, price ticks $370 with the in-progress bar wider than 1.5×
median range. Tier 2 fires:

> 🔥 **RADAR — AMD** +12.40% **\[promoted: 0.8h on watchlist]**
> equity · US-OPEN
>
> **BOS confirmed:** LONG above $362.0000
> **Catalyst:** AMD beat Q1 estimates
> **Type:** earnings · **Conviction:** 85/100
> **Horizon:** swing
>
> Earnings beat with raised guidance
>
> **Kill:** Drop back below 362
>
> Vol $50.0M | OI $10.0M | Funding 0.0000%
> [Open in Lighter](https://app.lighter.xyz/trade/AMD)

The `[promoted: 0.8h on watchlist]` tag tells you the catalyst was already
classified — the alert is firing on confirmed structure, not fresh news. That
signal is high-quality precisely because the catalyst pre-existed the cross.

## Tuning guide — the 4 most-touched config values

All in `radar/config.py`.

### `WATCHLIST_SCORE_THRESHOLD` (default 60)
Floor for entering the watchlist when BOS hasn't confirmed at scan time.
- **Lower (40-50)** → more soft signals, more Tier 2 polling cost, more
  promotions per day. Use when you want everything the classifier flagged.
- **Higher (80+)** → only the most catalyst-rich moves are watched. Best when
  Tier 2 polling is rate-limited or you want a low-noise mode.

### `WATCHLIST_TTL_HOURS` (default 72)
How long a watchlist entry survives without a confirmed break before being
auto-expired.
- **Shorter (24h)** → favors fast-moving catalysts, drops slow stories.
  Cheaper polling.
- **Longer (96–120h)** → captures multi-day arcs (e.g. TON, where the
  catalyst-to-confirmation lag stretched ~5 days). Watchlist size will grow.

### `SWING_LOOKBACK_HOURS` (default 48)
How far back to scan for prior pivots when computing references.
- **Shorter (24h)** → reacts to recent local pivots, works on choppy markets,
  but more noise.
- **Longer (96h+)** → uses heavier macro-level pivots; fewer but more
  significant breaks.

### `RANGE_EXPANSION_MULTIPLIER` (default 1.5)
Current bar range vs lookback median required to qualify as a break.
- **Lower (1.2)** → fires on weaker thrusts; risk of being faked out by wicks.
- **Higher (2.0+)** → only confirms on impulsive moves. Fewer signals,
  higher precision.

## Known limitations

1. **SDK polling latency.** Tier 2 polls every 60s by default
   (`TRIGGER_POLL_INTERVAL_SEC`). Live price information is therefore at most
   60s stale — fine for catalyst-grade timing, but a hard floor on detection
   speed. WebSocket-based live price is on the v3 roadmap.

2. **In-progress bar range can be misleading early in the bar.** During the
   first ~10 minutes of an hourly bar, `current_bar_range` is small even if a
   real impulsive move is forming. The range-expansion check therefore gates
   *late* in the bar's lifecycle. This is by design — false positives early
   in a bar would be more costly than a small lag.

3. **First valid swing wins.** When multiple historical pivots are within the
   eligible lookback window, the *highest* unbroken candidate is returned for
   `find_swing_high` (and lowest for `find_swing_low`). In choppy markets with
   multiple equally valid swings, you may see the alert reference a level
   that's slightly higher than what most chartists would draw. Multi-pivot
   ranking by recency × magnitude is on the v2.1 list below.

4. **Cold-start watchlist.** A market with `<10` lookback bars produces
   `median_bar_range = 0`, which we explicitly guard so no false breaks fire.
   New listings and freshly-added tickers will quietly miss the BOS gate
   until the rolling 1h history fills in (~10 hours minimum).

5. **BTC-beta gate is now stricter.** Previously the rule dropped only when
   *both* alpha_z and r_alpha_pct were weak (AND). The new rule drops on
   *either* (OR). This is a deliberate tightening matching the spec — but
   expect ~30-50% more crypto candidates to drop with `pure_btc_beta` than
   under the legacy gate.

## v2.1 roadmap

The following deliberately did not ship in this PR; they're tracked for the
next iteration:

1. **Per-asset-class swing lookback tuning** — currently a single
   `SWING_LOOKBACK_HOURS=48` applies everywhere; equities and memes likely
   want different values.
2. **Multi-timeframe BOS confirmation** — require 4h + 1h to agree before
   firing.
3. **Two-bar close confirmation** — alert only on the second close above the
   level (filters single-wick fakeouts).
4. **WebSocket-based live price** — replaces 60s SDK polling with sub-second
   triggers.
5. **Sector-relative breakout normalization** — measure breakouts against
   asset-class beta-residual rather than absolute price.
6. **Volume-weighted breakout strength scoring** — annotate alerts with how
   much volume confirmed the break.
