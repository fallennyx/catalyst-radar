"""Walk-forward replay comparing SWING_LOOKBACK_4H_BARS = 15/20/25/30.

Loads bars from data/may4_full_bars.csv, walks forward per-ticker, calls
ranker.has_breakout_structure at each step with the 4h lookback monkey-patched.
Records distinct breakout events (with 4h cooldown) and writes a
side-by-side markdown table to data/lookback_4h_compare.md.

Run: python -m scripts.lookback_4h_compare
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar import config, ranker
from radar.storage import Bar
from radar.universe import Market

BARS_CSV = ROOT / "data" / "may4_full_bars.csv"
OUT_MD = ROOT / "data" / "lookback_4h_compare.md"

LOOKBACK_VARIANTS = [15, 20, 25, 30]
COOLDOWN_HOURS = 4  # don't re-record same-direction fires within this window
MIN_HISTORY_HOURS = 80  # ranker needs ample 1h history to synthesize 4h pivots


def _parse_iso(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _fmt_ts(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


def load_bars_by_ticker() -> tuple[dict[str, list[Bar]], dict[str, str]]:
    """Return (ticker -> sorted bars, ticker -> asset_class)."""
    bars: dict[str, list[Bar]] = defaultdict(list)
    asset_class: dict[str, str] = {}
    with open(BARS_CSV, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ticker = row["ticker"]
            try:
                bars[ticker].append(
                    Bar(
                        ticker=ticker,
                        ts=_parse_iso(row["ts"]),
                        open=float(row["open"] or 0) or None,
                        high=float(row["high"] or 0) or None,
                        low=float(row["low"] or 0) or None,
                        close=float(row["price"] or 0) or None,
                        volume=float(row["volume_24h_usd"] or 0) or None,
                    )
                )
                asset_class[ticker] = row["asset_class"]
            except (ValueError, KeyError):
                continue
    for t in bars:
        bars[t].sort(key=lambda b: b.ts)
    return bars, asset_class


def simulate_ticker(
    ticker: str,
    bars: list[Bar],
    asset_class_str: str,
) -> list[tuple[int, str, float, float, str]]:
    """Walk forward bar-by-bar. Return list of (ts, direction, price, level, structure_type)."""
    market = Market(
        ticker=ticker,
        asset_class=asset_class_str,
        max_leverage=10,
        price=0.0,
        volume_24h_usd=0.0,
        oi_usd=0.0,
        funding_1h=0.0,
        pct_24h=0.0,
        pct_1h=0.0,
    )
    fires: list[tuple[int, str, float, float, str]] = []
    last_fire_ts: dict[str, int] = {}
    for i in range(MIN_HISTORY_HOURS, len(bars)):
        history = bars[: i + 1]
        current = bars[i]
        if current.close is None:
            continue
        broke, direction, level, stype = ranker.has_breakout_structure(
            market, history, current_price=float(current.close)
        )
        if not broke or direction is None:
            continue
        # cooldown per direction
        last = last_fire_ts.get(direction, 0)
        if current.ts - last < COOLDOWN_HOURS * 3600:
            continue
        last_fire_ts[direction] = current.ts
        fires.append((current.ts, direction, float(current.close), float(level or 0.0), stype or "?"))
    return fires


def run_variant(
    lookback_4h_bars: int,
    bars_by_ticker: dict[str, list[Bar]],
    ac_by_ticker: dict[str, str],
) -> dict[str, list[tuple[int, str, float, float, str]]]:
    """Run the walk-forward simulation for one SWING_LOOKBACK_4H_BARS value."""
    config.SWING_LOOKBACK_4H_BARS = lookback_4h_bars
    results: dict[str, list[tuple[int, str, float, float, str]]] = {}
    for ticker, bars in bars_by_ticker.items():
        if len(bars) < MIN_HISTORY_HOURS + 10:
            continue
        fires = simulate_ticker(ticker, bars, ac_by_ticker[ticker])
        if fires:
            results[ticker] = fires
    return results


def _fire_key(ticker: str, fire: tuple[int, str, float, float, str]) -> tuple[str, int, str]:
    """A fire is uniquely identified by (ticker, hour-bucket, direction).

    Hour-bucket = ts rounded to the nearest 4h cooldown window so a fire that
    moves by ±1h between variants still matches up.
    """
    ts, direction, _price, _level, _stype = fire
    bucket = (ts // (COOLDOWN_HOURS * 3600)) * (COOLDOWN_HOURS * 3600)
    return (ticker, bucket, direction)


def write_markdown(
    variant_results: dict[int, dict[str, list[tuple[int, str, float, float, str]]]],
) -> None:
    lines: list[str] = []
    lines.append("# SWING_LOOKBACK_4H_BARS sweep — may4_full_bars.csv replay\n\n")
    lines.append(f"Walk-forward BOS simulation. Cooldown: {COOLDOWN_HOURS}h per direction. "
                 f"Min history before first eval: {MIN_HISTORY_HOURS}h. Only the 4h lookback "
                 "knob varies — 1h path uses defaults (lookback=24, validation=2, range=1.2×).\n\n")
    lines.append("**Reading guide**: Under current (4h-priority) code, a shorter 4h lookback "
                 "doesn't add many *new* alerts — it mostly re-tags events from `1h` to `4h` "
                 "(more structural conviction, tighter level for the trade plan). The "
                 "'Net new fires' column below isolates the genuine pickup.\n\n")

    # Summary table
    lines.append("## Summary\n\n")
    lines.append("| Lookback | Days | Total fires | 4h-tagged | 1h-tagged | "
                 "Net new vs LB=30 |\n")
    lines.append("|---|---|---|---|---|---|\n")
    # Build sets of unique fire keys per variant
    keys_per_variant: dict[int, set[tuple[str, int, str]]] = {}
    for lb in LOOKBACK_VARIANTS:
        keys_per_variant[lb] = set()
        for ticker, fires in variant_results[lb].items():
            for f in fires:
                keys_per_variant[lb].add(_fire_key(ticker, f))
    baseline = keys_per_variant[30]
    for lb in LOOKBACK_VARIANTS:
        results = variant_results[lb]
        total = sum(len(v) for v in results.values())
        n_4h = sum(1 for v in results.values() for f in v if f[4] == "4h")
        n_1h = sum(1 for v in results.values() for f in v if f[4] == "1h")
        net_new = len(keys_per_variant[lb] - baseline)
        lines.append(f"| {lb} | {lb * 4 / 24:.1f}d | {total} | {n_4h} | {n_1h} | "
                     f"+{net_new} |\n")
    lines.append("\n")

    # Per-ticker fire counts where things differed
    all_tickers = sorted({t for r in variant_results.values() for t in r})
    lines.append("## Per-ticker fire counts (only tickers where the knob mattered)\n\n")
    lines.append("| Ticker | LB=15 | LB=20 | LB=25 | LB=30 |\n")
    lines.append("|---|---|---|---|---|\n")
    for t in all_tickers:
        counts = [len(variant_results[lb].get(t, [])) for lb in LOOKBACK_VARIANTS]
        if len(set(counts)) > 1:
            lines.append(f"| {t} | {counts[0]} | {counts[1]} | {counts[2]} | {counts[3]} |\n")
    lines.append("\n")

    # === DIFFERENTIAL FIRE LOG: events firing under LB=15 but NOT LB=30 ===
    lines.append("## Differential fires — fire under LB=15 but NOT under LB=30\n\n")
    lines.append("These are the events you would gain by switching from 5d to 2.5d 4h lookback. "
                 "Use this to judge whether the new fires look like real volatile plays or "
                 "whipsaw noise.\n\n")
    lines.append("| Time | Ticker | Dir | Price | Level | Type@15 |\n")
    lines.append("|---|---|---|---|---|---|\n")
    diff_15_not_30: list[tuple[int, str, str, str, str, str]] = []
    for ticker, fires in variant_results[15].items():
        for f in fires:
            if _fire_key(ticker, f) not in baseline:
                ts, direction, price, level, stype = f
                diff_15_not_30.append(
                    (ts, ticker, direction, _fmt_price(price), _fmt_price(level), stype)
                )
    diff_15_not_30.sort()
    for ts, ticker, direction, price, level, stype in diff_15_not_30:
        lines.append(f"| {_fmt_ts(ts)} | {ticker} | {direction} | {price} | {level} | {stype} |\n")
    lines.append("\n")

    # === RE-TAGGED FIRES: same event, different structure_type between LB=15 and LB=30 ===
    lines.append("## Re-tagged fires — same event, different `structure_type` between LB=15 "
                 "and LB=30\n\n")
    lines.append("Events that fire under both, but where LB=15 tags them `4h` (higher conviction "
                 "+ tighter level) and LB=30 tags them `1h`. The trade plan's stop level differs "
                 "between the two columns.\n\n")
    lines.append("| Time | Ticker | Dir | Price | Level@15 | Type@15 | Level@30 | Type@30 |\n")
    lines.append("|---|---|---|---|---|---|---|---|\n")
    # Index variant 30 by key for lookup
    by_key_30: dict[tuple[str, int, str], tuple[int, str, float, float, str]] = {}
    for ticker, fires in variant_results[30].items():
        for f in fires:
            by_key_30[(ticker, _fire_key(ticker, f)[1], f[1])] = f
    retagged: list[tuple[int, str, str, str, str, str, str, str]] = []
    for ticker, fires in variant_results[15].items():
        for f in fires:
            ts15, direction, price, level15, stype15 = f
            key = _fire_key(ticker, f)
            other = by_key_30.get(key)
            if other is None:
                continue
            _, _, _, level30, stype30 = other
            if stype15 != stype30:
                retagged.append((
                    ts15, ticker, direction, _fmt_price(price),
                    _fmt_price(level15), stype15,
                    _fmt_price(level30), stype30,
                ))
    retagged.sort()
    for ts, ticker, direction, price, l15, s15, l30, s30 in retagged:
        lines.append(f"| {_fmt_ts(ts)} | {ticker} | {direction} | {price} | "
                     f"{l15} | {s15} | {l30} | {s30} |\n")
    lines.append("\n")

    # === Full per-variant fire logs at the bottom, for reference ===
    for lb in LOOKBACK_VARIANTS:
        results = variant_results[lb]
        lines.append(f"## LB={lb} ({lb * 4 / 24:.1f}d) — full fire log (reference)\n\n")
        lines.append("| Time | Ticker | Dir | Price | Level | Type |\n")
        lines.append("|---|---|---|---|---|---|\n")
        rows: list[tuple[int, str, str, str, str, str]] = []
        for ticker, fires in results.items():
            for ts, direction, price, level, stype in fires:
                rows.append((ts, ticker, direction, _fmt_price(price), _fmt_price(level), stype))
        rows.sort()
        for ts, ticker, direction, price, level, stype in rows:
            lines.append(f"| {_fmt_ts(ts)} | {ticker} | {direction} | {price} | {level} | {stype} |\n")
        lines.append("\n")

    OUT_MD.write_text("".join(lines))
    print(f"Wrote {OUT_MD} ({len(lines)} lines, "
          f"{len(diff_15_not_30)} differential, {len(retagged)} re-tagged)")


def main() -> None:
    print(f"Loading bars from {BARS_CSV}...")
    bars_by_ticker, ac_by_ticker = load_bars_by_ticker()
    print(f"Loaded {len(bars_by_ticker)} tickers, "
          f"total bars: {sum(len(b) for b in bars_by_ticker.values())}")

    variant_results: dict[int, dict[str, list[tuple[int, str, float, float, str]]]] = {}
    for lb in LOOKBACK_VARIANTS:
        print(f"Running SWING_LOOKBACK_4H_BARS = {lb}...")
        variant_results[lb] = run_variant(lb, bars_by_ticker, ac_by_ticker)
        total = sum(len(v) for v in variant_results[lb].values())
        print(f"  → {total} fires across {len(variant_results[lb])} tickers")

    write_markdown(variant_results)


if __name__ == "__main__":
    main()
