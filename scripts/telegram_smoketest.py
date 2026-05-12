"""One-shot Telegram smoke test.

Loads .env, sends:
  1. A plain ping (verifies TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID + network).
  2. A fully-formed mock BOS alert via send_bos_alert (real formatter + plan).

Exits non-zero if either send returns False.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# Ensure repo root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import telegram as tg  # noqa: E402
from radar.trade_plan import TradePlan  # noqa: E402


_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def ping() -> bool:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not _TOKEN or not chat_id:
        print("FAIL ping: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False
    try:
        tg._send_sync(chat_id, "Catalyst Radar — test ping ✅")
        print("OK ping")
        return True
    except Exception as e:
        print(f"FAIL ping: {e}")
        return False


def mock_bos_alert() -> bool:
    market = SimpleNamespace(
        ticker="XAU",
        asset_class="commodity",
        pct_24h=2.34,
        volume_24h_usd=128_400_000.0,
        oi_usd=42_100_000.0,
        funding_1h=0.000045,
    )
    classifier = SimpleNamespace(
        catalyst_type="macro",
        direction="long",
        confidence=0.78,
        conviction=0.78,
        summary="Fed minutes hint at earlier cuts; dollar weakness driving bid in gold.",
        primary_catalyst="Fed minutes hint at earlier rate cuts",
        continuation_thesis=(
            "Real-yield compression + DXY breakdown supports continued bid through "
            "the prior 4h swing; spec longs not yet stretched."
        ),
        kill_signal="Hawkish Fed speaker or DXY reclaiming 104.20",
        horizon="swing",
        evidence_quotes=[
            "FOMC participants saw scope for earlier easing if disinflation persists.",
            "Gold extends gains as Treasury yields slide.",
        ],
    )
    metadata = {
        "breakout_level": 2415.30,
        "structure_direction": "long",
        "swing_high_reference": 2415.30,
        "swing_low_reference": 2378.10,
        "median_bar_range": 4.20,
        "promoted_from_watchlist": False,
    }
    entry = 2418.75
    stop = 2410.40
    risk = entry - stop
    plan = TradePlan(
        direction="long",
        entry=entry,
        stop=stop,
        tp1=entry + 1.5 * risk,
        tp2=entry + 3.0 * risk,
        risk_per_unit=risk,
        r_multiple_tp1=1.5,
        r_multiple_tp2=3.0,
        tp1_fraction=0.33,
        tp2_fraction=0.33,
        breakeven_trigger=entry + 1.5 * risk,
        trail_atr=6.10,
        trail_atr_mult=1.5,
        runner_fraction=0.34,
    )
    ok = tg.send_bos_alert(
        market=market,
        classifier_result=classifier,
        metadata=metadata,
        source="tier1_immediate",
        plan=plan,
    )
    print("OK mock_bos_alert" if ok else "FAIL mock_bos_alert")
    return ok


if __name__ == "__main__":
    results = [ping(), mock_bos_alert()]
    sys.exit(0 if all(results) else 1)
