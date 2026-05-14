"""Stage 2 — direction adjudicator + full-context reasoner.

In v3.2 the predictor is no longer a downgrade-only post-filter. It is the
**final authority on alert direction**. The structural BOS cross fires the
event; the predictor decides what the user reads.

The reasoner sees:
  - 48h of hourly OHLC + volume
  - 8 most recent 15m bars (intra-bar wick / sweep evidence)
  - The structural break (pivot side broken, frame, distance, range expansion)
  - The current 1h bar's wick shape (was the pivot wicked then reclaimed?)
  - Order-book imbalance (live bid/ask USD, ratio)
  - OI / funding / volume context (latest bar + 1h delta)
  - HTF trend status (7d)
  - BTC macro context (24h move)
  - Stage 1 classifier output (catalyst type, direction, evidence)
  - News bundle (raw)
  - Prior alerts on this ticker (last 7d)
  - Tier (1 vs 2) + watchlist age if Tier 2

Output: ``PredictorResult(final_direction, direction_confidence, verdict, …)``
where ``final_direction ∈ {"long", "short", "no_trade"}``. The trade plan is
built from ``final_direction``, not the structural cross. The ``verdict`` field
is retained for backward compatibility but is advisory only — the alert always
sends. Direction is the only thing the LLM controls.

REST-based (no SDK), no new dependency.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, ranker
from .catalysts import NewsItem
from .universe import Market

log = logging.getLogger(__name__)


# ============ schema ============

VALID_VERDICTS = {"ALERT_NOW", "DOWNGRADE_TO_WATCHLIST", "DROP"}
VALID_HORIZONS = {"intraday", "1-3_days", "swing_1_2_weeks"}
VALID_DIRECTIONS = {"long", "short", "no_trade"}


@dataclass
class PredictorResult:
    # ---- v3.2 direction adjudication ----
    final_direction: str                 # "long" | "short" | "no_trade"
    direction_confidence: float          # [0, 1] — how sure of the final direction
    setup_quality: float                 # [0, 1] — clean break vs noise

    # ---- prose enrichment ----
    thesis: str                          # 2-4 sentences explaining the call
    kill_signal: str                     # specific price + reason that invalidates
    expected_horizon: str                # intraday | 1-3_days | swing_1_2_weeks
    expected_r_multiple: float           # realistic R target
    entry_guidance: str                  # market | pullback to $X | wait for retest

    # ---- legacy / advisory ----
    verdict: str = "ALERT_NOW"           # ALERT_NOW | DOWNGRADE_TO_WATCHLIST | DROP (advisory in v3)
    risks: list[str] = field(default_factory=list)


_ANALYZE_TOOL = {
    "name": "decide_direction",
    "description": (
        "Record your direction call on this structural breakout candidate. "
        "You are the FINAL authority on which way (if any) the user should trade. "
        "All fields are required except 'risks'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "final_direction": {
                "type": "string",
                "enum": sorted(VALID_DIRECTIONS),
                "description": (
                    "Your direction call. 'long' = user should buy. 'short' = user should "
                    "sell short. 'no_trade' = the structural cross fired but the situation "
                    "is ambiguous (sweep, conflicting catalyst, weak confirmation) and "
                    "trading either side now is a coin flip. Use 'no_trade' liberally when "
                    "evidence is mixed — it's better than putting the user into a bad trade. "
                    "You MAY override the structural direction (long break → short call) when "
                    "evidence strongly supports a fade — but only with concrete reasons."
                ),
            },
            "direction_confidence": {
                "type": "number",
                "description": (
                    "How sure are you of final_direction? [0,1]. "
                    ">=0.75 = strong (all signals agree, clean setup). "
                    "0.5-0.75 = OK (most signals agree, some mixed). "
                    "0.3-0.5 = tentative (real but contested). "
                    "<0.3 = no_trade territory. Be honest — this controls how the user sizes."
                ),
            },
            "setup_quality": {
                "type": "number",
                "description": (
                    "Overall setup cleanliness [0,1]. Clean impulsive break, good distance "
                    "past pivot, confirming volume = high. Sloppy wick, low volume, choppy "
                    "leading bars = low. Independent of direction_confidence — a setup can "
                    "be high-quality but ambiguous direction."
                ),
            },
            "thesis": {
                "type": "string",
                "description": (
                    "2-4 sentences in plain English explaining (a) why you chose this "
                    "direction and (b) what's driving the move. Reference specific signals "
                    "(e.g. 'OI rising into the break + bid-heavy book + earnings beat'). "
                    "Talk like a colleague. No jargon for jargon's sake. If you chose "
                    "'no_trade' or flipped direction, the thesis MUST justify why."
                ),
            },
            "kill_signal": {
                "type": "string",
                "description": (
                    "The specific price level or condition that invalidates this trade. "
                    "Plain English. Example: 'Below $0.1450 — that's where the breakout "
                    "fails and structure reverses.'"
                ),
            },
            "expected_horizon": {
                "type": "string",
                "enum": sorted(VALID_HORIZONS),
                "description": "How long the trade typically takes to resolve.",
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
                    "'wait for retest of $0.1456 breakout level'. If final_direction "
                    "is 'no_trade', set this to 'no trade — wait'."
                ),
            },
            "verdict": {
                "type": "string",
                "enum": sorted(VALID_VERDICTS),
                "description": (
                    "Advisory severity tag. ALERT_NOW = clean. DOWNGRADE_TO_WATCHLIST = "
                    "mixed. DROP = strongly contradicting. Used only for log labelling; "
                    "the alert still sends regardless."
                ),
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-3 specific things that could go wrong.",
            },
        },
        "required": [
            "final_direction", "direction_confidence", "setup_quality",
            "thesis", "kill_signal", "expected_horizon",
            "expected_r_multiple", "entry_guidance", "verdict",
        ],
    },
}


_SYSTEM_PROMPT = """You are an experienced discretionary daytrader. You are reviewing a structural breakout candidate that has already passed a deterministic gate: price crossed a swing pivot with confirming range expansion.

