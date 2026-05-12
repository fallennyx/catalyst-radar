"""Catalyst classifier.

Calls Claude Haiku with `tool_choice` to force a structured-output JSON payload,
parses it through Pydantic, then runs a substring validator: every quoted
evidence string must appear verbatim in the news bundle. Any classifier output
that fails the validator is dropped.

Public API:
    classify(market, news_items) -> ClassifierResult | None
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import config
from .catalysts import NewsItem
from .universe import Market

log = logging.getLogger(__name__)


# ============ schema ============

VALID_CATALYST_TYPES = {
    "earnings",
    "guidance",
    "regulatory",
    "macro",
    "tokenomics",
    "partnership",
    "exchange_listing",
    "exploit_or_outage",
    "etf_or_fund_flow",
    "geopolitics",
    "technical_breakout",
    "rumor",
    "none",
}

VALID_DIRECTIONS = {"long", "short", "neutral"}


class ClassifierResult(BaseModel):
    catalyst_type: str = Field(...)
    direction: str = Field(...)
    confidence: float = Field(..., ge=0.0, le=1.0)
    summary: str = Field(..., max_length=400)
    evidence_quotes: list[str] = Field(default_factory=list)
    is_actionable: bool = True

    # ---- BOS filter additions (all optional, sensible defaults) ----
    # The Haiku classifier doesn't necessarily emit these yet. They exist so
    # downstream consumers (telegram, suppression) can read them uniformly.
    alert_priority: str = "NORMAL"      # "NORMAL" | "SUPPRESS" | "WATCHLIST"
    primary_catalyst: str | None = None  # plain-English headline. defaults to summary
    conviction: float | None = None      # [0, 1]. defaults to confidence
    horizon: str = "unknown"             # "intraday" | "swing" | "multiweek" | "unknown"
    continuation_thesis: str | None = None
    kill_signal: str = ""                # what would invalidate the thesis

    @field_validator("catalyst_type")
    @classmethod
    def _ct(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in VALID_CATALYST_TYPES:
            return "none"
        return v

    @field_validator("direction")
    @classmethod
    def _dir(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in VALID_DIRECTIONS:
            return "neutral"
        return v

    @model_validator(mode="after")
    def _fill_bos_defaults(self):
        """Default BOS fields from the legacy fields when they're not supplied."""
        if not self.primary_catalyst:
            self.primary_catalyst = self.summary
        if self.conviction is None:
            self.conviction = self.confidence
        if not self.continuation_thesis:
            self.continuation_thesis = self.summary
        return self


# ============ tool spec for Anthropic ============

CLASSIFY_TOOL = {
    "name": "record_catalyst",
    "description": (
        "Record a structured catalyst classification for the candidate ticker. "
        "Use ONLY information present in the supplied news bundle. Each evidence "
        "quote MUST be an exact verbatim substring of one news item's title or body."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "catalyst_type": {
                "type": "string",
                "enum": sorted(VALID_CATALYST_TYPES),
                "description": "Best-fit category. Use 'none' if there is no clear catalyst.",
            },
            "direction": {
                "type": "string",
                "enum": sorted(VALID_DIRECTIONS),
                "description": "Implied directional bias of the catalyst.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Model confidence in the classification (0–1).",
            },
            "summary": {
                "type": "string",
                "maxLength": 400,
                "description": "One-sentence plain-English explanation, no jargon.",
            },
            "evidence_quotes": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 3,
                "description": (
                    "Up to 3 verbatim short quotes from the news bundle that "
                    "support the classification. Each must appear EXACTLY in the input."
                ),
            },
            "is_actionable": {
                "type": "boolean",
                "description": "False if the catalyst is rumor-only, expired, or already priced in.",
            },
        },
        "required": ["catalyst_type", "direction", "confidence", "summary", "is_actionable"],
    },
}


# ============ prompt construction ============

_SYSTEM_PROMPT = """You are a sober, skeptical macro/markets analyst.

Your only job is to look at a small bundle of news items about a ticker that
just made an unusual price move and call ONE structured catalyst.

Hard rules:
1. You may ONLY use information from the news bundle. No outside knowledge.
2. Every evidence_quote MUST be a verbatim substring of one of the news items.
   Never paraphrase, never combine words from different items into one quote.
3. If nothing in the bundle plausibly explains the move, return catalyst_type="none",
   direction="neutral", is_actionable=false.
4. Prefer "none" over guessing. Bad classifications cost more than missed ones.
5. Use the record_catalyst tool. Do not respond in plain prose.
"""

