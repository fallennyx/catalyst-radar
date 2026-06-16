"""Backtest EMIT alerts: recompute BOS direction at alert time, fetch forward
prices at +1/2/4/8h from the same stored 1h bars, score signed P&L per alert.

The alerts table never persisted `direction` (replays ran --no-classify), so we
faithfully re-derive the engine's intended long/short by re-running
`ranker.has_breakout_structure` on the exact 1h history the engine saw at alert
time. Forward returns come from the same bars_1h table — no external fetch, no
look-ahead beyond the chosen horizon.

Outputs:
  data/alert_backtest.csv  — one row per EMIT alert (downloadable)
  data/alert_backtest.md   — aggregated accuracy analysis
"""
from __future__ import annotations

import csv
import sqlite3
import statistics as st
from dataclasses import dataclass, field
from datetime import datetime, timezone

from radar import ranker, config
from radar.storage import Bar
from radar.universe import Market

SOURCES = [
    ("data/march_replay.db",      "feb25-mar23"),
    ("data/may5_full_replay.db",  "apr05-may06"),
    ("data/may9_replay.db",       "may07-may09"),
    ("data/replay_may10_11.db",   "may09-may11"),
]
HORIZONS_H = [1, 2, 4, 8]
HISTORY_HOURS = 240


@dataclass
class Row:
    db: str
    ticker: str
    asset_class: str
    score: float | None
    alpha_z: float | None
    r_alpha_pct: float | None
    alert_ts: int
    alert_hour: int            # UTC 0-23
    alert_dow: str             # Mon-Sun
    direction: str | None
    structure_type: str | None
    breakout_level: float | None
    breakout_dist_pct: float | None   # (entry - level) / level * 100, directional
    reproduced: bool
    entry: float | None
    # market regime at alert time
    btc_ret_1h_pct: float | None      # BTC close-over-close 1h before alert
    btc_ret_4h_pct: float | None
    btc_range_expansion: bool | None  # BTC itself in 2× range expansion at alert?
    # ticker context at alert bar
    ticker_ret_4h_pct: float | None   # ticker's own 4h prior return
    vol_ratio: float | None           # alert-bar volume / median(last 48 bars)
    score_pctile: float | None        # score rank among same-UTC-day EMITs
    cluster_size: int | None          # # tickers that EMITted same UTC hour
    repeat_fire_8h: bool              # same ticker EMITted within prior 8h?
    fwd: dict[int, float | None] = field(default_factory=dict)
    signed_ret: dict[int, float | None] = field(default_factory=dict)


def _bars_upto(cx: sqlite3.Connection, ticker: str, ts: int) -> list[Bar]:
    cur = cx.execute(
        "SELECT * FROM bars_1h WHERE ticker=? AND ts<=? AND ts>=? ORDER BY ts ASC",
        (ticker, ts, ts - HISTORY_HOURS * 3600),
    )
    return [
        Bar(ticker=r["ticker"], ts=int(r["ts"]), open=r["open"], high=r["high"],
            low=r["low"], close=r["close"], volume=r["volume"], oi=r["oi"],
            funding=r["funding"])
        for r in cur.fetchall()
    ]


def _close_at(cx: sqlite3.Connection, ticker: str, ts: int) -> float | None:
    r = cx.execute(
        "SELECT close FROM bars_1h WHERE ticker=? AND ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts-?) LIMIT 1",
        (ticker, ts - 1800, ts + 1800, ts),
    ).fetchone()
    return float(r["close"]) if r and r["close"] is not None else None


def _btc_ret(cx: sqlite3.Connection, ts: int, lookback_h: int) -> float | None:
    c0 = _close_at(cx, "BTC", ts - lookback_h * 3600)
    c1 = _close_at(cx, "BTC", ts)
    if c0 and c1 and c0 > 0:
        return (c1 - c0) / c0 * 100.0
    return None


