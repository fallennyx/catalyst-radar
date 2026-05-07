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
import os
import signal
import sys
import time
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from . import (
    beta, catalysts, classifier, config, ranker, storage,
    suppression, telegram, universe,
)
from .suppression import Alert

log = logging.getLogger("radar")


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
    ts = int(time.time()) // 3600 * 3600
    storage.insert_bar(
        ticker=market.ticker,
        ts=ts,
        close=market.price,
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

    for market, score in candidates:
        try:
            news = catalysts.fetch_for_market(market, lookback_hours=config.NEWS_LOOKBACK_HOURS)
            result = classifier.classify(market, news)
            if result is None:
                log.info("Tier 1 %s: classifier returned None — DROP", market.ticker)
                continue
            if getattr(result, "alert_priority", "NORMAL") == "SUPPRESS":
                alert = build_alert(market, result, histories.get(market.ticker, {}),
                                    btc_rets=btc_rets, score=score)
                storage.record_alert(alert, decision="DROP",
                                     reason="classifier_suppressed",
                                     classifier=result)
                continue

            alert = build_alert(market, result, histories.get(market.ticker, {}),
                                btc_rets=btc_rets, score=score)
            bar_history = storage.recent_bars(
                market.ticker,
                hours=config.SWING_LOOKBACK_HOURS * 2,
            )
            decision, reason, metadata = suppression.evaluate(market, alert, bar_history)
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
                telegram.send_bos_alert(market, result, metadata, source="tier1_immediate")
                log.info(
                    "Tier 1 → EMIT %s %s at break of %s",
                    market.ticker,
                    getattr(result, "catalyst_type", "?"),
                    metadata.get("breakout_level"),
                )
            else:
                log.info("Tier 1 → DROP %s: %s", market.ticker, reason)
        except Exception as e:
            log.exception("Tier 1 error on %s: %s", market.ticker, e)


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

            telegram.send_bos_alert(live_market, result, metadata, source="tier2_promoted")

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


async def main_async() -> None:
    storage.init_db()
    log.info("catalyst-radar starting; tier1=%ds, tier2=%ds",
             config.FAST_CADENCE_SEC, config.TRIGGER_POLL_INTERVAL_SEC)
    await asyncio.gather(
        tier1_discovery_scan(),
        tier2_trigger_watch(),
    )


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
