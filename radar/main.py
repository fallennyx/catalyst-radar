"""Catalyst Radar — two-tier asyncio orchestrator.

TIER 1 (every FAST_CADENCE_SEC): full universe scan → ranker → catalysts →
classifier → suppression. Each candidate routes to one of {EMIT, WATCHLIST, DROP}.

TIER 2 (every TRIGGER_POLL_INTERVAL_SEC): polls watchlist tickers for live
mark-price crosses against stored swing references. On confirmed cross +
range expansion, promote the watchlist entry to EMIT and remove it.

Both tiers run in a single asyncio event loop in one Python process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from . import (
    beta, catalysts, classifier, config, fetch_bars, lighter, predictor,
    ranker, storage, suppression, telegram, trade_plan, universe,
)
from .suppression import Alert

log = logging.getLogger("radar")


# Module state shared across loops:
#   _LAST_PRUNE_TS — last DB prune timestamp; 0.0 forces a prune on first cycle
#   _LAST_SCAN_TS  — wall time of the last completed Tier 1 cycle; powers the
#                    hourly report's heartbeat line
#   _LAST_TOP_CANDIDATES — list of (ticker, score, pct_24h) from the last Tier 1
#                    scan; surfaced in the hourly report so the user can see
#                    what's brewing even when no BOS has fired yet
_LAST_PRUNE_TS: float = 0.0
_LAST_SCAN_TS: float = 0.0
_LAST_TOP_CANDIDATES: list[tuple[str, float, float]] = []


# ============================================================================
# logging + history helpers
# ============================================================================

def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def build_history(ticker: str) -> dict[str, list[float]]:
    """Pull rolling 1h bars and assemble the arrays the ranker/beta want."""
    rows = storage.recent_bars(ticker, hours=config.ROLLING_WINDOW_DAYS * 24)
    if not rows:
        return {}
    closes = [r.close for r in rows if r.close is not None]
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev)
    return {
        "ret_1h": rets,
        "vol_1h": [r.volume for r in rows if r.volume is not None],
        "oi_1h": [r.oi for r in rows if r.oi is not None],
        "funding": [r.funding for r in rows if r.funding is not None],
    }


def btc_history() -> list[float]:
    rows = storage.recent_bars("BTC", hours=config.ROLLING_WINDOW_DAYS * 24)
    closes = [r.close for r in rows if r.close is not None]
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev)
    return rets


def _record_market_bar(market: Any) -> None:
    """Fold a live mark-price snapshot into both the 1h and 15m bucket OHLCs.

    Lighter only exposes a current price (no in-progress bar OHLC). We must
    aggregate across Tier-1 ticks ourselves — otherwise the live bar's
    high/low stay NULL, ``has_breakout_structure`` sees range = 0, the
    range-expansion gate never opens, and every cycle becomes
    ``DROP no_structure_break``.

    Writes to both ``bars_1h`` and ``bars_15m`` so the BOS engine's parallel
    fast-confirmation gate has fresh in-progress 15m data to evaluate.
    """
    now = int(time.time())
    storage.upsert_bar_from_tick(
        ticker=market.ticker,
        ts=now // 3600 * 3600,
        price=market.price,
        volume=market.volume_24h_usd,
        oi=market.oi_usd,
        funding=market.funding_1h,
    )
    storage.upsert_bar_15m_from_tick(
        ticker=market.ticker,
        ts=storage.floor_to_15m_bucket(now),
        price=market.price,
        volume=market.volume_24h_usd,
        oi=market.oi_usd,
        funding=market.funding_1h,
    )


def build_alert(market: Any, result: Any, history_dict: dict[str, list[float]],
                btc_rets: list[float] | None = None,
                score: float = 0.0) -> Alert:
    """Translate (market, classifier_result) → Alert payload for suppression."""
    hist = dict(history_dict or {})
    if market.asset_class.startswith("crypto") and btc_rets:
        hist["btc_ret_1h"] = btc_rets
    alpha_z, r_alpha_pct = beta.compute_alpha_z(market, hist)
    return Alert(
        ticker=market.ticker,
        asset_class=market.asset_class,
        score=float(score),
        alpha_z=float(alpha_z) if alpha_z != float("inf") else 0.0,
        r_alpha_pct=float(r_alpha_pct or 0.0),
        classifier_result=result,
    )


# ============================================================================
# Startup backfill — fill bars_1h so BOS can fire immediately on (re)start
# ============================================================================

def _compute_backfill_hours(ticker: str, asset_class: str | None = None) -> int | None:
    """Returns hours of history to backfill for ``ticker``, or None to skip.

    The fetch target defaults to ``BOS_BAR_HISTORY_HOURS`` (240h ≈ 10 calendar
    days), which is enough for the 4h-BOS frame on a 24/7 asset. For asset
    classes with intraday-restricted trading (equities only trade ~6.5h/day,
    so 240 calendar hours yield ~65 bars — below the 132-bar 4h-BOS floor) the
    target is overridden via ``BACKFILL_HOURS_BY_CLASS``. The density check
    window stays at ``BOS_BAR_HISTORY_HOURS`` so the threshold semantics don't
    change for crypto; this means equities re-trigger a full backfill on every
    boot (they never reach the 120-bar density floor in 240h of calendar time),
    which is the right trade given equities are rate-limit-bounded by yfinance.

    Two-stage logic:

    1. **Density check** — count bars in the BOS window. If we don't have at
       least ``BACKFILL_MIN_DENSITY_FRAC`` of the window populated, fetch the
       full target regardless of recency.
    2. **Freshness check** — if density is OK and the most recent bar is
       < BACKFILL_GAP_THRESHOLD_SEC old, skip. Otherwise fetch just the
       missing tail (clamped to the target window).
    """
    target = config.BACKFILL_HOURS_BY_CLASS.get(
        asset_class or "", config.BOS_BAR_HISTORY_HOURS,
    )
    bar_count = storage.count_recent_bars(ticker, hours=config.BOS_BAR_HISTORY_HOURS)
    min_density = int(config.BOS_BAR_HISTORY_HOURS * config.BACKFILL_MIN_DENSITY_FRAC)
    if bar_count < min_density:
        return target
    last = storage.last_bar_ts(ticker)
    if last is None:
        return target
    gap_sec = int(time.time()) - int(last)
    if gap_sec < config.BACKFILL_GAP_THRESHOLD_SEC:
        return None
    return min(target, max(1, math.ceil(gap_sec / 3600)))


def _parse_iso_to_unix(value: str) -> int | None:
    """`_iso()` in fetch_bars emits `%Y-%m-%dT%H:%M:%SZ`. Reverse cleanly."""
    if not value:
        return None
    try:
        v = value.rstrip("Z")
        dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return None


def _persist_fetched_rows(ticker: str, rows: list[dict]) -> int:
    """Bulk-insert fetched rows into bars_1h. Idempotent (INSERT OR REPLACE).

    Per-row try/except so one malformed row can't tank a 240-row batch.
    """
    inserted = 0
    for r in rows or []:
        try:
            ts = _parse_iso_to_unix(r.get("ts", ""))
            if ts is None:
                continue
            storage.insert_bar(
                ticker=ticker,
                ts=ts,
                open_=r.get("open"),
                high=r.get("high"),
                low=r.get("low"),
                close=r.get("price"),
                volume=r.get("volume_24h_usd"),
                oi=r.get("oi_usd"),
                funding=r.get("funding_1h"),
            )
            inserted += 1
        except Exception as e:
            log.debug("backfill %s: skipped malformed row: %s", ticker, e)
    return inserted


def _persist_fetched_rows_15m(ticker: str, rows: list[dict]) -> int:
    """Same as _persist_fetched_rows but writes to bars_15m."""
    inserted = 0
    for r in rows or []:
        try:
            ts = _parse_iso_to_unix(r.get("ts", ""))
            if ts is None:
                continue
            storage.insert_bar_15m(
                ticker=ticker,
                ts=ts,
                open_=r.get("open"),
                high=r.get("high"),
                low=r.get("low"),
                close=r.get("price"),
                volume=r.get("volume_24h_usd"),
                oi=r.get("oi_usd"),
                funding=r.get("funding_1h"),
            )
            inserted += 1
        except Exception as e:
            log.debug("backfill 15m %s: skipped malformed row: %s", ticker, e)
    return inserted


async def _backfill_bars_for_universe() -> int:
    """Fill bars_1h for each Lighter ticker with the missing history tail.

    Runs once at engine start before the Tier 1/2 loops. Idempotent: re-runs
    on a populated DB log mostly `fresh — skipping` lines. Cancellable on
    SIGTERM — checks `_RUNNING` between tickers and propagates CancelledError
    from `asyncio.wait_for`.
    """
    log.info("Backfill: starting")
    markets = universe.get_leveraged_universe()
    if not markets:
        log.warning("Backfill: empty Lighter universe — skipping (Tier 1 will retry)")
        return 0

    fetchable: list[tuple[str, str]] = []
    unmappable: list[str] = []
    for m in markets:
        if fetch_bars.is_fetchable(m.ticker, m.asset_class):
            fetchable.append((m.ticker, m.asset_class))
        else:
            unmappable.append(m.ticker)
    if unmappable:
        log.warning(
            "Backfill: %d tickers have no fetcher mapping — they will cold-start "
            "the slow way: %s",
            len(unmappable), unmappable,
        )

    start = time.time()
    total_inserted = 0
    skipped = 0
    failed = 0
    fetched = 0

    for i, (ticker, asset_class) in enumerate(fetchable, start=1):
        if not _RUNNING:
            log.info("Backfill: cancelled after %d/%d tickers", i - 1, len(fetchable))
            break
        hours = _compute_backfill_hours(ticker, asset_class)
        if hours is None:
            skipped += 1
            continue
        days = max(1, math.ceil(hours / 24))
        # Honor per-ticker overrides (e.g. PAXG is commodity-classed but
        # trades on crypto venues — route via fetch_crypto).
        route_class = fetch_bars.TICKER_ROUTE_OVERRIDES.get(ticker, asset_class)
        fetcher = fetch_bars.ROUTES.get(route_class)
        if fetcher is None:
            failed += 1
            continue
        t0 = time.time()
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(fetcher, ticker, days),
                timeout=config.BACKFILL_PER_TICKER_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            failed += 1
            log.warning(
                "Backfill %s: timeout after %ds",
                ticker, config.BACKFILL_PER_TICKER_TIMEOUT_SEC,
            )
            continue
        except asyncio.CancelledError:
            log.info("Backfill: cancelled mid-fetch at %s (%d/%d)",
                     ticker, i, len(fetchable))
            raise
        except Exception as e:
            failed += 1
            log.warning("Backfill %s: fetch failed: %s", ticker, e)
            continue
        inserted = _persist_fetched_rows(ticker, rows or [])
        total_inserted += inserted
        fetched += 1
        dt = time.time() - t0
        log.info(
            "Backfill %s: +%d bars in %.1fs (asset_class=%s, %d/%d)",
            ticker, inserted, dt, asset_class, i, len(fetchable),
        )
        await asyncio.sleep(config.BACKFILL_SLEEP_BETWEEN_SEC)

    elapsed = time.time() - start
    log.info(
        "Backfill complete: %d bars across %d tickers in %.1fs "
        "(skipped fresh=%d, failed=%d, unmappable=%d)",
        total_inserted, fetched, elapsed, skipped, failed, len(unmappable),
    )
    return total_inserted


async def _backfill_bars_15m_for_universe() -> int:
    """Backfill the bars_15m table for tickers with a 15m route.

    Only crypto tickers have 15m fetchers (Coinbase / Bybit); equity /
    commodity / forex are skipped (yfinance intraday minimum granularity is
    workable but adds complexity, and the 15m frame's payoff is largest on
    24/7 crypto where intra-bar latency is the killer).

    Idempotent: re-runs on a populated DB skip tickers whose latest 15m bar
    is fresher than ``BACKFILL_15M_GAP_THRESHOLD_SEC``. Target window:
    ``BOS_15M_HISTORY_BARS`` (~200 bars ≈ 50h).
    """
    log.info("Backfill 15m: starting")
    markets = universe.get_leveraged_universe()
    if not markets:
        log.warning("Backfill 15m: empty universe — skipping")
        return 0

    target_bars = config.BOS_15M_HISTORY_BARS
    target_hours = target_bars * 15 // 60 + 1   # 51h for 200 bars
    days = max(1, math.ceil(target_hours / 24))  # 3 days (clamped by fetchers)
    now = int(time.time())

    total_inserted = 0
    fetched = 0
    skipped = 0
    failed = 0

    for i, m in enumerate(markets, start=1):
        if not _RUNNING:
            log.info("Backfill 15m: cancelled after %d/%d tickers", i - 1, len(markets))
            break
        route_class = fetch_bars.TICKER_ROUTE_OVERRIDES.get(m.ticker, m.asset_class)
        fetcher = fetch_bars.ROUTES_15M.get(route_class)
        if fetcher is None:
            # equity / commodity / forex — no 15m fetcher; BOS will use 1h only
            continue
        last = storage.last_bar_15m_ts(m.ticker)
        if last is not None and (now - int(last)) < config.BACKFILL_15M_GAP_THRESHOLD_SEC:
            density = storage.count_recent_bars_15m(m.ticker, hours=target_hours)
            if density >= int(target_bars * config.BACKFILL_MIN_DENSITY_FRAC):
                skipped += 1
                continue
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(fetcher, m.ticker, days),
                timeout=config.BACKFILL_PER_TICKER_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            failed += 1
            log.warning("Backfill 15m %s: timeout", m.ticker)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            failed += 1
            log.warning("Backfill 15m %s: fetch failed: %s", m.ticker, e)
            continue
        inserted = _persist_fetched_rows_15m(m.ticker, rows or [])
        total_inserted += inserted
        fetched += 1
        log.info("Backfill 15m %s: +%d bars (%d/%d)", m.ticker, inserted, i, len(markets))
        await asyncio.sleep(config.BACKFILL_SLEEP_BETWEEN_SEC)

    log.info(
        "Backfill 15m complete: %d bars across %d tickers (skipped fresh=%d, failed=%d)",
        total_inserted, fetched, skipped, failed,
    )
    return total_inserted


# ============================================================================
# Auto-prune — wired into Tier 1; runs once per PRUNE_INTERVAL_SEC at most
# ============================================================================

async def _maybe_prune(now_ts: float) -> None:
    """Prune old bars + alerts if PRUNE_INTERVAL_SEC has elapsed since last run.

    First invocation always fires (`_LAST_PRUNE_TS=0.0`) — cleans up anything
    stale from a previous deployment sharing the same DB volume.
    """
    global _LAST_PRUNE_TS
    if now_ts - _LAST_PRUNE_TS < config.PRUNE_INTERVAL_SEC:
        return
    try:
        bars_removed = await asyncio.to_thread(
            storage.prune_old_bars, config.ROLLING_WINDOW_DAYS,
        )
        bars_15m_removed = await asyncio.to_thread(
            storage.prune_old_bars_15m, config.ROLLING_WINDOW_DAYS,
        )
        alerts_removed = await asyncio.to_thread(
            storage.prune_old_alerts, config.PRUNE_ALERTS_DAYS,
        )
        log.info(
            "Prune: removed %d 1h-bars, %d 15m-bars >%dd old, %d alerts >%dd old",
            bars_removed, bars_15m_removed, config.ROLLING_WINDOW_DAYS,
            alerts_removed, config.PRUNE_ALERTS_DAYS,
        )
    except Exception as e:
        log.warning("Prune failed (will retry next interval): %s", e)
    _LAST_PRUNE_TS = now_ts


# ============================================================================
# Hourly report — heartbeat + watchlist summary to Telegram
# ============================================================================

def _md_escape(s: str) -> str:
    """Mirror radar.telegram._md_escape so the report renders cleanly."""
    if not s:
        return ""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


def _format_hourly_report() -> str:
    """Build the Markdown body for the hourly Telegram report."""
    now = time.time()
    if _LAST_SCAN_TS > 0:
        scan_age_min = (now - _LAST_SCAN_TS) / 60.0
        scan_age = f"{scan_age_min:.1f}m ago"
    else:
        scan_age = "no scan yet"

    try:
        universe_n = len(universe.get_leveraged_universe())
    except Exception:
        universe_n = 0

    try:
        watchlist = storage.list_active_watchlist()
    except Exception:
        watchlist = []

    lines: list[str] = []
    lines.append(f"📊 *RADAR HOURLY* · last scan {scan_age}")
    lines.append(f"Universe: {universe_n} tickers · Watchlist: {len(watchlist)}")
    lines.append("")

    if watchlist:
        lines.append("*Active watchlist:*")
        for entry in watchlist[:config.HOURLY_REPORT_MAX_WATCHLIST_LINES]:
            ticker = _md_escape(str(entry.get("ticker") or "?"))
            direction = (entry.get("direction_bias") or "?").upper()
            level_label = ""
            if direction == "LONG" and entry.get("swing_high_reference") is not None:
                level_label = f" above ${entry['swing_high_reference']:.4f}"
            elif direction == "SHORT" and entry.get("swing_low_reference") is not None:
                level_label = f" below ${entry['swing_low_reference']:.4f}"
            hours_on = ""
            added_at = entry.get("added_at")
            if added_at:
                try:
                    dt = datetime.fromisoformat(str(added_at).replace("Z", ""))
                    age_h = (datetime.utcnow() - dt).total_seconds() / 3600
                    hours_on = f" · {age_h:.1f}h on list"
                except Exception:
                    pass
            lines.append(f"• {ticker} {direction}{level_label}{hours_on}")
        extra = len(watchlist) - config.HOURLY_REPORT_MAX_WATCHLIST_LINES
        if extra > 0:
            lines.append(f"…and {extra} more")
        lines.append("")
    else:
        lines.append("_Watchlist empty._")
        lines.append("")

    if _LAST_TOP_CANDIDATES:
        lines.append("*Recent top movers (no BOS yet):*")
        for ticker, score, pct in _LAST_TOP_CANDIDATES[:config.HOURLY_REPORT_MAX_TOP_CANDIDATES]:
            lines.append(f"• {_md_escape(ticker)} · score `{score:.1f}` · {pct:+.2f}%")
        lines.append("")

    return "\n".join(lines).rstrip()


def _send_hourly_report() -> bool:
    """Send the hourly report via the existing telegram main-chat path."""
    body = _format_hourly_report()
    try:
        return telegram._send_main(body, market_label="hourly_report")
    except Exception as e:
        log.warning("Hourly report send failed: %s", e)
        return False


# ============================================================================
# Tier 1 — discovery scan
# ============================================================================

async def run_discovery_cycle() -> None:
    """One Tier 1 cycle. Sync internals, called from the async loop."""
    storage.expire_stale_watchlist()

    markets = universe.get_leveraged_universe()
    if not markets:
        log.info("Tier 1: empty universe — skipping cycle")
        return

    histories: dict[str, dict[str, list[float]]] = {}
    for m in markets:
        try:
            storage.upsert_market_state(m)
            _record_market_bar(m)
            histories[m.ticker] = build_history(m.ticker)
        except Exception as e:
            log.warning("Tier 1 snapshot failed for %s: %s", m.ticker, e)

    btc_rets = btc_history()
    candidates = ranker.top_n_movers(markets, histories=histories)
    log.info("Tier 1: %d candidates: %s",
             len(candidates), [m.ticker for m, _ in candidates])

    # Capture top candidates for the hourly report (heartbeat + transparency).
    global _LAST_TOP_CANDIDATES, _LAST_SCAN_TS
    _LAST_TOP_CANDIDATES = [
        (m.ticker, float(s), float(getattr(m, "pct_24h", 0.0) or 0.0))
        for m, s in candidates[:config.HOURLY_REPORT_MAX_TOP_CANDIDATES]
    ]

    for market, score in candidates:
        try:
            # ---- Load bar history (1h + 15m) ----
            bar_history = storage.recent_bars(
                market.ticker, hours=config.BOS_BAR_HISTORY_HOURS,
            )
            bar_history_15m = storage.recent_bars_15m(
                market.ticker, bars=config.BOS_15M_HISTORY_BARS,
            )

            # ---- Pre-check BOS using both timeframes ----
            has_bos = False
            if bar_history:
                try:
                    has_bos, _bk_lvl, _bk_dir, _bk_type = ranker.has_breakout_structure(
                        market, bar_history, market.price,
                        history_15m=bar_history_15m or None,
                    )
                except Exception:
                    has_bos = False

            # ---- Cost gate (v3): no LLM unless BOS confirmed ----
            # Non-BOS candidates always DROP no_structure_break. The score-
            # based watchlist routing still happens later in suppression Rule 0
            # for candidates that have BOS but classifier disagrees on direction.
            if not has_bos:
                alert = build_alert(market, None, histories.get(market.ticker, {}),
                                    btc_rets=btc_rets, score=score)
                storage.record_alert(alert, decision="DROP",
                                     reason="no_structure_break",
                                     classifier=None)
                continue

            # ---- Order-book sentiment (advisory, fed to LLM + alert body) ----
            book_sentiment = "neutral"
            book_ratio: float | None = None
            try:
                mid = lighter.market_id_for(market.ticker)
                if mid is not None:
                    depth = lighter.fetch_order_book_depth(mid, levels=10)
                    if depth is not None:
                        bid_usd, ask_usd = depth
                        book_ratio, book_sentiment = lighter.imbalance_sentiment(
                            bid_usd, ask_usd,
                        )
            except Exception as e:
                log.debug("orderbook fetch failed for %s: %s", market.ticker, e)

            # ---- LLM classifier (Stage 1) — enrichment only, never suppresses ----
            news = catalysts.fetch_for_market(market, lookback_hours=config.NEWS_LOOKBACK_HOURS)
            result = classifier.classify(market, news)
            # Note: classifier.classify no longer short-circuits on empty news
            # in v3. It may still return None on Gemini errors / fabricated
            # quote rejection. When None, the alert still fires with a
            # placeholder result so the LLM never suppresses.
            if result is None:
                log.info("Tier 1 %s: classifier returned None — proceeding with placeholder",
                         market.ticker)
                from .classifier import ClassifierResult
                result = ClassifierResult(
                    catalyst_type="none", direction="neutral", confidence=0.0,
                    summary="LLM unavailable — structural break only.",
                    evidence_quotes=[], is_actionable=True,
                )
            # alert_priority="SUPPRESS" is honored only as a flag in the alert
            # body — does NOT block the alert per the v3 no-suppression policy.

            alert = build_alert(market, result, histories.get(market.ticker, {}),
                                btc_rets=btc_rets, score=score)
            decision, reason, metadata = suppression.evaluate(
                market, alert, bar_history, history_15m=bar_history_15m or None,
            )
            metadata["book_sentiment"] = book_sentiment
            if book_ratio is not None:
                metadata["book_ratio"] = book_ratio
            storage.record_alert(alert, decision=decision, reason=reason,
                                 classifier=result)

            if decision == "WATCHLIST":
                metadata = {**metadata, "score": score}
                telegram.send_watchlist_notification(market, result, metadata)
                log.info(
                    "Tier 1 → WATCHLIST %s (high=%s, low=%s)",
                    market.ticker,
                    metadata.get("swing_high_reference"),
                    metadata.get("swing_low_reference"),
                )
            elif decision == "EMIT":
                # Determine direction for the trade plan — prefer the
                # structural direction (BOS-derived) when classifier disagrees
                # or is neutral. This is consistent with the no-suppression
                # policy: structure wins.
                plan_direction = (
                    metadata.get("structure_direction")
                    or str(getattr(result, "direction", "") or "")
                )
                plan = trade_plan.compute_plan(
                    market, bar_history, metadata, direction=plan_direction,
                )
                # ---- Volume profile confirmation badge (advisory) ----
                try:
                    poc = ranker.compute_volume_profile_poc(bar_history)
                except Exception:
                    poc = None
                metadata["vpoc_price"] = poc
                metadata["vpoc_near_breakout"] = ranker.is_breakout_near_poc(
                    metadata.get("breakout_level"), poc,
                )
                # ---- Stage 2 enrichment — advisory only ----
                pred = None
                if config.STAGE2_ENABLED and plan is not None:
                    btc_hist = storage.recent_bars(
                        "BTC", hours=config.STAGE2_BAR_HISTORY_HOURS,
                    )
                    try:
                        pred = predictor.analyze(
                            market, result, plan, metadata,
                            bar_history, btc_hist, news, prior_alerts=[],
                        )
                    except Exception as e:
                        log.warning("Stage 2 crashed for %s: %s", market.ticker, e)
                        pred = None
                # Stage 2 verdicts (DROP / DOWNGRADE_TO_WATCHLIST) are now
                # ADVISORY — they appear in the alert body as warnings but
                # never block the alert. v3 policy: BOS is the only suppressor.
                metadata = {**metadata, "predictor_result": pred}
                telegram.send_bos_alert(
                    market, result, metadata,
                    source="tier1_immediate", plan=plan,
                )
                log.info(
                    "Tier 1 → EMIT %s %s at break of %s%s%s",
                    market.ticker,
                    getattr(result, "catalyst_type", "?"),
                    metadata.get("breakout_level"),
                    f" plan(stop={plan.stop:.4f} tp1={plan.tp1:.4f} tp2={plan.tp2:.4f})"
                    if plan is not None else " (no plan)",
                    f" [stage2={pred.verdict}]"
                    if pred is not None and pred.verdict != "ALERT_NOW" else "",
                )
            else:
                log.info("Tier 1 → DROP %s: %s", market.ticker, reason)
        except Exception as e:
            log.exception("Tier 1 error on %s: %s", market.ticker, e)

    _LAST_SCAN_TS = time.time()


# ============================================================================
# Tier 2 — trigger watch
# ============================================================================

async def run_trigger_poll() -> None:
    """One Tier 2 poll over the active watchlist."""
    active = storage.list_active_watchlist()
    if not active:
        return
    log.info("Tier 2: polling %d watchlist tickers", len(active))

    for entry in active[:config.TRIGGER_POLL_MAX_TICKERS]:
        ticker = entry.get("ticker", "?")
        try:
            live_market = universe.get_market_snapshot(ticker)
            if live_market is None:
                log.warning("Tier 2 %s: no live snapshot (delisted?) — skipping", ticker)
                continue

            recent = storage.recent_bars(ticker, hours=2)
            if not recent:
                log.debug("Tier 2 %s: no recent bars — skipping", ticker)
                continue
            current_bar = recent[-1]
            current_bar_range = float((current_bar.high or 0.0) - (current_bar.low or 0.0))

            broke, direction, breakout_level = ranker.check_breakout_against_stored_references(
                current_price=float(live_market.price or 0.0),
                current_bar_range=current_bar_range,
                swing_high_reference=entry.get("swing_high_reference"),
                swing_low_reference=entry.get("swing_low_reference"),
                median_bar_range=float(entry.get("median_bar_range") or 0.0),
                direction_bias=str(entry.get("direction_bias") or ""),
            )

            storage.update_watchlist_poll(ticker, float(live_market.price or 0.0))

            if not broke:
                continue

            # Promote: rehydrate the classifier result, ship the alert,
            # remove from watchlist, record EMIT.
            from .classifier import ClassifierResult
            try:
                result = ClassifierResult.model_validate_json(entry.get("classifier_json") or "{}")
            except Exception as e:
                log.warning("Tier 2 %s: classifier json invalid (%s) — using stub", ticker, e)
                result = ClassifierResult(
                    catalyst_type="none", direction=direction or "long",
                    confidence=0.5, summary=entry.get("catalyst_summary") or "",
                )

            try:
                added_at = datetime.fromisoformat(entry["added_at"])
                hours_on_watchlist = round(
                    (datetime.utcnow() - added_at).total_seconds() / 3600, 1
                )
            except Exception:
                hours_on_watchlist = 0.0

            metadata = {
                "breakout_level": breakout_level,
                "swing_high_reference": entry.get("swing_high_reference"),
                "swing_low_reference": entry.get("swing_low_reference"),
                "median_bar_range": entry.get("median_bar_range"),
                "promoted_from_watchlist": True,
                "hours_on_watchlist": hours_on_watchlist,
            }

            # Pull a wider history window so the trade plan can find a "next
            # prior swing" target; `recent` (2h) is too narrow.
            plan_history = storage.recent_bars(
                ticker, hours=config.BOS_BAR_HISTORY_HOURS,
            )
            plan = trade_plan.compute_plan(
                live_market, plan_history, metadata,
                direction=str(getattr(result, "direction", "")
                              or entry.get("direction_bias") or ""),
            )

            telegram.send_bos_alert(
                live_market, result, metadata,
                source="tier2_promoted", plan=plan,
            )

            promoted_alert = Alert(
                ticker=ticker,
                asset_class=str(entry.get("asset_class") or live_market.asset_class),
                score=float(entry.get("score") or 0.0),
                alpha_z=0.0,
                r_alpha_pct=0.0,
                classifier_result=result,
            )
            storage.record_alert(
                promoted_alert,
                decision="EMIT",
                reason=f"watchlist_promoted_{hours_on_watchlist}h",
                classifier=result,
            )
            storage.remove_from_watchlist(ticker)
            log.info(
                "Tier 2 → PROMOTED %s at %.6f (broke %s, on watchlist %.1fh)",
                ticker, float(live_market.price or 0.0),
                breakout_level, hours_on_watchlist,
            )
        except Exception as e:
            log.exception("Tier 2 error on %s: %s", ticker, e)


# ============================================================================
# loops + entrypoint
# ============================================================================

_RUNNING = True


async def tier1_discovery_scan() -> None:
    while _RUNNING:
        started = time.time()
        try:
            await run_discovery_cycle()
        except Exception as e:
            log.exception("Tier 1 cycle blew up: %s", e)
        elapsed = time.time() - started
        try:
            await _maybe_prune(time.time())
        except Exception as e:
            log.warning("Prune scheduler hiccup: %s", e)
        sleep_for = max(1, config.FAST_CADENCE_SEC - int(elapsed))
        log.info("Tier 1 done in %.1fs; sleeping %ds", elapsed, sleep_for)
        await asyncio.sleep(sleep_for)


async def tier2_trigger_watch() -> None:
    while _RUNNING:
        try:
            await run_trigger_poll()
        except Exception as e:
            log.exception("Tier 2 poll blew up: %s", e)
        await asyncio.sleep(config.TRIGGER_POLL_INTERVAL_SEC)


async def tier3_hourly_report() -> None:
    """Push an hourly heartbeat + watchlist summary to Telegram.

    Aligned to ``HOURLY_REPORT_OFFSET_SEC`` past the top of each UTC hour
    (default :05) so the report lands after the just-closed 1h bar has been
    recorded and scored by Tier 1 — and so it doesn't drift based on boot time.
    Doubles as the engine-alive signal: if these stop arriving, something is
    wrong on the host. The Telegram send is offloaded to a worker thread to
    avoid blocking Tier 1/2.
    """
    while _RUNNING:
        now = time.time()
        next_top = (int(now) // 3600 + 1) * 3600 + config.HOURLY_REPORT_OFFSET_SEC
        await asyncio.sleep(max(1.0, next_top - now))
        if not _RUNNING:
            break
        try:
            await asyncio.to_thread(_send_hourly_report)
        except Exception as e:
            log.warning("Hourly report cycle blew up: %s", e)


async def main_async() -> None:
    storage.init_db()
    log.info("catalyst-radar starting; tier1=%ds, tier2=%ds",
             config.FAST_CADENCE_SEC, config.TRIGGER_POLL_INTERVAL_SEC)

    if config.BACKFILL_ENABLED:
        try:
            await _backfill_bars_for_universe()
            await _backfill_bars_15m_for_universe()
        except asyncio.CancelledError:
            log.info("Backfill cancelled — proceeding to shutdown")
            return
        except Exception as e:
            log.exception("Backfill blew up — continuing without it: %s", e)

    loops = [tier1_discovery_scan(), tier2_trigger_watch()]
    if config.HOURLY_REPORT_ENABLED:
        loops.append(tier3_hourly_report())
    await asyncio.gather(*loops)


def _graceful_shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
    global _RUNNING
    log.info("received signal %d — shutting down after current cycle", signum)
    _RUNNING = False


def main() -> None:
    load_dotenv()
    _setup_logging()
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    log.info("radar stopped")


if __name__ == "__main__":
    main()
