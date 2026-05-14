"""Telegram delivery.

POSTs to the Bot HTTP API via `requests`. Failures here NEVER raise out of
`send_alert` / `send_watchlist_notification` — Telegram being down must not
crash the loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)


def _watchlist_chat_id() -> str | None:
    """Optional separate chat for soft watchlist notifications."""
    cid = os.environ.get("TELEGRAM_WATCHLIST_CHAT_ID")
    return cid or os.environ.get("TELEGRAM_CHAT_ID")


def _md_escape(s: str) -> str:
    """Escape characters that legacy Markdown parser chokes on."""
    if not s:
        return ""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


_ARROW = {"long": "🟢 LONG", "short": "🔴 SHORT", "neutral": "⚪ NEUTRAL"}

# Conviction-tier rendering — driven by direction_adjudicator.AdjudicatedDirection
_TIER_BADGE = {
    ("STRONG", "long"):     "🟢🟢🟢 *STRONG LONG*",
    ("STRONG", "short"):    "🔴🔴🔴 *STRONG SHORT*",
    ("OK", "long"):         "🟢 *LONG*",
    ("OK", "short"):        "🔴 *SHORT*",
    ("TENTATIVE", "long"):  "🟡 *LONG (tentative)*",
    ("TENTATIVE", "short"): "🟠 *SHORT (tentative)*",
}


def _render_tier_badge(tier: str, direction: str) -> str:
    """Build the headline direction badge from the adjudicator's tier + direction."""
    direction = (direction or "").lower()
    if tier == "NO_TRADE" or direction == "no_trade":
        return "⏸ *NO TRADE — direction unclear*"
    return _TIER_BADGE.get((tier, direction), f"*{(direction or '?').upper()}*")


def _format_alert(alert: Any, classifier: Any | None) -> str:
    """Build the Markdown payload sent to Telegram."""
    ticker = _md_escape(getattr(alert, "ticker", "?"))
    asset_class = _md_escape(getattr(alert, "asset_class", "?"))
    score = float(getattr(alert, "score", 0.0) or 0.0)
    pct = float(getattr(alert, "r_alpha_pct", 0.0) or 0.0)
    az = getattr(alert, "alpha_z", None)

    header = f"*{ticker}*  ({asset_class})"
    move = f"{pct:+.2f}% (α-resid)" if asset_class.startswith("crypto") else f"{pct:+.2f}% (24h)"
    score_line = f"score `{score:.2f}`"
    if az is not None and az != float("inf"):
        score_line += f", α-z `{float(az):.2f}`"

    if classifier is None:
        body = "_no classification_"
        ctype = direction = ""
    else:
        ctype = _md_escape(getattr(classifier, "catalyst_type", "?"))
        direction = getattr(classifier, "direction", "neutral")
        conf = float(getattr(classifier, "confidence", 0.0) or 0.0)
        summary = _md_escape(getattr(classifier, "summary", "") or "")
        body = (
            f"*{_ARROW.get(direction, direction.upper())}* — "
            f"`{ctype}` (conf {conf:.2f})\n"
            f"{summary}"
        )

    quotes = []
    if classifier is not None:
        for q in (getattr(classifier, "evidence_quotes", None) or [])[:2]:
            q = _md_escape(q.strip())
            if q:
                quotes.append(f"> {q}")
    quote_block = "\n".join(quotes)

    parts = [header, f"{move}  ·  {score_line}", body]
    if quote_block:
        parts.append(quote_block)
    return "\n\n".join(parts)


