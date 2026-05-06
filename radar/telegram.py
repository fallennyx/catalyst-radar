"""Telegram delivery.

We use the synchronous `python-telegram-bot` Bot API. Failures here NEVER raise
out of `send_alert` / `send_watchlist_notification` — Telegram being down must
not crash the loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from . import config

log = logging.getLogger(__name__)

_BOT = None


def _bot():
    global _BOT
    if _BOT is not None:
        return _BOT
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — alerts will only be logged")
        return None
    try:
        from telegram import Bot  # type: ignore
    except Exception as e:
        log.warning("python-telegram-bot unavailable: %s", e)
        return None
    _BOT = Bot(token=token)
    return _BOT


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


def _send_sync(bot: Any, chat_id: str, text: str) -> None:
    """python-telegram-bot v21 is async-only. Run the coro on a private loop."""
    coro = bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=config.TELEGRAM_PARSE_MODE,
        disable_web_page_preview=True,
    )
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


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
    bot = _bot()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot is None or not chat_id:
        return False
    try:
        _send_sync(bot, chat_id, text)
        return True
    except Exception as e:
        log.warning("telegram send failed for %s: %s", market_label, e)
        return False


def _send_watchlist(text: str, market_label: str = "?") -> bool:
    log.info("WATCHLIST %s: %s", market_label, text.replace("\n", " | "))
    bot = _bot()
    chat_id = _watchlist_chat_id()
    if bot is None or not chat_id:
        return False
    try:
        _send_sync(bot, chat_id, text)
        return True
    except Exception as e:
        log.warning("telegram watchlist send failed for %s: %s", market_label, e)
        return False


def send_bos_alert(
    market: Any,
    classifier_result: Any,
    metadata: dict,
    source: str = "tier1_immediate",
) -> bool:
    """Send a confirmed BOS alert.

    `source` is "tier1_immediate" (BOS confirmed at scan time) or
    "tier2_promoted" (BOS confirmed via watchlist polling).
    """
    breakout_level = metadata.get("breakout_level")
    promoted_tag = ""
    if metadata.get("promoted_from_watchlist"):
        promoted_tag = f" \\[promoted: {metadata.get('hours_on_watchlist', 0)}h on watchlist]"

    direction = (getattr(classifier_result, "direction", "neutral") or "neutral").lower()
    direction_emoji = "🔥" if direction == "long" else ("🩸" if direction == "short" else "⚪")

    primary = (
        getattr(classifier_result, "primary_catalyst", None)
        or getattr(classifier_result, "summary", None)
        or "(no catalyst description)"
    )
    catalyst_type = getattr(classifier_result, "catalyst_type", "?")
    conviction = float(getattr(classifier_result, "conviction", None)
                       or getattr(classifier_result, "confidence", 0.0) or 0.0)
    horizon = getattr(classifier_result, "horizon", "unknown")
    thesis = (
        getattr(classifier_result, "continuation_thesis", None)
        or getattr(classifier_result, "summary", None)
        or ""
    )
    kill = getattr(classifier_result, "kill_signal", "") or "—"

    ticker = _md_escape(getattr(market, "ticker", "?"))
    asset_class = _md_escape(getattr(market, "asset_class", "?"))
    pct_24h = float(getattr(market, "pct_24h", 0.0) or 0.0)
    vol = float(getattr(market, "volume_24h_usd", 0.0) or 0.0)
    oi = float(getattr(market, "oi_usd", 0.0) or 0.0)
    funding = float(getattr(market, "funding_1h", 0.0) or 0.0)

    body = (
        f"{direction_emoji} *RADAR — {ticker}* {pct_24h:+.2f}%{promoted_tag}\n"
        f"{asset_class}{_session_tag(market)}\n\n"
        f"*BOS confirmed:* {direction.upper()} above ${_fmt_price(breakout_level)}\n"
        f"*Catalyst:* {_md_escape(primary)}\n"
        f"*Type:* {_md_escape(catalyst_type)} · *Conviction:* {conviction*100:.0f}/100\n"
        f"*Horizon:* {_md_escape(horizon)}\n\n"
        f"{_md_escape(thesis)}\n\n"
        f"*Kill:* {_md_escape(kill)}\n\n"
        f"Vol ${vol/1e6:.1f}M | OI ${oi/1e6:.1f}M | Funding {funding*100:.4f}%\n"
        f"[Open in Lighter](https://app.lighter.xyz/trade/{ticker})"
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