_FEW_SHOTS = [
    {
        "ticker": "NVDA",
        "asset_class": "equity",
        "news": [
            {
                "title": "NVIDIA reports record Q3 revenue, beats estimates",
                "body": "NVIDIA Corporation announced fiscal Q3 revenue of $35.1B, exceeding analyst estimates of $33.2B, driven by data-center demand.",
            }
        ],
        "expected": {
            "catalyst_type": "earnings",
            "direction": "long",
            "confidence": 0.88,
            "summary": "NVIDIA reported a Q3 revenue beat driven by data-center demand.",
            "evidence_quotes": ["NVIDIA Corporation announced fiscal Q3 revenue of $35.1B, exceeding analyst estimates"],
            "is_actionable": True,
        },
    },
    {
        "ticker": "BTC",
        "asset_class": "crypto_t1",
        "news": [
            {
                "title": "Markets quiet ahead of Fed meeting",
                "body": "Trading volumes were muted across major risk assets.",
            }
        ],
        "expected": {
            "catalyst_type": "none",
            "direction": "neutral",
            "confidence": 0.2,
            "summary": "No specific BTC catalyst found; news bundle is generic.",
            "evidence_quotes": [],
            "is_actionable": False,
        },
    },
    {
        "ticker": "ARB",
        "asset_class": "crypto_t2",
        "news": [
            {
                "title": "ARB token unlock scheduled 2026-05-16",
                "body": "Approximately 92.65M ARB tokens (~2.5% of supply) unlock on May 16.",
            }
        ],
        "expected": {
            "catalyst_type": "tokenomics",
            "direction": "short",
            "confidence": 0.7,
            "summary": "Large ARB token unlock incoming, increasing near-term sell pressure.",
            "evidence_quotes": ["Approximately 92.65M ARB tokens (~2.5% of supply) unlock on May 16."],
            "is_actionable": True,
        },
    },
]


def _format_news_bundle(items: Iterable[NewsItem]) -> str:
    chunks = []
    for i, n in enumerate(items, 1):
        chunks.append(f"[{i}] SOURCE: {n.source}\nTITLE: {n.title}\nBODY: {n.body}\nURL: {n.url}")
    return "\n\n".join(chunks) if chunks else "(no news items found)"


def _build_user_prompt(market: Market, items: list[NewsItem]) -> str:
    examples = []
    for ex in _FEW_SHOTS:
        news_block = "\n".join(f"- {n['title']}: {n['body']}" for n in ex["news"])
        examples.append(
            f"EXAMPLE\nTICKER: {ex['ticker']} ({ex['asset_class']})\n"
            f"NEWS:\n{news_block}\n"
            f"CALL: {json.dumps(ex['expected'])}\n"
        )
    examples_block = "\n".join(examples)

    return (
        f"{examples_block}\n"
        f"=== ACTUAL TASK ===\n"
        f"TICKER: {market.ticker} (asset_class={market.asset_class})\n"
        f"24h price change: {market.pct_24h:+.2f}%\n"
        f"NEWS BUNDLE:\n{_format_news_bundle(items)}\n\n"
        f"Now classify using the record_catalyst tool."
    )


# ============ Anthropic call ============

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — classifier will return None")
        return None
    try:
        from anthropic import Anthropic  # type: ignore
    except Exception as e:
        log.warning("anthropic SDK unavailable: %s", e)
        return None
    _CLIENT = Anthropic()
    return _CLIENT


def _extract_tool_input(response: Any) -> dict[str, Any] | None:
    content = getattr(response, "content", None) or []
    for block in content:
        block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if block_type == "tool_use":
            inp = getattr(block, "input", None)
            if inp is None and isinstance(block, dict):
                inp = block.get("input")
            if isinstance(inp, dict):
                return inp
    return None


# ============ Gemini call (REST, no SDK) ============

# Mirror of CLASSIFY_TOOL into Gemini's function-declaration schema. Gemini
# accepts an OpenAPI subset; the 'enum' / 'maxLength' / 'minimum' constraints
# from CLASSIFY_TOOL transfer 1:1.
_GEMINI_FUNCTION_DECL = {
    "name": "record_catalyst",
    "description": (
        "Record a structured catalyst classification for the candidate ticker. "
        "Use ONLY information present in the supplied news bundle. Each evidence "
        "quote MUST be an exact verbatim substring of one news item's title or body."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "catalyst_type": {
                "type": "string",
                "enum": sorted(VALID_CATALYST_TYPES),
                "description": "Best-fit category. Use 'none' if there is no clear catalyst.",
            },
            "direction": {
                "type": "string",
                "enum": ["long", "neutral", "short"],
                "description": "Implied directional bias of the catalyst.",
            },
            "confidence": {
                "type": "number",
                "description": "Model confidence in the classification (0–1).",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence plain-English explanation, no jargon. Max 400 chars.",
            },
            "evidence_quotes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Up to 3 verbatim short quotes from the news bundle.",
            },
            "is_actionable": {
                "type": "boolean",
                "description": "False if the catalyst is rumor-only, expired, or already priced in.",
            },
        },
        "required": ["catalyst_type", "direction", "confidence", "summary", "is_actionable"],
    },
}