def _send_sync(chat_id: str, text: str) -> None:
    """POST to the Telegram Bot HTTP API. Raises on HTTP / API error."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": config.TELEGRAM_PARSE_MODE,
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if not r.ok or not r.json().get("ok", False):
        raise RuntimeError(f"telegram api {r.status_code}: {r.text[:300]}")


def send_alert(alert: Any, classifier: Any | None = None) -> bool:
    """Send a single alert to Telegram (legacy signature kept for replay/main).

    Internally delegates to the BOS-aware formatter so the payload still uses
    the structural metadata when present. Returns True on success.
    """
    text = _format_alert(alert, classifier)
    return _send_main(text, market_label=getattr(alert, "ticker", "?"))


# ============================================================================
# BOS-aware send_alert and send_watchlist_notification
# ============================================================================

def _session_tag(market: Any) -> str:
    """Optional session tag (e.g. NYSE-OPEN). Cheap heuristic — leaves blank
    on errors."""
    try:
        from datetime import datetime, timezone
        h = datetime.now(tz=timezone.utc).hour
        if h == config.NYSE_OPEN_UTC:
            return " · NYSE-OPEN"
        if config.NYSE_OPEN_UTC < h < config.NYSE_CLOSE_UTC:
            return " · US-OPEN"
    except Exception:
        pass
    return ""


def _fmt_price(value: float | None, default: str = "—") -> str:
    if value is None:
        return default
    if abs(value) < 1:
        return f"{value:.6f}"
    if abs(value) < 100:
        return f"{value:.4f}"
    return f"{value:.2f}"


def _send_main(text: str, market_label: str = "?") -> bool:
    log.info("ALERT %s: %s", market_label, text.replace("\n", " | "))
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not chat_id:
        return False
    try:
        _send_sync(chat_id, text)
        return True
    except Exception as e:
        log.warning("telegram send failed for %s: %s", market_label, e)
        return False


def _send_watchlist(text: str, market_label: str = "?") -> bool:
    log.info("WATCHLIST %s: %s", market_label, text.replace("\n", " | "))
    chat_id = _watchlist_chat_id()
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not chat_id:
        return False
    try:
        _send_sync(chat_id, text)
        return True
    except Exception as e:
        log.warning("telegram watchlist send failed for %s: %s", market_label, e)
        return False


def _format_plan(plan: Any) -> str:
    """Render a TradePlan as a Markdown block matching the BOS alert style.

    The plan is a multi-stage ladder, not a flat TP1/TP2 cut:
      - Scale TP1_FRACTION at TP1, then move stop to entry (breakeven)
      - Scale TP2_FRACTION at TP2
      - Trail the remainder by TRAIL_ATR_MULT × ATR(14h)
    Old single-target callers still see TP1/TP2 with %s and R-multiples; the
    scale-out + trailing block only renders when those fields are populated.
    """
    direction = (getattr(plan, "direction", "") or "").upper()
    entry = float(getattr(plan, "entry", 0.0) or 0.0)
    stop = float(getattr(plan, "stop", 0.0) or 0.0)
    tp1 = float(getattr(plan, "tp1", 0.0) or 0.0)
    tp2 = float(getattr(plan, "tp2", 0.0) or 0.0)
    risk = float(getattr(plan, "risk_per_unit", 0.0) or 0.0)
    r_tp1 = float(getattr(plan, "r_multiple_tp1", 0.0) or 0.0)
    r_tp2 = float(getattr(plan, "r_multiple_tp2", 0.0) or 0.0)
    tp1_frac = float(getattr(plan, "tp1_fraction", 0.0) or 0.0)
    tp2_frac = float(getattr(plan, "tp2_fraction", 0.0) or 0.0)
    runner_frac = float(getattr(plan, "runner_fraction", 0.0) or 0.0)
    trail_atr = getattr(plan, "trail_atr", None)
    trail_mult = float(getattr(plan, "trail_atr_mult", 0.0) or 0.0)
    breakeven = getattr(plan, "breakeven_trigger", None)

    def _pct(level: float) -> float:
        return ((level - entry) / entry * 100.0) if entry else 0.0

    def _f(frac: float) -> str:
        return f"{int(round(frac * 100))}%"

    tp1_label = f"TP1: ${_fmt_price(tp1)} ({_pct(tp1):+.2f}%, {r_tp1:.1f}R)"
    tp2_label = f"TP2: ${_fmt_price(tp2)} ({_pct(tp2):+.2f}%, {r_tp2:.1f}R)"
    if tp1_frac > 0:
        tp1_label += f" — close {_f(tp1_frac)}"
        if breakeven is not None:
            tp1_label += ", move stop → entry"
    if tp2_frac > 0:
        tp2_label += f" — close {_f(tp2_frac)}"

    lines = [
        f"*Plan:* {direction} @ ${_fmt_price(entry)}",
        f"Stop: ${_fmt_price(stop)} ({_pct(stop):+.2f}%)   Risk: ${_fmt_price(risk)}",
        tp1_label,
        tp2_label,
    ]
    if trail_atr and trail_mult > 0 and runner_frac > 0:
        trail_dist = float(trail_atr) * trail_mult
        lines.append(
            f"Trail: remaining {_f(runner_frac)} by {trail_mult:.1f}×ATR "
            f"(~${_fmt_price(trail_dist)} = {(trail_dist / entry * 100.0 if entry else 0.0):.2f}%)"
        )
    return "\n".join(lines)


def send_bos_alert(
    market: Any,
    classifier_result: Any,
    metadata: dict,
    source: str = "tier1_immediate",
    plan: Any | None = None,
) -> bool:
    """Send a confirmed BOS alert.

    `source` is "tier1_immediate" (BOS confirmed at scan time) or
    "tier2_promoted" (BOS confirmed via watchlist polling).

    `plan` is an optional ``trade_plan.TradePlan`` whose levels are appended
    as a "Plan:" block. Computed by the caller; passing ``None`` falls back
    to the legacy alert-without-plan format.
    """
    # ---- Adjudicated direction is the source of truth (v3.2) ----
    adj = metadata.get("adjudicated") if isinstance(metadata, dict) else None
    if adj is not None:
        direction = (getattr(adj, "direction", "") or "").lower()
        tier = getattr(adj, "conviction_tier", "OK")
        flipped = bool(getattr(adj, "flipped", False))
        fallback = bool(getattr(adj, "fallback", False))
    else:
        direction = (getattr(classifier_result, "direction", "neutral") or "neutral").lower()
        tier = "OK"
        flipped = False
        fallback = False
    tier_badge = _render_tier_badge(tier, direction)

    # ---- Thesis: prefer the predictor's reasoning; fall back to classifier ----
    pred = metadata.get("predictor_result") if isinstance(metadata, dict) else None
    if pred is not None:
        thesis = getattr(pred, "thesis", "") or ""
    else:
        thesis = (
            getattr(classifier_result, "continuation_thesis", None)
            or getattr(classifier_result, "summary", None)
            or ""
        )

    ticker = _md_escape(getattr(market, "ticker", "?"))
    price = float(getattr(market, "price", 0.0) or 0.0)
    pct_24h = float(getattr(market, "pct_24h", 0.0) or 0.0)

    # ---- TP/SL line — flat, scannable. ----
    if plan is not None and direction in ("long", "short"):
        entry = float(getattr(plan, "entry", 0.0) or 0.0)
        stop = float(getattr(plan, "stop", 0.0) or 0.0)
        tp1 = float(getattr(plan, "tp1", 0.0) or 0.0)
        tp2 = float(getattr(plan, "tp2", 0.0) or 0.0)
        r_tp1 = float(getattr(plan, "r_multiple_tp1", 0.0) or 0.0)
        r_tp2 = float(getattr(plan, "r_multiple_tp2", 0.0) or 0.0)
        plan_line = (
            f"\n*Entry* ${_fmt_price(entry)}  ·  *Stop* ${_fmt_price(stop)}  ·  "
            f"*TP1* ${_fmt_price(tp1)} ({r_tp1:.1f}R)  ·  *TP2* ${_fmt_price(tp2)} ({r_tp2:.1f}R)"
        )
    else:
        plan_line = ""

    # ---- Direction-trust flags (only when the user needs to know) ----
    trust_notes: list[str] = []
    if flipped and adj is not None:
        sdir = (metadata.get("structure_direction") or "?").upper()
        trust_notes.append(f"🔄 flipped from structural {sdir}")
    if fallback:
        trust_notes.append("⚙️ LLM unreachable — structural direction only")
    trust_block = ("\n" + " · ".join(trust_notes)) if trust_notes else ""

    body = (
        f"{tier_badge} — *{ticker}*\n"
        f"Price ${_fmt_price(price)} ({pct_24h:+.2f}%){trust_block}"
        f"{plan_line}\n\n"
        f"{_md_escape(thesis)}"
    )
    return _send_main(body, market_label=str(getattr(market, "ticker", "?")))


def send_watchlist_notification(
    market: Any,
    classifier_result: Any,
    metadata: dict,
) -> bool:
    """Soft notification: catalyst is strong but BOS hasn't confirmed yet.

    Goes to TELEGRAM_WATCHLIST_CHAT_ID if set, otherwise the main chat with
    a watchlist emoji prefix.
    """
    direction = (getattr(classifier_result, "direction", "neutral") or "neutral").lower()
    if direction == "long":
        watch_level = metadata.get("swing_high_reference")
        watch_action = f"break above ${_fmt_price(watch_level)}"
    elif direction == "short":
        watch_level = metadata.get("swing_low_reference")
        watch_action = f"break below ${_fmt_price(watch_level)}"
    else:
        watch_action = "structural confirmation"

    primary = (
        getattr(classifier_result, "primary_catalyst", None)
        or getattr(classifier_result, "summary", None)
        or "(no catalyst description)"
    )

    ticker = _md_escape(getattr(market, "ticker", "?"))
    asset_class = _md_escape(getattr(market, "asset_class", "?"))
    pct_24h = float(getattr(market, "pct_24h", 0.0) or 0.0)
    vol = float(getattr(market, "volume_24h_usd", 0.0) or 0.0)
    oi = float(getattr(market, "oi_usd", 0.0) or 0.0)
    score = float(metadata.get("score", 0.0) or 0.0)

    body = (
        f"👀 *WATCHLIST — {ticker}* {pct_24h:+.2f}%\n"
        f"{asset_class}\n\n"
        f"*Catalyst:* {_md_escape(primary)}\n"
        f"*Awaiting:* {watch_action} for {direction.upper()} confirmation\n\n"
        f"Vol ${vol/1e6:.1f}M | OI ${oi/1e6:.1f}M | Score {score:.0f}"
    )
    return _send_watchlist(body, market_label=str(getattr(market, "ticker", "?")))
