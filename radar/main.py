"""Catalyst Radar orchestrator loop.

universe → ranker → catalysts → classifier → suppression → telegram → sleep
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any

from dotenv import load_dotenv

from . import beta, catalysts, classifier, config, ranker, storage, suppression, telegram, universe
from .suppression import Alert

log = logging.getLogger("radar")


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _build_history(ticker: str) -> dict[str, list[float]]:
    """Pull rolling 1h bars and assemble the arrays the ranker/beta want."""
    rows = storage.recent_bars(ticker, hours=config.ROLLING_WINDOW_DAYS * 24)
    if not rows:
        return {}
    closes = [r["close"] for r in rows if r["close"] is not None]
    rets = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev)
    return {
        "ret_1h": rets,
        "vol_1h": [r["volume"] for r in rows if r["volume"] is not None],
        "oi_1h": [r["oi"] for r in rows if r["oi"] is not None],
        "funding": [r["funding"] for r in rows if r["funding"] is not None],
    }


def _btc_history() -> list[float]:
    rows = storage.recent_bars("BTC", hours=config.ROLLING_WINDOW_DAYS * 24)
    closes = [r["close"] for r in rows if r["close"] is not None]
    rets = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev)
    return rets


def _record_market_bar(market: Any) -> None:
    """Snapshot the current market into bars_1h, bucketed to the hour."""
    ts = int(time.time()) // 3600 * 3600
    storage.insert_bar(
        ticker=market.ticker,
        ts=ts,
        close=market.price,
        volume=market.volume_24h_usd,
        oi=market.oi_usd,
        funding=market.funding_1h,
    )


def cycle() -> None:
    markets = universe.get_leveraged_universe()
    if not markets:
        log.info("cycle: empty universe — skipping")
        return

    histories: dict[str, dict[str, list[float]]] = {}
    for m in markets:
        try:
            storage.upsert_market_state(m)
            _record_market_bar(m)
            histories[m.ticker] = _build_history(m.ticker)
        except Exception as e:
            log.warning("snapshot failed for %s: %s", m.ticker, e)

    btc_rets = _btc_history()

    candidates = ranker.top_n_movers(markets, histories=histories)
    log.info("cycle: %d top movers", len(candidates))

    for market, score in candidates:
        try:
            hist = dict(histories.get(market.ticker, {}))
            if market.asset_class.startswith("crypto") and btc_rets:
                hist["btc_ret_1h"] = btc_rets
            alpha_z, r_alpha_pct = beta.compute_alpha_z(market, hist)

            news_items = catalysts.fetch_for_market(market)
            classification = classifier.classify(market, news_items)

            alert = Alert(
                ticker=market.ticker,
                asset_class=market.asset_class,
                score=score,
                alpha_z=alpha_z,
                r_alpha_pct=r_alpha_pct,
            )

            decision, reason = suppression.evaluate(alert)
            storage.record_alert(alert, decision=decision, reason=reason, classifier=classification)

            if decision == "EMIT":
                telegram.send_alert(alert, classification)
            else:
                log.info("DROP %s: %s", market.ticker, reason)
        except Exception as e:
            # Per-market try/except so one bad ticker can't kill the cycle.
            log.exception("cycle: ticker %s failed: %s", market.ticker, e)


# ---------- shutdown ----------

_RUNNING = True


def _graceful_shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
    global _RUNNING
    log.info("received signal %d — shutting down after current cycle", signum)
    _RUNNING = False


def main() -> None:
    load_dotenv()
    _setup_logging()
    storage.init_db()

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    log.info("catalyst-radar starting; cadence=%ds", config.FAST_CADENCE_SEC)

    while _RUNNING:
        started = time.time()
        try:
            cycle()
        except Exception as e:
            log.exception("cycle blew up: %s", e)
        elapsed = time.time() - started
        sleep_for = max(1, config.FAST_CADENCE_SEC - int(elapsed))
        log.info("cycle done in %.1fs; sleeping %ds", elapsed, sleep_for)
        for _ in range(sleep_for):
            if not _RUNNING:
                break
            time.sleep(1)

    log.info("radar stopped")


if __name__ == "__main__":
    main()