def _classify_gemini(market: Market, news_items: list[NewsItem]) -> ClassifierResult | None:
    """Call Gemini 2.5 Flash via REST. No SDK, no new dependency."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — classifier will return None")
        return None
    try:
        import requests  # noqa: WPS433
    except Exception as e:
        log.warning("requests unavailable for Gemini call: %s", e)
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={api_key}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": _build_user_prompt(market, news_items)}],
        }],
        "tools": [{"functionDeclarations": [_GEMINI_FUNCTION_DECL]}],
        "toolConfig": {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": ["record_catalyst"],
            }
        },
        "generationConfig": {
            "temperature": config.GEMINI_TEMPERATURE,
            "maxOutputTokens": config.GEMINI_MAX_TOKENS,
        },
    }

    try:
        r = requests.post(url, json=body, timeout=20)
    except Exception as e:
        log.warning("Gemini call failed for %s: %s", market.ticker, e)
        return None
    if r.status_code != 200:
        log.warning("Gemini returned %d for %s: %s", r.status_code, market.ticker, r.text[:200])
        return None
    try:
        payload = r.json()
    except ValueError:
        log.warning("Gemini returned non-JSON for %s", market.ticker)
        return None

    # Gemini surfaces function calls inside candidates[0].content.parts[*].functionCall
    candidates = payload.get("candidates") or []
    args: dict | None = None
    for cand in candidates:
        for part in (cand.get("content") or {}).get("parts") or []:
            fc = part.get("functionCall")
            if fc and fc.get("name") == "record_catalyst":
                args = fc.get("args") or {}
                break
        if args is not None:
            break
    if args is None:
        log.warning("Gemini: no functionCall in response for %s", market.ticker)
        return None

    try:
        result = ClassifierResult(**args)
    except ValidationError as e:
        log.warning("Gemini classifier: pydantic validation failed for %s: %s", market.ticker, e)
        return None

    corpus = _build_news_corpus(news_items)
    if not _validate_quotes(result.evidence_quotes, corpus):
        log.info("Gemini classifier: dropped %s — fabricated evidence quote", market.ticker)
        return None
    return result


# ============ substring validator ============

def _build_news_corpus(items: Iterable[NewsItem]) -> str:
    parts = []
    for n in items:
        parts.append(n.title or "")
        parts.append(n.body or "")
    return "\n".join(parts)


def _validate_quotes(quotes: list[str], corpus: str) -> bool:
    """Every quote must be a non-empty verbatim substring of the corpus."""
    if not quotes:
        return True  # zero quotes is fine — model said "none" or had no support
    for q in quotes:
        if not q or not q.strip():
            return False
        if q.strip() not in corpus:
            return False
    return True


# ============ public entrypoint ============

def classify(market: Market, news_items: list[NewsItem]) -> ClassifierResult | None:
    # v3: no early return on empty news. Niche tickers (FF, USELESS, STABLE,
    # NATGAS, …) rarely surface in mainstream news sources, but their
    # structural breakouts are the most actionable. The classifier still runs
    # — with `_format_news_bundle` rendering "(no news items found)" so the
    # LLM has explicit context — and downstream policy never blocks the alert
    # based on the LLM's verdict. Whatever the LLM returns (or None on error)
    # is folded into the alert body as commentary, not as a gate.

    # Route by configured provider. Gemini is the cheapest-tier default;
    # Anthropic Haiku is the no-key fallback / opt-in path.
    if config.LLM_PROVIDER == "gemini":
        return _classify_gemini(market, news_items)

    # ---- Anthropic Haiku path (legacy / fallback) ----
    client = _client()
    if client is None:
        return None

    user_prompt = _build_user_prompt(market, news_items)

    try:
        response = client.messages.create(
            model=config.HAIKU_MODEL,
            max_tokens=config.HAIKU_MAX_TOKENS,
            temperature=config.HAIKU_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            tools=[CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "record_catalyst"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        log.warning("Haiku call failed for %s: %s", market.ticker, e)
        return None

    payload = _extract_tool_input(response)
    if payload is None:
        log.warning("classifier: no tool_use block in response for %s", market.ticker)
        return None

    try:
        result = ClassifierResult(**payload)
    except ValidationError as e:
        log.warning("classifier: pydantic validation failed for %s: %s", market.ticker, e)
        return None

    corpus = _build_news_corpus(news_items)
    if not _validate_quotes(result.evidence_quotes, corpus):
        log.info("classifier: dropped %s — fabricated evidence quote", market.ticker)
        return None

    return result