def _btc_range_expansion(cx: sqlite3.Connection, ts: int) -> bool | None:
    """True if BTC's alert-bar range > 2× median of prior 48 1h bars."""
    cur = cx.execute(
        "SELECT high, low FROM bars_1h WHERE ticker='BTC' AND ts<=? ORDER BY ts DESC LIMIT 49",
        (ts,),
    ).fetchall()
    if len(cur) < 10:
        return None
    alert_bar = cur[0]
    if alert_bar["high"] is None or alert_bar["low"] is None:
        return None
    alert_range = float(alert_bar["high"]) - float(alert_bar["low"])
    prior_ranges = [float(r["high"]) - float(r["low"])
                    for r in cur[1:] if r["high"] and r["low"]]
    if not prior_ranges:
        return None
    med = st.median(prior_ranges)
    return alert_range > 2.0 * med if med > 0 else None


def _ticker_ret_4h(cx: sqlite3.Connection, ticker: str, ts: int) -> float | None:
    c0 = _close_at(cx, ticker, ts - 4 * 3600)
    c1 = _close_at(cx, ticker, ts)
    if c0 and c1 and c0 > 0:
        return (c1 - c0) / c0 * 100.0
    return None


def _vol_ratio(bars: list[Bar]) -> float | None:
    if len(bars) < 2:
        return None
    alert_vol = bars[-1].volume
    if alert_vol is None:
        return None
    prior_vols = [b.volume for b in bars[-49:-1] if b.volume is not None]
    if len(prior_vols) < 5:
        return None
    med = st.median(prior_vols)
    return float(alert_vol) / med if med > 0 else None


