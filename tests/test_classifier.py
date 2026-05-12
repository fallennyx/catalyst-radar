"""Classifier tests with a mocked Anthropic client.

We never reach the real API in tests — we patch radar.classifier._client to
return a stub whose .messages.create returns a fake response object.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from radar import classifier, config
from radar.catalysts import NewsItem
from radar.universe import Market


@pytest.fixture(autouse=True)
def _pin_anthropic_provider(monkeypatch):
    """These tests stub the Anthropic-style client. Force the legacy path."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")


def _market():
    return Market(
        ticker="NVDA",
        asset_class="equity",
        market_id="NVDA",
        max_leverage=20.0,
        price=900.0,
        volume_24h_usd=50_000_000,
        oi_usd=10_000_000,
        funding_1h=0.0,
        pct_24h=8.0,
    )


def _news():
    return [
        NewsItem(
            ticker="NVDA",
            source="Reuters",
            title="NVIDIA reports record Q3 revenue, beats estimates",
            body="NVIDIA Corporation announced fiscal Q3 revenue of $35.1B, exceeding analyst estimates of $33.2B.",
            url="https://example.com/nvda",
            published=1_700_000_000,
        ),
    ]


def _fake_response(payload: dict):
    """Build a MagicMock that mimics the anthropic SDK response shape."""
    block = SimpleNamespace(type="tool_use", name="record_catalyst", input=payload)
    return SimpleNamespace(content=[block])


def _stub_client_returning(payload: dict):
    fake = MagicMock()
    fake.messages.create.return_value = _fake_response(payload)
    return fake


# ---------- substring validator ----------

def test_validator_accepts_verbatim_quote():
    items = _news()
    corpus = classifier._build_news_corpus(items)
    quote = "NVIDIA Corporation announced fiscal Q3 revenue of $35.1B"
    assert classifier._validate_quotes([quote], corpus) is True


def test_validator_rejects_fabricated_quote():
    items = _news()
    corpus = classifier._build_news_corpus(items)
    fake_quote = "NVIDIA confirmed a $50B partnership with OpenAI"
    assert classifier._validate_quotes([fake_quote], corpus) is False


def test_validator_rejects_paraphrase():
    items = _news()
    corpus = classifier._build_news_corpus(items)
    paraphrase = "Nvidia's quarterly earnings exceeded Wall Street expectations"
    assert classifier._validate_quotes([paraphrase], corpus) is False


def test_validator_accepts_empty_quote_list():
    items = _news()
    corpus = classifier._build_news_corpus(items)
    assert classifier._validate_quotes([], corpus) is True


# ---------- end-to-end classify() with mocked client ----------

def test_classify_returns_result_with_valid_quotes(monkeypatch):
    payload = {
        "catalyst_type": "earnings",
        "direction": "long",
        "confidence": 0.9,
        "summary": "NVIDIA beat revenue estimates.",
        "evidence_quotes": ["NVIDIA Corporation announced fiscal Q3 revenue of $35.1B"],
        "is_actionable": True,
    }
    monkeypatch.setattr(classifier, "_client", lambda: _stub_client_returning(payload))
    result = classifier.classify(_market(), _news())
    assert result is not None
    assert result.catalyst_type == "earnings"
    assert result.direction == "long"
    assert result.confidence == 0.9


def test_classify_drops_fabricated_evidence(monkeypatch):
    """The substring validator must catch a hallucinated quote."""
    payload = {
        "catalyst_type": "partnership",
        "direction": "long",
        "confidence": 0.95,
        "summary": "NVIDIA announced a giant partnership.",
        "evidence_quotes": ["NVIDIA confirmed a $50B partnership with OpenAI"],
        "is_actionable": True,
    }
    monkeypatch.setattr(classifier, "_client", lambda: _stub_client_returning(payload))
    assert classifier.classify(_market(), _news()) is None


def test_classify_calls_llm_even_with_empty_news(monkeypatch):
    """v3 behavior: empty news no longer short-circuits — the classifier
    sends a structural-only prompt to the LLM so niche-ticker breakouts
    (FF, USELESS, STABLE) still get LLM commentary in the alert body."""
    # Force Anthropic provider so we can spy on the client; default is gemini.
    monkeypatch.setattr(classifier.config, "LLM_PROVIDER", "anthropic")
    sentinel = MagicMock()
    payload = {
        "catalyst_type": "none", "direction": "neutral", "confidence": 0.2,
        "summary": "no clear news catalyst — structural break only",
        "evidence_quotes": [],
    }
    monkeypatch.setattr(classifier, "_client",
                        lambda: _stub_client_returning(payload))
    result = classifier.classify(_market(), [])
    # The LLM was called and returned a real result (not the synthetic
    # is_actionable=False placeholder of v2).
    assert result is not None
    assert result.catalyst_type == "none"


def test_classify_returns_none_when_client_unavailable(monkeypatch):
    monkeypatch.setattr(classifier, "_client", lambda: None)
    assert classifier.classify(_market(), _news()) is None


def test_classify_handles_invalid_payload(monkeypatch):
    payload = {
        # confidence > 1.0 → pydantic should reject
        "catalyst_type": "earnings",
        "direction": "long",
        "confidence": 9.9,
        "summary": "x",
        "evidence_quotes": [],
        "is_actionable": True,
    }
    monkeypatch.setattr(classifier, "_client", lambda: _stub_client_returning(payload))
    assert classifier.classify(_market(), _news()) is None


def test_classify_normalizes_unknown_catalyst_type(monkeypatch):
    payload = {
        "catalyst_type": "not_a_real_category",
        "direction": "long",
        "confidence": 0.5,
        "summary": "x",
        "evidence_quotes": [],
        "is_actionable": True,
    }
    monkeypatch.setattr(classifier, "_client", lambda: _stub_client_returning(payload))
    result = classifier.classify(_market(), _news())
    assert result is not None
    assert result.catalyst_type == "none"


def test_extract_tool_input_parses_dict_block():
    """The extractor must also handle the dict shape returned by some SDK versions."""
    raw = SimpleNamespace(content=[{"type": "tool_use", "input": {"x": 1}}])
    assert classifier._extract_tool_input(raw) == {"x": 1}
