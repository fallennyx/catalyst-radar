"""Export bars from a snapshot DB to replay's CSV format.

Usage:
    python scripts/replay_export.py \
        --db data/radar_snapshot.db \
        --out data/replay_may10_11.csv \
        --start 2026-05-04 --end 2026-05-11

`--start` / `--end` filter the ts range. Include warmup hours BEFORE the period
you actually care about (BOS needs ≥132h of prior bars to fire). For May 10-11
analysis, --start 2026-05-04 gives a comfortable cushion.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


REPLAY_FIELDS = [
    "ts", "ticker", "asset_class", "max_leverage",
    "open", "high", "low", "price",
    "volume_24h_usd", "oi_usd", "funding_1h",
    "pct_24h", "pct_1h",
]


def _to_iso(unix_ts: int) -> str:
    return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, help="snapshot SQLite path")
    p.add_argument("--out", required=True, help="output CSV path")
    p.add_argument("--start", required=True, help="YYYY-MM-DD UTC (inclusive) — warmup begins here")
    p.add_argument("--end", required=True, help="YYYY-MM-DD UTC (inclusive)")
    args = p.parse_args(argv)

    start_ts = _parse_date(args.start)
    end_ts = _parse_date(args.end) + 86399  # end of day inclusive

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    cx = sqlite3.connect(args.db)
    cx.row_factory = sqlite3.Row

    # markets_state has asset_class + max_leverage per ticker (latest snapshot).
    # bars_1h has OHLCV. Inner join filters out bars whose ticker has no
    # markets_state row (rare — implies a delisted ticker).
    rows = cx.execute(
        """
        SELECT
            b.ts AS ts,
            b.ticker AS ticker,
            ms.asset_class AS asset_class,
            COALESCE(ms.max_leverage, 1.0) AS max_leverage,
            b.open AS open,
            b.high AS high,
            b.low AS low,
            b.close AS price,
            b.volume AS volume_24h_usd,
            COALESCE(b.oi, 0) AS oi_usd,
            COALESCE(b.funding, 0) AS funding_1h
        FROM bars_1h b
        JOIN markets_state ms ON ms.ticker = b.ticker
        WHERE b.ts BETWEEN ? AND ?
        ORDER BY b.ts ASC, b.ticker ASC
        """,
        (start_ts, end_ts),
    ).fetchall()

    if not rows:
        print(f"No bars in [{args.start}, {args.end}] — check the DB range.", file=sys.stderr)
        return 2

    # We need pct_24h / pct_1h per row. The replay's ranker doesn't strictly
    # need them (it builds returns from the bar history), but the CSV format
    # carries them so we compute on the fly to avoid silent NaNs.
    prior_close: dict[str, list[tuple[int, float]]] = {}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPLAY_FIELDS)
        w.writeheader()
        for r in rows:
            ticker = r["ticker"]
            ts = int(r["ts"])
            price = float(r["price"] or 0.0)

            hist = prior_close.setdefault(ticker, [])
            pct_1h = 0.0
            pct_24h = 0.0
            if hist:
                prev_ts, prev_close = hist[-1]
                if prev_close:
                    pct_1h = (price - prev_close) / prev_close * 100.0
                # 24h ago: walk back through hist
                target = ts - 24 * 3600
                for hts, hc in reversed(hist):
                    if hts <= target and hc:
                        pct_24h = (price - hc) / hc * 100.0
                        break
            hist.append((ts, price))
            # keep a bounded tail for memory (we only need ~25 entries back)
            if len(hist) > 30:
                del hist[:-30]

            w.writerow({
                "ts": _to_iso(ts),
                "ticker": ticker,
                "asset_class": r["asset_class"],
                "max_leverage": r["max_leverage"],
                "open": r["open"] if r["open"] is not None else "",
                "high": r["high"] if r["high"] is not None else "",
                "low": r["low"] if r["low"] is not None else "",
                "price": price,
                "volume_24h_usd": r["volume_24h_usd"] if r["volume_24h_usd"] is not None else "",
                "oi_usd": r["oi_usd"],
                "funding_1h": r["funding_1h"],
                "pct_24h": f"{pct_24h:.4f}",
                "pct_1h": f"{pct_1h:.4f}",
            })

    n_rows = len(rows)
    n_tickers = len({r["ticker"] for r in rows})
    print(f"Wrote {n_rows} rows ({n_tickers} tickers) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