def process(db: str, label: str) -> list[Row]:
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row
    emits = cx.execute(
        "SELECT ticker, asset_class, score, alpha_z, r_alpha_pct, created_at "
        "FROM alerts WHERE decision='EMIT' ORDER BY created_at ASC"
    ).fetchall()

    # cluster size: # tickers emitting in the same UTC hour
    from collections import Counter
    hour_cluster: Counter[int] = Counter(
        int(a["created_at"]) // 3600 * 3600 for a in emits
    )
    # set of (ticker, hour_bucket) for repeat-fire detection
    emit_hours: set[tuple[str, int]] = set()

    out: list[Row] = []
    for a in emits:
        tkr = a["ticker"]
        ts = int(a["created_at"])
        dt = datetime.fromtimestamp(ts, timezone.utc)
        hour_bucket = ts // 3600 * 3600

        hist = _bars_upto(cx, tkr, ts)
        entry = float(hist[-1].close) if hist and hist[-1].close is not None else None

        direction = stype = level = None
        reproduced = False
        breakout_dist_pct = None
        if hist and entry is not None:
            mkt = Market(ticker=tkr, asset_class=a["asset_class"], price=entry)
            broke, direction, level, stype = ranker.has_breakout_structure(
                mkt, hist, current_price=entry
            )
            reproduced = bool(broke and direction)
            if level and entry and level > 0:
                raw_dist = (entry - level) / level * 100.0
                breakout_dist_pct = raw_dist if direction == "long" else -raw_dist

        fwd: dict[int, float | None] = {}
        sret: dict[int, float | None] = {}
        for h in HORIZONS_H:
            p = _close_at(cx, tkr, ts + h * 3600)
            fwd[h] = p
            if p is not None and entry and direction in ("long", "short"):
                raw = (p - entry) / entry * 100.0
                sret[h] = raw if direction == "long" else -raw
            else:
                sret[h] = None

        # repeat fire: was this ticker in the emit_hours set for any prior 8h bucket?
        prior_buckets = {(ts - i * 3600) // 3600 * 3600 for i in range(1, 9)}
        repeat = any((tkr, b) in emit_hours for b in prior_buckets)
        emit_hours.add((tkr, hour_bucket))

        out.append(Row(
            db=label,
            ticker=tkr,
            asset_class=a["asset_class"],
            score=a["score"],
            alpha_z=a["alpha_z"],
            r_alpha_pct=a["r_alpha_pct"],
            alert_ts=ts,
            alert_hour=dt.hour,
            alert_dow=dt.strftime("%a"),
            direction=direction,
            structure_type=stype,
            breakout_level=level,
            breakout_dist_pct=breakout_dist_pct,
            reproduced=reproduced,
            entry=entry,
            btc_ret_1h_pct=_btc_ret(cx, ts, 1),
            btc_ret_4h_pct=_btc_ret(cx, ts, 4),
            btc_range_expansion=_btc_range_expansion(cx, ts),
            ticker_ret_4h_pct=_ticker_ret_4h(cx, tkr, ts),
            vol_ratio=_vol_ratio(hist),
            score_pctile=None,  # filled after all alerts collected per day
            cluster_size=hour_cluster[hour_bucket],
            repeat_fire_8h=repeat,
            fwd=fwd,
            signed_ret=sret,
        ))
    cx.close()
    return out


def _fill_score_pctiles(rows: list[Row]) -> None:
    from collections import defaultdict
    by_day: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        day = datetime.fromtimestamp(r.alert_ts, timezone.utc).strftime("%Y-%m-%d")
        by_day[day].append(r)
    for day_rows in by_day.values():
        scores = sorted(
            (r.score for r in day_rows if r.score is not None), reverse=False
        )
        n = len(scores)
        for r in day_rows:
            if r.score is not None and n > 0:
                rank = scores.index(r.score)
                r.score_pctile = round(rank / n * 100.0, 1)


def _f(v: float | None, decimals: int = 4) -> str:
    return f"{v:.{decimals}f}" if v is not None else ""


def _agg(vals: list[float]) -> tuple[float, float, int]:
    if not vals:
        return (0.0, 0.0, 0)
    wins = sum(1 for v in vals if v > 0)
    return (st.mean(vals), wins / len(vals) * 100.0, len(vals))


def write_csv(rows: list[Row], path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        head = [
            "db", "ticker", "asset_class",
            "score", "score_pctile", "alpha_z", "r_alpha_pct",
            "alert_utc", "alert_hour", "alert_dow",
            "direction", "structure_type",
            "breakout_level", "breakout_dist_pct", "reproduced", "entry",
            "btc_ret_1h_pct", "btc_ret_4h_pct", "btc_range_expansion",
            "ticker_ret_4h_pct", "vol_ratio",
            "cluster_size", "repeat_fire_8h",
        ]
        for h in HORIZONS_H:
            head += [f"px_+{h}h", f"signed_ret_+{h}h_pct"]
        w.writerow(head)
        for r in rows:
            ut = datetime.fromtimestamp(r.alert_ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
            line = [
                r.db, r.ticker, r.asset_class,
                _f(r.score, 3), _f(r.score_pctile, 1), _f(r.alpha_z, 4), _f(r.r_alpha_pct, 4),
                ut, r.alert_hour, r.alert_dow,
                r.direction or "", r.structure_type or "",
                _f(r.breakout_level, 6), _f(r.breakout_dist_pct, 4),
                int(r.reproduced), _f(r.entry, 6),
                _f(r.btc_ret_1h_pct, 4), _f(r.btc_ret_4h_pct, 4),
                "" if r.btc_range_expansion is None else int(r.btc_range_expansion),
                _f(r.ticker_ret_4h_pct, 4), _f(r.vol_ratio, 3),
                r.cluster_size, int(r.repeat_fire_8h),
            ]
            for h in HORIZONS_H:
                line += [_f(r.fwd[h], 6), _f(r.signed_ret[h], 4)]
            w.writerow(line)


def write_md(rows: list[Row], path: str) -> None:
    scored = [r for r in rows if r.reproduced]

    def block(subset: list[Row], title: str) -> list[str]:
        lines = [f"### {title} (n={len(subset)})", "",
                 "| Horizon | n | Win rate | Avg signed return | Median |",
                 "|---|---|---|---|---|"]
        for h in HORIZONS_H:
            vals = [r.signed_ret[h] for r in subset if r.signed_ret[h] is not None]
            avg, wr, n = _agg(vals)
            med = st.median(vals) if vals else 0.0
            lines.append(f"| +{h}h | {n} | {wr:.1f}% | {avg:+.2f}% | {med:+.2f}% |")
        lines.append("")
        return lines

    md: list[str] = ["# Alert Direction Backtest", "",
          f"- Total EMIT alerts pulled: **{len(rows)}**",
          f"- Direction reproduced via BOS recompute: **{len(scored)}** "
          f"({len(scored)/len(rows)*100:.0f}%)",
          f"- Longs: {sum(1 for r in scored if r.direction=='long')} | "
          f"Shorts: {sum(1 for r in scored if r.direction=='short')}",
          "",
          "Signed return: positive = engine direction was correct.",
          "Entry = close of the alert-hour bar. No fees/slippage.", ""]

    md += block(scored, "All reproduced alerts")
    md += block([r for r in scored if r.direction == "long"], "Longs only")
    md += block([r for r in scored if r.direction == "short"], "Shorts only")

    # --- BTC regime cuts ---
    btc_up   = [r for r in scored if r.btc_ret_4h_pct is not None and r.btc_ret_4h_pct >  1.0]
    btc_flat = [r for r in scored if r.btc_ret_4h_pct is not None and -1.0 <= r.btc_ret_4h_pct <= 1.0]
    btc_down = [r for r in scored if r.btc_ret_4h_pct is not None and r.btc_ret_4h_pct < -1.0]
    md += ["## BTC regime at alert time (+4h horizon)", "",
           "BTC 4h return before alert bucketed: >+1% = Up, -1%..+1% = Flat, <-1% = Down", "",
           "| BTC regime | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for label, subset in [("Up (>+1%)", btc_up), ("Flat", btc_flat), ("Down (<-1%)", btc_down)]:
        vals = [r.signed_ret[4] for r in subset if r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        md.append(f"| {label} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- score pctile ---
    top = [r for r in scored if r.score_pctile is not None and r.score_pctile >= 75]
    bot = [r for r in scored if r.score_pctile is not None and r.score_pctile < 25]
    md += ["## Score percentile (+4h horizon)", "",
           "| Score bucket | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for label, subset in [("Top 25% score", top), ("Bottom 25% score", bot)]:
        vals = [r.signed_ret[4] for r in subset if r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        md.append(f"| {label} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- alpha_z cut ---
    hi_a = [r for r in scored if r.alpha_z is not None and abs(r.alpha_z) >= 3.0]
    lo_a = [r for r in scored if r.alpha_z is not None and abs(r.alpha_z) < 3.0]
    md += ["## Alpha-Z strength (+4h horizon)", "",
           "| |alpha_z| bucket | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for label, subset in [("≥3 (strong decoupling)", hi_a), ("<3 (weak)", lo_a)]:
        vals = [r.signed_ret[4] for r in subset if r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        md.append(f"| {label} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- vol ratio ---
    hi_v = [r for r in scored if r.vol_ratio is not None and r.vol_ratio >= 2.0]
    lo_v = [r for r in scored if r.vol_ratio is not None and r.vol_ratio < 2.0]
    md += ["## Volume ratio at alert (+4h horizon)", "",
           "| Vol ratio | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for label, subset in [("≥2× median volume", hi_v), ("<2× median volume", lo_v)]:
        vals = [r.signed_ret[4] for r in subset if r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        md.append(f"| {label} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- cluster size ---
    iso = [r for r in scored if r.cluster_size == 1]
    clust = [r for r in scored if r.cluster_size is not None and r.cluster_size >= 5]
    md += ["## Alert cluster size (+4h horizon)", "",
           "Cluster size = # tickers that EMITted in the same UTC hour.", "",
           "| Cluster | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for label, subset in [("Isolated (1 ticker/hr)", iso), ("Clustered (≥5/hr)", clust)]:
        vals = [r.signed_ret[4] for r in subset if r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        md.append(f"| {label} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- repeat fire ---
    rep = [r for r in scored if r.repeat_fire_8h]
    fresh = [r for r in scored if not r.repeat_fire_8h]
    md += ["## Repeat-fire (+4h horizon)", "",
           "repeat_fire_8h = same ticker EMITted within prior 8h.", "",
           "| | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for label, subset in [("First fire", fresh), ("Repeat within 8h", rep)]:
        vals = [r.signed_ret[4] for r in subset if r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        md.append(f"| {label} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- hour of day ---
    md += ["## By UTC hour (+4h horizon, top/bottom 5)", "",
           "| UTC hour | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    hour_data = []
    for h in range(24):
        vals = [r.signed_ret[4] for r in scored
                if r.alert_hour == h and r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        if n >= 5:
            hour_data.append((h, n, wr, avg))
    hour_data.sort(key=lambda x: x[2], reverse=True)
    for h, n, wr, avg in hour_data[:5]:
        md.append(f"| {h:02d}:00 UTC | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("| ... | | | |")
    for h, n, wr, avg in hour_data[-5:]:
        md.append(f"| {h:02d}:00 UTC | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- asset class ---
    md += ["## By asset class (+4h horizon)", "",
           "| Asset class | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for c in sorted({r.asset_class for r in scored}):
        vals = [r.signed_ret[4] for r in scored
                if r.asset_class == c and r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        if n:
            md.append(f"| {c} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- structure type ---
    md += ["## By structure type (+4h horizon)", "",
           "| Structure | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    for stp in sorted({r.structure_type for r in scored if r.structure_type}):
        vals = [r.signed_ret[4] for r in scored
                if r.structure_type == stp and r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        if n:
            md.append(f"| {stp} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    # --- ISO week ---
    md += ["## By ISO week (+4h horizon)", "",
           "| ISO week | n | Win rate | Avg signed return |",
           "|---|---|---|---|"]
    def wk(r: Row) -> str:
        d = datetime.fromtimestamp(r.alert_ts, timezone.utc).isocalendar()
        return f"{d.year}-W{d.week:02d}"
    for w in sorted({wk(r) for r in scored}):
        vals = [r.signed_ret[4] for r in scored
                if wk(r) == w and r.signed_ret[4] is not None]
        avg, wr, n = _agg(vals)
        if n:
            md.append(f"| {w} | {n} | {wr:.1f}% | {avg:+.2f}% |")
    md.append("")

    md += ["## Column glossary",
           "- **signed_ret_+Nh_pct**: ((price at alert+Nh - entry) / entry) × 100, negated for short alerts. Positive = direction correct.",
           "- **btc_ret_4h_pct**: BTC's close-over-close return over the 4h before the alert fired.",
           "- **btc_range_expansion**: 1 if BTC's alert-bar range > 2× its own 48-bar median (BTC itself in impulse).",
           "- **ticker_ret_4h_pct**: the alerted ticker's own 4h return going into the alert — was it already running?",
           "- **vol_ratio**: alert-bar volume ÷ median volume of prior 48 bars.",
           "- **score_pctile**: alert's composite score rank among all same-day EMITs (0=lowest, 100=highest).",
           "- **cluster_size**: # tickers that EMITted in the same UTC hour. High = broad market move; 1 = isolated signal.",
           "- **repeat_fire_8h**: 1 if the same ticker EMITted within the prior 8h (continuation vs fresh break).",
           "- **breakout_dist_pct**: how far price had already moved past the BOS pivot at entry, in %.",
           "- **alpha_z / r_alpha_pct**: BTC-decoupling metrics from the beta module.",
           "- **Win rate**: fraction of alerts where signed return > 0 at that horizon. Coin-flip = 50%."]

    with open(path, "w") as f:
        f.write("\n".join(md) + "\n")


def main() -> None:
    all_rows: list[Row] = []
    seen: set[tuple[str, int]] = set()
    for db, label in SOURCES:
        for r in process(db, label):
            key = (r.ticker, r.alert_ts)
            if key in seen:
                continue
            seen.add(key)
            all_rows.append(r)
    all_rows.sort(key=lambda r: r.alert_ts)
    _fill_score_pctiles(all_rows)
    write_csv(all_rows, "data/alert_backtest.csv")
    write_md(all_rows, "data/alert_backtest.md")
    rep = sum(1 for r in all_rows if r.reproduced)
    print(f"alerts={len(all_rows)} reproduced={rep} "
          f"-> data/alert_backtest.csv + data/alert_backtest.md")


if __name__ == "__main__":
    main()
