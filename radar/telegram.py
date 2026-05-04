"""Telegram delivery.

We use the synchronous `python-telegram-bot` Bot API. Failures here NEVER raise
out of `send_alert` — Telegram being down must not crash the loop.
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
    """Send a single alert to Telegram. Returns True on success, False otherwise."""
    text = _format_alert(alert, classifier)
    log.info("ALERT: %s", text.replace("\n", " | "))

    bot = _bot()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if bot is None or not chat_id:
        return False

    try:
        _send_sync(bot, chat_id, text)
        return True
    except Exception as e:
        log.warning("telegram send failed for %s: %s", getattr(alert, "ticker", "?"), e)
        return False
