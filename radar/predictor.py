"""Stage 2 — full-context reasoner.

Stage 1 (radar/classifier.py) is a fast news labeler that runs on every
top-N candidate. Stage 2 runs only on candidates that survive the entire
suppression chain (Rules 0-4) — the alerts that would actually fire.

The reasoner sees:
  - 48h of hourly OHLC + volume
  - The structural break (4h swing level, distance, range/volume expansion)
  - The deterministic trade plan (entry/stop/TP1/TP2/trail-ATR)
  - BTC macro context (24h move, structural state)
  - ATR-14 + median range
  - Stage 1's classifier output (catalyst type, evidence)
  - News bundle (raw)
  - Last 3 alerts on this ticker in past 7 days

Output: a verdict — ALERT_NOW / DOWNGRADE_TO_WATCHLIST / DROP — plus a
plain-English thesis, kill signal, expected horizon, R-target, entry
guidance, and risks.

**Critical design rule:** stage 2 cannot UPGRADE alerts the suppression
chain rejected. Its veto power is downgrade-only. Pure structural setups
that pass the gates are real; the LLM polishes the alert and can demote
it on contradicting evidence, but never invent new alerts.

REST-based (no SDK), no new dependency.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import config, ranker
from .catalysts import NewsItem
from .universe import Market

log = logging.getLogger(__name__)


# ============ schema ============

VALID_VERDICTS = {"ALERT_NOW", "DOWNGRADE_TO_WATCHLIST", "DROP"}
VALID_HORIZONS = {"intraday", "1-3_days", "swing_1_2_weeks"}


@dataclass
class PredictorResult:
    verdict: str                         # ALERT_NOW | DOWNGRADE_TO_WATCHLIST | DROP
    direction_confidence: float          # [0, 1]
    setup_quality: float                 # [0, 1]
    thesis: str                          # 2-4 sentences
    kill_signal: str                     # specific price + reason
    expected_horizon: str                # intraday | 1-3_days | swing_1_2_weeks
    expected_r_multiple: float           # realistic R target
    entry_guidance: str                  # market | pullback to $X | wait for retest
    risks: list[str] = field(default_factory=list)


_ANALYZE_TOOL = {
    "name": "analyze_setup",
    "description": (
        "Record your verdict on this structural breakout candidate. "
        "All fields are required except 'risks' (1-3 entries recommended)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": sorted(VALID_VERDICTS),
                "description": (
                    "ALERT_NOW = clean structural setup with no contradicting signals — "
                    "send to user. DOWNGRADE_TO_WATCHLIST = setup is OK but conditions "
                    "are mixed (catalyst rumor-only, BTC reversing, news contradicts). "
                    "DROP = clear contradicting evidence (concrete bad news, imminent "
                    "supply event). Default to ALERT_NOW unless you have a specific reason."
                ),
            },
            "direction_confidence": {
                "type": "number",
                "description": "How confident in the trade direction (long vs short). 0-1.",
            },
            "setup_quality": {
                "type": "number",
                "description": "Overall setup cleanliness — clean breakout vs noisy chop. 0-1.",
            },
            "thesis": {
                "type": "string",
                "description": (
                    "2-4 sentences in plain English explaining what's happening and "
                    "why the trade idea exists. No jargon. Talk like a colleague."
                ),
            },
            "kill_signal": {
                "type": "string",
                "description": (
                    "The specific price level or condition that would invalidate this trade. "
                    "Plain English. Example: 'Below $0.1450 — that's where the breakout fails "
                    "and structure reverses.'"
                ),
            },
            "expected_horizon": {
                "type": "string",
                "enum": sorted(VALID_HORIZONS),
                "description": "How long the trade typically takes to resolve at typical pace.",
            },
            "expected_r_multiple": {
                "type": "number",
                "description": (
                    "Realistic R-multiple target based on structure and context. "
                    "1.5 = small move, 3 = decent breakout, 5+ = trending breakout."
                ),
            },
            "entry_guidance": {
                "type": "string",
                "description": (
                    "How to enter. Examples: 'market', 'pullback to $0.1466', "
                    "'wait for retest of $0.1456 breakout level'."
                ),
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-3 specific things that could go wrong with this trade.",
            },
        },
        "required": [
            "verdict", "direction_confidence", "setup_quality",
            "thesis", "kill_signal", "expected_horizon",
            "expected_r_multiple", "entry_guidance",
        ],
    },
}


_SYSTEM_PROMPT = """You are an experienced crypto trader reviewing a structural breakout candidate.

The structural setup is ALREADY REAL. Deterministic gates have confirmed:
- 4h swing high/low broken
- 1h candle range >= 2x median (impulsive move)
- 1h volume >= 1.5x median (real participation)
- Higher-timeframe trend agrees with the direction

