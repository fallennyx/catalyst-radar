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
- FLIP (long structure → short call, or vice versa) ONLY when (a) sweep evidence is clear AND (b) at least 2 other signals point the opposite way. Flipping is rare and you must justify it concretely in the reason.
- NO_TRADE when signals are genuinely split — e.g. structure says long, catalyst says short, book is neutral, OI is mixed. Don't force a call.
- confidence must reflect reality. If you are 60% sure of long, output 60, not 90. The user sizes off this number.

OUTPUT FORMAT
Respond with a JSON object — and ONLY a JSON object, no surrounding prose, no code fences. Exactly these keys:

{
  "direction": "long" | "short" | "no_trade",
  "confidence": <integer 0-100>,
  "reason": "<2-4 sentences in plain English. Reference the specific signals you weighed (sweep, OI, book, catalyst, HTF, BTC). Name the invalidation price. Talk like a colleague. If you chose no_trade or flipped vs structure, the reason MUST justify why.>"
}

- direction MUST be one of the three strings above (lowercase). Anything else is a hard error.
- confidence is an INTEGER on the 0-100 scale (not 0-1). 80+ = strong, 50-79 = OK, 30-49 = tentative, <30 = no_trade territory.
- reason is required and must be non-empty.
- Output nothing else — no preamble, no markdown, just the JSON object."""


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
        f"Respond with the JSON object specified in the system prompt — keys: "
        f"direction, confidence (0-100 integer), reason. No code fences, no prose around it. "
        f"Be honest about ambiguity — use 'no_trade' liberally when signals split. Justify "
        f"every flip in the reason with concrete signals."
    )


# ============ Groq call (OpenAI-compatible JSON mode) ============

def _parse_json_payload(content: str) -> dict | None:
    """Extract a JSON object from the LLM response. JSON mode is strict on Groq,
    but be defensive — strip code fences and lead-in prose just in case."""
    if not content:
        return None
    s = content.strip()
    # Strip ```json ... ``` fences if the model added them despite JSON mode.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    # Slice to the outermost { } if there's any leading/trailing text.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    blob = s[start:end + 1]
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def analyze(
    market: Market,
    classifier_result: Any,
    signal_bundle: dict,
    bar_history: list,
    bars_15m: list | None,
    news_items: list[NewsItem],
    prior_alerts: list[dict] | None = None,
) -> PredictorResult | None:
    """Run the direction adjudicator via Groq + Llama 3.3 70B (JSON mode).

    Returns ``None`` only on infrastructure failure (no key, library missing,
    HTTP error, malformed JSON) — callers fall back to the structural direction
    in that case. The alert path must never crash on a predictor failure.
    """
    if not config.STAGE2_ENABLED and not config.DIRECTION_ADJUDICATOR_ENABLED:
        return None
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log.warning("predictor: GROQ_API_KEY not set — skipping adjudication")
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

    url = f"{config.GROQ_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.STAGE2_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": config.STAGE2_TEMPERATURE,
        "max_tokens": config.STAGE2_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    try:
        r = requests.post(url, headers=headers, json=body, timeout=config.GROQ_HTTP_TIMEOUT)
    except Exception as e:
        log.warning("predictor: Groq call failed for %s: %s", market.ticker, e)
        return None
    if r.status_code != 200:
        log.warning("predictor: Groq returned %d for %s: %s",
                    r.status_code, market.ticker, r.text[:200])
        return None
    try:
        payload = r.json()
    except ValueError:
        log.warning("predictor: Groq returned non-JSON envelope for %s", market.ticker)
        return None

    try:
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except (AttributeError, IndexError, TypeError):
        content = ""
    args = _parse_json_payload(content)
    if args is None:
        log.warning("predictor: could not parse JSON content for %s: %r",
                    market.ticker, (content or "")[:200])
        return None

    try:
        direction = str(args.get("direction", "")).lower().strip()
        if direction not in VALID_DIRECTIONS:
            # Garbage direction → fall back to structural (preserves v3
            # no-suppression invariant: alert still fires).
            direction = (signal_bundle or {}).get("structure_direction") or "no_trade"
        if direction == "no_trade" and not config.DIR_ALLOW_NO_TRADE:
            direction = (signal_bundle or {}).get("structure_direction") or "long"
        if config.DIR_ALLOW_FLIP is False:
            sdir = (signal_bundle or {}).get("structure_direction")
            if sdir in ("long", "short") and direction in ("long", "short") and direction != sdir:
                direction = sdir

        # Confidence: accept 0-100 (preferred) or 0-1 (defensive). Normalize → [0,1].
        raw_conf = args.get("confidence", 50)
        try:
            cf = float(raw_conf)
        except (TypeError, ValueError):
            cf = 50.0
        if cf > 1.0:
            cf = cf / 100.0
        confidence = max(0.0, min(1.0, cf))

        reason = str(args.get("reason", "") or "").strip()[:1500]

        return PredictorResult(
            final_direction=direction,
            direction_confidence=confidence,
            setup_quality=confidence,           # collapsed schema — single conviction value
            thesis=reason,
            kill_signal="",
            expected_horizon="intraday",
            expected_r_multiple=1.5,
            entry_guidance="market" if direction in ("long", "short") else "no trade — wait",
            verdict="ALERT_NOW",
            risks=[],
        )
    except (TypeError, ValueError) as e:
        log.warning("predictor: parsing failed for %s: %s", market.ticker, e)
        return None
