"""Historical replay harness.

Walks the engine through historical market data hour by hour without ever
calling Lighter, RSS feeds, EDGAR, etc. Used to validate the ranker, BTC-beta
gate, and suppression chain against past moves you already know about.

Inputs
------
1. A CSV of hourly bars with this header:

     ts,ticker,asset_class,max_leverage,price,volume_24h_usd,oi_usd,funding_1h,pct_24h,pct_1h

   `ts` may be either a unix integer or an ISO-8601 string ("2024-01-15T10:00:00Z").

2. (Optional) a JSON news archive — flat list:

     [
       {"ticker": "BTC", "source": "...", "title": "...", "body": "...",
        "url": "...", "published": "2024-01-15T10:30:00Z"}
     ]

What it does
------------
For each unique hourly timestamp in the CSV (oldest → newest):
  1. Sets `storage._now` to that timestamp (virtual clock).
  2. Loads bars at that ts into a *separate* SQLite DB (default `data/replay.db`)
     so production state is untouched.
  3. Runs ranker → beta → suppression with histories built from prior bars.
  4. If a news archive is provided, runs the classifier against the local items
     for the ticker; otherwise classification is skipped (Anthropic API not called).
  5. Logs every EMIT/DROP decision and records it in the replay DB.

Run as a module:

    python -m radar.replay --bars data/sample_bars.csv \\
                           --news data/sample_news_archive.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import beta, classifier, config, ranker, storage, suppression
from .catalysts import NewsItem
from .suppression import Alert
from .universe import Market

log = logging.getLogger("radar.replay")


# ============ parsing helpers ============

def _parse_ts(value: Any) -> int:
    if value is None or value == "":
        raise ValueError("missing ts")
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError as e:
        raise ValueError(f"unparseable ts: {value!r}") from e


def _floor_hour(ts: int) -> int:
    return ts - (ts % 3600)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ============ loaders ============

def load_bars(path: str | Path) -> dict[int, list[Market]]:
    """Returns dict mapping floored-hourly ts → list of Market snapshots at that ts."""
    out: dict[int, list[Market]] = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        required = {"ts", "ticker", "asset_class", "price"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"bars CSV missing columns: {sorted(missing)}")
        for row in reader:
            ts = _floor_hour(_parse_ts(row["ts"]))
            ticker = row["ticker"].strip().upper()
            asset_class = row["asset_class"].strip()
            if asset_class not in config.SYMBOL_TO_CLASS.values():
                log.warning("row %s: unknown asset_class %r — skipping", ticker, asset_class)
                continue
            m = Market(
                ticker=ticker,
                asset_class=asset_class,
                market_id=ticker,
                max_leverage=_safe_float(row.get("max_leverage"), 1.0) or 1.0,
                price=_safe_float(row.get("price")),
                volume_24h_usd=_safe_float(row.get("volume_24h_usd")),
                oi_usd=_safe_float(row.get("oi_usd")),
                funding_1h=_safe_float(row.get("funding_1h")),
                pct_24h=_safe_float(row.get("pct_24h")),
                pct_1h=_safe_float(row.get("pct_1h")),
            )
            out[ts].append(m)
    return dict(out)


def load_news_archive(path: str | Path) -> dict[str, list[NewsItem]]:
    """Returns dict mapping ticker → list of NewsItem sorted by `published` ascending."""
    if path is None:
        return {}
    with open(path) as f:
        raw = json.load(f)
    by_ticker: dict[str, list[NewsItem]] = defaultdict(list)
    for entry in raw:
        ticker = (entry.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        published = entry.get("published")
        if isinstance(published, str):
            try:
                published = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
            except ValueError:
                published = 0
        elif isinstance(published, (int, float)):
            published = int(published)
        else:
            published = 0
        title = entry.get("title", "")
        url = entry.get("url", "") or f"replay://{ticker}/{hashlib.sha1(title.encode()).hexdigest()[:8]}"
        by_ticker[ticker].append(NewsItem(
            ticker=ticker,
            source=entry.get("source", "archive"),
            title=title,
            body=entry.get("body", "") or "",
            url=url,
            published=published,
        ))
    for items in by_ticker.values():
        items.sort(key=lambda n: n.published)
    return dict(by_ticker)


# ============ catalyst stub ============

def make_replay_fetcher(
    archive: dict[str, list[NewsItem]],
    now_fn: Callable[[], int],
):
    """Build a fetch_for_market replacement that reads from the local archive."""
    def fetch(market: Market, lookback_hours: int = config.NEWS_LOOKBACK_HOURS) -> list[NewsItem]:
        items = archive.get(market.ticker, [])
        if not items:
            return []
        now = now_fn()
        cutoff = now - lookback_hours * 3600
        filtered = [n for n in items if cutoff <= n.published <= now]
        # newest first, capped
        filtered.sort(key=lambda n: n.published, reverse=True)
        return filtered[: config.NEWS_MAX_ITEMS]
    return fetch


# ============ default emit handler ============

def _default_emit(alert: Alert, classification: Any | None) -> None:
    when = datetime.fromtimestamp(storage._now(), tz=timezone.utc).isoformat()
    summary = ""
    if classification is not None:
        summary = (
            f" | {getattr(classification, 'catalyst_type', '?')}"
            f"/{getattr(classification, 'direction', '?')}"
            f" conf={getattr(classification, 'confidence', 0.0):.2f}"
        )
    log.info(
        "%s  EMIT  %s (%s)  score=%.2f  α-z=%s  r_α=%.2f%%%s",
        when, alert.ticker, alert.asset_class, alert.score,
        f"{alert.alpha_z:.2f}" if alert.alpha_z != float("inf") else "inf",
        alert.r_alpha_pct, summary,
    )


# ============ history (built from the replay DB) ============

def _history_for(ticker: str, db_path: str) -> dict[str, list[float]]:
    rows = storage.recent_bars(ticker, hours=config.ROLLING_WINDOW_DAYS * 24, db_path=db_path)
    if not rows:
        return {}
    closes = [r["close"] for r in rows if r["close"] is not None]
    rets: list[float] = []
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


def _btc_returns(db_path: str) -> list[float]:
    return _history_for("BTC", db_path).get("ret_1h", [])


# ============ summary report ============

def summarize(db_path: str) -> dict[str, Any]:
    """Compute a per-ticker / per-rule breakdown from the replay DB."""
    emitted = storage.execute(
        "SELECT ticker, asset_class, COUNT(*) AS n "
        "FROM alerts WHERE decision = 'EMIT' "
        "GROUP BY ticker, asset_class ORDER BY n DESC LIMIT 25",
        db_path=db_path,
    )
    drop_reasons = storage.execute(
        "SELECT CASE "
        "  WHEN instr(reason, ':') > 0 THEN substr(reason, 1, instr(reason, ':') - 1) "
        "  ELSE reason END AS rule, "
        "COUNT(*) AS n "
        "FROM alerts WHERE decision = 'DROP' "
        "GROUP BY rule ORDER BY n DESC",
        db_path=db_path,
    )
    catalysts = storage.execute(
        "SELECT catalyst_type, direction, COUNT(*) AS n "
        "FROM alerts WHERE decision = 'EMIT' AND catalyst_type IS NOT NULL "
        "GROUP BY catalyst_type, direction ORDER BY n DESC LIMIT 25",
        db_path=db_path,
    )
    return {
        "emitted_by_ticker": [
            {"ticker": r["ticker"], "asset_class": r["asset_class"], "n": r["n"]}
            for r in emitted
        ],
        "drops_by_rule": [
            {"rule": r["rule"], "n": r["n"]} for r in drop_reasons
        ],
        "catalysts": [
            {"type": r["catalyst_type"], "direction": r["direction"], "n": r["n"]}
            for r in catalysts
        ],
    }


def _print_summary(counts: dict[str, int], summary: dict[str, Any]) -> None:
    print()
    print("=== Replay Summary ===")
    print(f"  cycles  = {counts['cycles']}")
    print(f"  emitted = {counts['emitted']}")
    print(f"  dropped = {counts['dropped']}")

    if summary["emitted_by_ticker"]:
        print("\n  Emitted by ticker:")
        for row in summary["emitted_by_ticker"]:
            print(f"    {row['ticker']:10s} {row['asset_class']:12s} {row['n']:>4d}")

    if summary["drops_by_rule"]:
        print("\n  Drops by rule:")
        for row in summary["drops_by_rule"]:
            print(f"    {row['rule']:20s} {row['n']:>4d}")

    if summary["catalysts"]:
        print("\n  Catalyst types (EMIT only):")
        for row in summary["catalysts"]:
            print(f"    {row['type']:18s} {row['direction']:8s} {row['n']:>4d}")
    print()


# ============ public API ============

def replay(
    bars_csv: str | Path,
    news_json: str | Path | None = None,
    db_path: str = "data/replay.db",
    classify: bool = True,
    emit_fn: Callable[[Alert, Any], None] | None = None,
    print_summary: bool = False,
) -> dict[str, int]:
    """Run a full historical replay. Returns counts: cycles/emitted/dropped."""
    bars_by_ts = load_bars(bars_csv)
    archive = load_news_archive(news_json) if news_json else {}
    emit_fn = emit_fn or _default_emit

    # Fresh replay DB so we never collide with production.
    if os.path.exists(db_path):
        os.remove(db_path)
    storage.init_db(db_path)

    # Make sub-modules that read config.DB_PATH (suppression, etc.) hit the
    # replay DB by default.
    original_db_path = config.DB_PATH
    config.DB_PATH = db_path

    timestamps = sorted(bars_by_ts.keys())
    if not timestamps:
        log.warning("replay: no bars to walk through")
        config.DB_PATH = original_db_path
        return {"cycles": 0, "emitted": 0, "dropped": 0}

    # virtual clock — closure captures the loop variable through `_state`
    _state = {"now": timestamps[0]}
    storage.set_clock(lambda: _state["now"])

    counts = {"cycles": 0, "emitted": 0, "dropped": 0}

    try:
        for ts in timestamps:
            _state["now"] = ts
            counts["cycles"] += 1
            markets = bars_by_ts[ts]

            for m in markets:
                storage.upsert_market_state(m, db_path=db_path)
                storage.insert_bar(
                    ticker=m.ticker, ts=ts,
                    close=m.price, volume=m.volume_24h_usd,
                    oi=m.oi_usd, funding=m.funding_1h,
                    db_path=db_path,
                )

            histories = {m.ticker: _history_for(m.ticker, db_path) for m in markets}
            btc_rets = _btc_returns(db_path)

            candidates = ranker.top_n_movers(markets, histories=histories)
            for market, score in candidates:
                hist = dict(histories.get(market.ticker, {}))
                if market.asset_class.startswith("crypto") and btc_rets:
                    hist["btc_ret_1h"] = btc_rets
                alpha_z, r_alpha_pct = beta.compute_alpha_z(market, hist)

                fetch = make_replay_fetcher(archive, lambda t=ts: t)
                news = fetch(market)
                cls = classifier.classify(market, news) if classify else None

                alert = Alert(
                    ticker=market.ticker,
                    asset_class=market.asset_class,
                    score=score,
                    alpha_z=alpha_z,
                    r_alpha_pct=r_alpha_pct,
                )
                decision, reason = suppression.evaluate(alert)
                storage.record_alert(alert, decision=decision, reason=reason,
                                     classifier=cls, db_path=db_path)
                if decision == "EMIT":
                    counts["emitted"] += 1
                    try:
                        emit_fn(alert, cls)
                    except Exception as e:
                        log.warning("emit handler raised: %s", e)
                else:
                    counts["dropped"] += 1
                    log.debug("DROP %s: %s", market.ticker, reason)
    finally:
        storage.set_clock(None)
        config.DB_PATH = original_db_path

    log.info("replay done: %s", counts)
    if print_summary:
        try:
            _print_summary(counts, summarize(db_path))
        except Exception as e:
            log.warning("summary failed: %s", e)
    return counts


# ============ CLI ============

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m radar.replay")
    p.add_argument("--bars", required=True, help="path to bars CSV")
    p.add_argument("--news", default=None, help="path to news archive JSON (optional)")
    p.add_argument("--db", default="data/replay.db", help="replay SQLite path (will be wiped)")
    p.add_argument("--no-classify", action="store_true",
                   help="skip Claude classifier calls (saves API spend)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_argparser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    counts = replay(
        bars_csv=args.bars,
        news_json=args.news,
        db_path=args.db,
        classify=not args.no_classify,
        print_summary=True,
    )
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