Your job is QUALITY ASSESSMENT, not gate-keeping. Use the data provided to:
1. Check if a catalyst supports the move (or if it's just technical chop)
2. Check if BTC's macro context is constructive or threatening
3. Identify the specific kill signal in plain English
4. Estimate realistic horizon and R-target
5. Flag risks the trader should watch for

Verdict rules (strict):
- ALERT_NOW (default): structural setup is real, no contradicting signals. Send the alert.
- DOWNGRADE_TO_WATCHLIST: conditions are mixed — catalyst is rumor-only, BTC about to reverse, news contradicts direction, recent dedup fatigue.
- DROP: a CLEAR contradicting signal — concrete adverse news, imminent supply event (large unlock in <24h), regulatory action.

CRITICAL: Default to ALERT_NOW. Use DOWNGRADE/DROP only with a specific, named reason. Do NOT second-guess the structural break with vibes. The structural gates are the edge; you are the storyteller and a downgrade-only gatekeeper."""


# ============ context builders ============

def _format_ohlc_table(bars: list, n_hours: int) -> str:
    """Compact OHLCV table — last n_hours bars, one line per bar."""
    if not bars:
        return "(no bar history available)"
    window = bars[-n_hours:] if len(bars) > n_hours else bars
    lines = ["ts_utc            open      high      low       close     volume"]
    from datetime import datetime, timezone
    for b in window:
        try:
            ts = datetime.fromtimestamp(int(b.ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = str(b.ts)
        o, h, l, c = b.open or 0, b.high or 0, b.low or 0, b.close or 0
        v = b.volume or 0
        lines.append(f"{ts}  {o:<9.4f} {h:<9.4f} {l:<9.4f} {c:<9.4f} {v:<.0f}")
    return "\n".join(lines)


def _format_news_bundle(items: Iterable[NewsItem]) -> str:
    chunks = []
    for i, n in enumerate(items, 1):
        chunks.append(f"[{i}] {n.source}: {n.title}\n    {n.body[:300]}")
    return "\n\n".join(chunks) if chunks else "(no news items found)"


def _format_prior_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "(no prior alerts on this ticker in past 7 days)"
    lines = []
    for a in alerts:
        lines.append(
            f"  {a.get('when','?')}  {a.get('decision','?')} "
            f"({a.get('reason','?')}) — {a.get('catalyst_type','-')}/{a.get('direction','-')}"
        )
    return "\n".join(lines)


def _build_user_prompt(
    market: Market,
    classifier_result: Any,
    plan: Any,
    metadata: dict,
    bar_history: list,
    btc_history: list,
    news_items: list[NewsItem],
    prior_alerts: list[dict],
) -> str:
    direction = (getattr(plan, "direction", "?") or "?").upper()
    entry = float(getattr(plan, "entry", 0.0) or 0.0)
    stop = float(getattr(plan, "stop", 0.0) or 0.0)
    tp1 = float(getattr(plan, "tp1", 0.0) or 0.0)
    tp2 = float(getattr(plan, "tp2", 0.0) or 0.0)
    risk = float(getattr(plan, "risk_per_unit", 0.0) or 0.0)
    r_tp1 = float(getattr(plan, "r_multiple_tp1", 0.0) or 0.0)
    r_tp2 = float(getattr(plan, "r_multiple_tp2", 0.0) or 0.0)
    trail_atr = getattr(plan, "trail_atr", None)

    breakout_level = metadata.get("breakout_level") if metadata else None
    median_range = metadata.get("median_bar_range") if metadata else None

    # Indicator snapshot
    atr = ranker.compute_atr(bar_history, period=int(config.ATR_PERIOD_HOURS))
    median_vol = ranker.compute_median_volume(
        bar_history[-(config.SWING_LOOKBACK_HOURS + 1):-1]
    )
    cur_vol = float(getattr(bar_history[-1], "volume", 0.0) or 0.0) if bar_history else 0.0
    vol_ratio = (cur_vol / median_vol) if median_vol > 0 else None

    # BTC context
    btc_24h_pct = None
    if len(btc_history) >= 25:
        last = float(btc_history[-1].close or 0.0)
        prior = float(btc_history[-25].close or 0.0)
        if prior > 0:
            btc_24h_pct = (last - prior) / prior * 100.0

    # Classifier (stage 1)
    cls_block = "(stage 1 classifier not run for this candidate)"
    if classifier_result is not None:
        cls_block = (
            f"catalyst_type: {getattr(classifier_result, 'catalyst_type', '?')}\n"
            f"direction: {getattr(classifier_result, 'direction', '?')}\n"
            f"confidence: {getattr(classifier_result, 'confidence', 0.0):.2f}\n"
            f"summary: {getattr(classifier_result, 'summary', '')}\n"
            f"is_actionable: {getattr(classifier_result, 'is_actionable', '?')}"
        )

    return (
        f"=== TICKER ===\n"
        f"{market.ticker} ({market.asset_class})  current price: ${market.price:.6f}\n\n"
        f"=== STRUCTURAL BREAK ===\n"
        f"direction: {direction}\n"
        f"4h swing level broken: ${breakout_level}\n"
        f"distance past break: {(((entry - breakout_level) / breakout_level * 100.0) if breakout_level else 0.0):+.2f}%\n"
        f"1h volume vs median: {f'{vol_ratio:.2f}x' if vol_ratio else 'n/a'}\n"
        f"1h median range: {median_range}\n"
        f"ATR-14h: {f'{atr:.6f}' if atr else 'n/a'}\n\n"
        f"=== TRADE PLAN ===\n"
        f"entry: ${entry:.6f}\n"
        f"stop: ${stop:.6f}  (risk ${risk:.6f}/unit)\n"
        f"TP1: ${tp1:.6f} ({r_tp1:.1f}R) — close 33%, move stop to entry\n"
        f"TP2: ${tp2:.6f} ({r_tp2:.1f}R) — close 33%\n"
        f"trail remaining 34% by 1.5x ATR ({trail_atr})\n\n"
        f"=== BTC MACRO CONTEXT ===\n"
        f"BTC 24h: {f'{btc_24h_pct:+.2f}%' if btc_24h_pct is not None else 'n/a'}\n\n"
        f"=== STAGE 1 CLASSIFIER ===\n{cls_block}\n\n"
        f"=== NEWS BUNDLE ===\n{_format_news_bundle(news_items)}\n\n"
        f"=== RECENT 48h OHLCV ===\n{_format_ohlc_table(bar_history, config.STAGE2_BAR_HISTORY_HOURS)}\n\n"
        f"=== RECENT ALERTS ON THIS TICKER (past 7d) ===\n{_format_prior_alerts(prior_alerts)}\n\n"
        f"Use the analyze_setup tool to record your verdict. Default to ALERT_NOW. "
        f"Only DOWNGRADE/DROP with a specific named reason."
    )


# ============ Gemini call ============

def analyze(
    market: Market,
    classifier_result: Any,
    plan: Any,
    metadata: dict,
    bar_history: list,
    btc_history: list,
    news_items: list[NewsItem],
    prior_alerts: list[dict] | None = None,
) -> PredictorResult | None:
    """Run stage 2 reasoner on a confirmed-EMIT structural setup.

    Returns ``None`` on API failure / parsing error — caller should treat
    that as ALERT_NOW (don't downgrade on infrastructure errors).
    """
    if not config.STAGE2_ENABLED:
        return None
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log.warning("predictor: GEMINI_API_KEY not set — skipping stage 2")
        return None
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.warning("predictor: requests unavailable: %s", e)
        return None

    user_prompt = _build_user_prompt(
        market, classifier_result, plan, metadata or {},
        bar_history or [], btc_history or [],
        news_items or [], prior_alerts or [],
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.STAGE2_MODEL}:generateContent?key={api_key}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": user_prompt}],
        }],
        "tools": [{"functionDeclarations": [_ANALYZE_TOOL]}],
        "toolConfig": {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": ["analyze_setup"],
            }
        },
        "generationConfig": {
            "temperature": config.STAGE2_TEMPERATURE,
            "maxOutputTokens": config.STAGE2_MAX_TOKENS,
            "thinkingConfig": {
                "thinkingBudget": int(config.STAGE2_THINKING_BUDGET),
            },
        },
    }

    try:
        r = requests.post(url, json=body, timeout=45)
    except Exception as e:
        log.warning("predictor: Gemini call failed for %s: %s", market.ticker, e)
        return None
    if r.status_code != 200:
        log.warning("predictor: Gemini returned %d for %s: %s",
                    r.status_code, market.ticker, r.text[:200])
        return None
    try:
        payload = r.json()
    except ValueError:
        log.warning("predictor: Gemini returned non-JSON for %s", market.ticker)
        return None

    candidates = payload.get("candidates") or []
    args: dict | None = None
    for cand in candidates:
        for part in (cand.get("content") or {}).get("parts") or []:
            fc = part.get("functionCall")
            if fc and fc.get("name") == "analyze_setup":
                args = fc.get("args") or {}
                break
        if args is not None:
            break
    if args is None:
        log.warning("predictor: no functionCall in response for %s", market.ticker)
        return None

    try:
        verdict = str(args.get("verdict", "ALERT_NOW")).upper()
        if verdict not in VALID_VERDICTS:
            verdict = "ALERT_NOW"
        horizon = str(args.get("expected_horizon", "intraday"))
        if horizon not in VALID_HORIZONS:
            horizon = "intraday"
        result = PredictorResult(
            verdict=verdict,
            direction_confidence=max(0.0, min(1.0, float(args.get("direction_confidence", 0.5)))),
            setup_quality=max(0.0, min(1.0, float(args.get("setup_quality", 0.5)))),
            thesis=str(args.get("thesis", ""))[:1500],
            kill_signal=str(args.get("kill_signal", ""))[:500],
            expected_horizon=horizon,
            expected_r_multiple=float(args.get("expected_r_multiple", 1.5)),
            entry_guidance=str(args.get("entry_guidance", "market"))[:200],
            risks=[str(r)[:200] for r in (args.get("risks") or [])][:3],
        )
        return result
    except (TypeError, ValueError) as e:
        log.warning("predictor: parsing failed for %s: %s", market.ticker, e)
        return None