Your job is to decide the TRADE DIRECTION the user should take RIGHT NOW. The user will read your call on Telegram and act on it immediately, so be honest: if the situation is ambiguous, say 'no_trade'. A missed setup costs nothing; a wrong-way alert can lose real money.

THE STRUCTURAL DIRECTION IS A PRIOR, NOT THE ANSWER. The engine printed "structure broke long" because price crossed a swing high — that fact is true, but it does not by itself tell you the next move is up. Re-evaluate using everything below:

DIRECTIONAL EVIDENCE TO WEIGH

1. SWEEP / LIQUIDITY GRAB — Look at the current 1h bar's wick + close. If the wick crossed the pivot but the body closed back inside (or near the pivot), this is a liquidity sweep and the next move is often the opposite direction. Same on 15m. Sweep = strong reason for no_trade or flip.

2. CATALYST DIRECTION — The stage-1 classifier inferred a direction from news. If it's strong and FRESH (< 4h) and points opposite the structure (e.g. classifier=short due to regulatory action while structure=long), prefer the catalyst direction or no_trade. Stale news (>12h) loses weight — the move is probably already priced.

3. ORDER-BOOK IMBALANCE — Live bid/ask USD depth on Lighter (top 10 levels). A bid/ask ratio >2 against the structural direction (e.g. bid-heavy book on a short break) at the moment of fire is a strong reversal tell. Ratios within 0.8-1.25 are neutral.

4. OI / FUNDING SKEW — Rising OI into a long break = real money joining = confirmation. Falling OI into a long break = distribution = fade. Negative funding (shorts paying longs) on a long break = squeeze setup, supports long. Extreme funding the same side as the break = crowded, fade risk.

5. VOLUME — Bar volume way above 30-day median + clean wick = participation. Below-median volume on a break = thin, often fades back.

6. HTF TREND — 7-day median close direction. A long break in a clear downtrend is a counter-trend trade and lower probability; not a veto, but a soft penalty. A long break in an established uptrend gets a confidence boost.

7. BTC MACRO — Crypto only. BTC making fresh 24h lows during an altcoin long break = high reversal risk. BTC flat or constructive = supportive.

8. PRIOR ALERTS — Multiple recent alerts on the same ticker same direction may indicate chop; the LATEST one is often the trap.

9. TIME OF DAY (UTC) — Asia hours (00-08 UTC) often see thin moves that reverse on NY open. NY open (13-15 UTC) and US close (20-22 UTC) carry weight.

DECISION RULES (strict)

- DEFAULT: confirm the structural direction with high confidence when 3+ signals above clearly agree.
- FLIP (long structure → short call, or vice versa) ONLY when (a) sweep evidence is clear AND (b) at least 2 other signals point the opposite way. Flipping is rare and you must justify it concretely in the thesis.
- NO_TRADE when signals are genuinely split — e.g. structure says long, catalyst says short, book is neutral, OI is mixed. Don't force a call.
- direction_confidence must reflect reality. If you are 60% sure of long, output 0.6, not 0.9. The user sizes off this number.

OUTPUT FORMAT
Call the `decide_direction` tool. Every required field must be populated. Be specific in the thesis — name the signals you weighed. Be specific in the kill_signal — name the exact price."""


# ============ context builders ============

def _format_ohlc_table(bars: list, n_hours: int) -> str:
    """Compact OHLCV table — last n_hours bars, one line per bar."""
    if not bars:
        return "(no bar history available)"
    window = bars[-n_hours:] if len(bars) > n_hours else bars
    lines = ["ts_utc            open      high      low       close     volume"]
    for b in window:
        try:
            ts = datetime.fromtimestamp(int(b.ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = str(b.ts)
        o, h, l, c = b.open or 0, b.high or 0, b.low or 0, b.close or 0
        v = b.volume or 0
        lines.append(f"{ts}  {o:<9.4f} {h:<9.4f} {l:<9.4f} {c:<9.4f} {v:<.0f}")
    return "\n".join(lines)


def _format_15m_tail(bars_15m: list | None, n: int = 8) -> str:
    """Last n 15m bars — gives the LLM intra-hour wick/sweep evidence."""
    if not bars_15m:
        return "(no 15m bars available — 1h-only mode)"
    window = bars_15m[-n:]
    lines = ["ts_utc            open      high      low       close     volume"]
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
        try:
            age_h = (datetime.now(tz=timezone.utc).timestamp() - int(n.published or 0)) / 3600.0
            age = f"  (~{age_h:.1f}h ago)" if n.published else ""
        except Exception:
            age = ""
        chunks.append(f"[{i}] {n.source}{age}: {n.title}\n    {n.body[:300]}")
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


def _wick_analysis(bar: Any, pivot: float | None, side: str) -> str:
    """Compute sweep evidence: did the wick cross the pivot but the body close back inside?"""
    if bar is None or pivot is None:
        return "(no wick data)"
    try:
        o = float(bar.open or 0.0)
        h = float(bar.high or 0.0)
        l = float(bar.low or 0.0)
        c = float(bar.close or 0.0)
    except Exception:
        return "(malformed bar)"
    rng = h - l
    if rng <= 0:
        return "(zero-range bar)"

    if side == "long":
        wicked_through = h > pivot
        body_above = min(o, c) > pivot       # body fully above pivot = no sweep
        body_below = max(o, c) < pivot       # body fully below pivot after wick = clean sweep
        upper_wick = h - max(o, c)
        upper_wick_pct = upper_wick / rng * 100.0
        if wicked_through and body_below:
            return f"⚠️ SWEEP: wick crossed ${pivot:.6f} (high ${h:.6f}) but body closed back BELOW pivot (close ${c:.6f}). Upper wick = {upper_wick_pct:.0f}% of bar range."
        if wicked_through and not body_above:
            return f"~ MARGINAL: wick crossed ${pivot:.6f} but body straddles pivot (open ${o:.6f}, close ${c:.6f}). Upper wick = {upper_wick_pct:.0f}% of range."
        if body_above:
            return f"✓ CLEAN: body fully above ${pivot:.6f} (open ${o:.6f}, close ${c:.6f}). No sweep evidence."
        return f"? UNCLEAR: bar h=${h:.6f} l=${l:.6f} o=${o:.6f} c=${c:.6f} pivot=${pivot:.6f}"
    # short side: pivot is the swing low
    wicked_through = l < pivot
    body_above = min(o, c) > pivot
    body_below = max(o, c) < pivot
    lower_wick = min(o, c) - l
    lower_wick_pct = lower_wick / rng * 100.0
    if wicked_through and body_above:
        return f"⚠️ SWEEP: wick crossed ${pivot:.6f} (low ${l:.6f}) but body closed back ABOVE pivot (close ${c:.6f}). Lower wick = {lower_wick_pct:.0f}% of bar range."
    if wicked_through and not body_below:
        return f"~ MARGINAL: wick crossed ${pivot:.6f} but body straddles pivot (open ${o:.6f}, close ${c:.6f}). Lower wick = {lower_wick_pct:.0f}% of range."
    if body_below:
        return f"✓ CLEAN: body fully below ${pivot:.6f} (open ${o:.6f}, close ${c:.6f}). No sweep evidence."
    return f"? UNCLEAR: bar h=${h:.6f} l=${l:.6f} o=${o:.6f} c=${c:.6f} pivot=${pivot:.6f}"


def _format_signal_bundle(bundle: dict) -> str:
    """Render the structured signal bundle. Bundle is built by direction_adjudicator
    OR by the legacy call site (plan-based). Missing keys render as 'n/a'."""
    def _g(k, default="n/a"):
        v = bundle.get(k)
        return v if v is not None else default

    return (
        f"=== STRUCTURAL TRIGGER ===\n"
        f"frame: {_g('structure_type')} | direction: {_g('structure_direction')} | pivot: ${_g('breakout_level')}\n"
        f"current price: ${_g('current_price')} | distance past pivot: {_g('distance_past_pivot_pct')}%\n"
        f"1h range vs median: {_g('range_ratio_1h')}× | 15m range vs median: {_g('range_ratio_15m')}×\n\n"
        f"=== CURRENT 1h BAR WICK ANALYSIS ===\n"
        f"{_g('wick_1h', '(no data)')}\n\n"
        f"=== CURRENT 15m BAR WICK ANALYSIS ===\n"
        f"{_g('wick_15m', '(no data)')}\n\n"
        f"=== ORDER BOOK (live, top 10 levels) ===\n"
        f"bid: ${_g('book_bid_usd')} | ask: ${_g('book_ask_usd')} | ratio (bid/ask): {_g('book_ratio')}\n"
        f"sentiment label: {_g('book_sentiment')}\n\n"
        f"=== OI / FUNDING / VOLUME ===\n"
        f"OI now: ${_g('oi_usd')} | OI 1h delta: {_g('oi_delta_pct')}%\n"
        f"Funding 1h: {_g('funding_pct')}%\n"
        f"Current bar volume vs 30d median: {_g('volume_ratio')}× | volume_z: {_g('volume_z')}\n\n"
        f"=== HTF / MACRO ===\n"
        f"7d trend aligned with structure: {_g('htf_aligned')}\n"
        f"BTC 24h: {_g('btc_24h_pct')}%\n\n"
        f"=== TIMING ===\n"
        f"tier: {_g('tier')} | watchlist age: {_g('watchlist_age_hours')}h | utc hour: {_g('utc_hour')}\n"
    )


def _build_user_prompt(
    market: Market,
    classifier_result: Any,
    signal_bundle: dict,
    bar_history: list,
    bars_15m: list | None,
    news_items: list[NewsItem],
    prior_alerts: list[dict],
) -> str:
    """Compose the user-prompt. The signal_bundle contains the structured numeric
    signals; this function adds prose context (news, classifier, OHLCV tables)."""

    # Classifier (stage 1) block
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
        f"{_format_signal_bundle(signal_bundle)}\n"
        f"=== STAGE 1 CLASSIFIER ===\n{cls_block}\n\n"
        f"=== NEWS BUNDLE ===\n{_format_news_bundle(news_items)}\n\n"
        f"=== RECENT 48h 1h OHLCV ===\n{_format_ohlc_table(bar_history, config.STAGE2_BAR_HISTORY_HOURS)}\n\n"
        f"=== LAST 8 15m BARS ===\n{_format_15m_tail(bars_15m, n=8)}\n\n"
        f"=== RECENT ALERTS ON THIS TICKER (past 7d) ===\n{_format_prior_alerts(prior_alerts)}\n\n"
        f"Call the decide_direction tool. Be honest about ambiguity — use 'no_trade' "
        f"liberally when signals split. Justify every flip in the thesis with concrete signals."
    )


# ============ Gemini call ============

def analyze(
    market: Market,
    classifier_result: Any,
    signal_bundle: dict,
    bar_history: list,
    bars_15m: list | None,
    news_items: list[NewsItem],
    prior_alerts: list[dict] | None = None,
) -> PredictorResult | None:
    """Run the direction adjudicator. Returns ``None`` only on infrastructure
    failure (no key, library missing) — callers fall back to the structural
    direction in that case. API errors return ``None`` too; the alert path
    must never crash on a predictor failure.
    """
    if not config.STAGE2_ENABLED and not config.DIRECTION_ADJUDICATOR_ENABLED:
        return None
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log.warning("predictor: GEMINI_API_KEY not set — skipping adjudication")
        return None
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.warning("predictor: requests unavailable: %s", e)
        return None

    user_prompt = _build_user_prompt(
        market, classifier_result, signal_bundle or {},
        bar_history or [], bars_15m,
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
                "allowedFunctionNames": ["decide_direction"],
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
            if fc and fc.get("name") == "decide_direction":
                args = fc.get("args") or {}
                break
        if args is not None:
            break
    if args is None:
        log.warning("predictor: no functionCall in response for %s", market.ticker)
        return None

    try:
        direction = str(args.get("final_direction", "")).lower().strip()
        if direction not in VALID_DIRECTIONS:
            # If the LLM returns garbage, fall back to the structural direction
            # (preserves the v3 no-suppression invariant: the alert still fires).
            direction = (signal_bundle or {}).get("structure_direction") or "no_trade"
        # Honor config flags
        if direction == "no_trade" and not config.DIR_ALLOW_NO_TRADE:
            direction = (signal_bundle or {}).get("structure_direction") or "long"
        if config.DIR_ALLOW_FLIP is False:
            sdir = (signal_bundle or {}).get("structure_direction")
            if sdir in ("long", "short") and direction in ("long", "short") and direction != sdir:
                direction = sdir
        verdict = str(args.get("verdict", "ALERT_NOW")).upper()
        if verdict not in VALID_VERDICTS:
            verdict = "ALERT_NOW"
        horizon = str(args.get("expected_horizon", "intraday"))
        if horizon not in VALID_HORIZONS:
            horizon = "intraday"
        return PredictorResult(
            final_direction=direction,
            direction_confidence=max(0.0, min(1.0, float(args.get("direction_confidence", 0.5)))),
            setup_quality=max(0.0, min(1.0, float(args.get("setup_quality", 0.5)))),
            thesis=str(args.get("thesis", ""))[:1500],
            kill_signal=str(args.get("kill_signal", ""))[:500],
            expected_horizon=horizon,
            expected_r_multiple=float(args.get("expected_r_multiple", 1.5)),
            entry_guidance=str(args.get("entry_guidance", "market"))[:200],
            verdict=verdict,
            risks=[str(r)[:200] for r in (args.get("risks") or [])][:3],
        )
    except (TypeError, ValueError) as e:
        log.warning("predictor: parsing failed for %s: %s", market.ticker, e)
        return None
