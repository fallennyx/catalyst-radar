"""Predictor (radar/predictor.py) tests.

We never touch the real Groq API — the HTTP call is monkey-patched. These
tests pin the request shape (model, JSON-mode response_format, bearer auth),
the response parser (direction enum, confidence scale normalization, JSON
extraction), and the failure modes (returns None on infrastructure error so
the caller falls back to the structural direction).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from radar import config, predictor, storage
from radar.universe import Market


def _market():
    return Market(
        ticker="PENDLE", asset_class="crypto_t2", market_id="PENDLE",
        max_leverage=10.0, price=1.7880, pct_24h=8.0,
    )


def _bundle(**overrides) -> dict:
    base = {
        "structure_direction": "long",
        "structure_type": "4h",
        "breakout_level": "1.710000",
        "current_price": "1.788000",
    }
    base.update(overrides)
    return base


def _bars(n: int = 60):
    return [
        storage.Bar(
            ticker="PENDLE", ts=i * 3600,
            open=1.7, high=1.71, low=1.69, close=1.7,
            volume=10_000, oi=0, funding=0,
        )
        for i in range(n)
    ]


def _stub_envelope(args: dict) -> dict:
    """Groq chat-completion envelope: choices[0].message.content is a JSON string."""
    return {
        "choices": [
            {"message": {"content": json.dumps(args)}}
        ]
    }


def _patch_post(monkeypatch, response_json: dict, status: int = 200):
    fake_resp = MagicMock()
    fake_resp.status_code = status
    fake_resp.json.return_value = response_json
    fake_resp.text = "(fake)"

    fake_requests = MagicMock()
    fake_requests.post.return_value = fake_resp

    import sys
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    return fake_requests


def _good_args(**overrides):
    base = {
        "direction": "long",
        "confidence": 82,
        "reason": "Clean 4h break above $1.71 with confirming volume. ETF flows positive.",
    }
    base.update(overrides)
    return base


def test_predictor_returns_none_when_both_flags_off(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", False)
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", False)
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_parses_valid_long_response(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args()))
    result = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert result is not None
    assert result.final_direction == "long"
    assert result.direction_confidence == pytest.approx(0.82)
    assert result.verdict == "ALERT_NOW"
    assert result.expected_horizon == "intraday"
    assert result.thesis.startswith("Clean 4h break")


def test_predictor_accepts_no_trade(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.setattr(config, "DIR_ALLOW_NO_TRADE", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args(direction="no_trade")))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "no_trade"


def test_predictor_accepts_flip_when_allowed(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.setattr(config, "DIR_ALLOW_FLIP", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args(direction="short")))
    r = predictor.analyze(_market(), None, _bundle(structure_direction="long"),
                          _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "short"


def test_predictor_blocks_flip_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.setattr(config, "DIR_ALLOW_FLIP", False)
    _patch_post(monkeypatch, _stub_envelope(_good_args(direction="short")))
    r = predictor.analyze(_market(), None, _bundle(structure_direction="long"),
                          _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "long"


def test_predictor_falls_back_to_structural_on_garbage_direction(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args(direction="moonshot")))
    r = predictor.analyze(_market(), None, _bundle(structure_direction="short"),
                          _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "short"


def test_predictor_clamps_confidence_to_unit_range(monkeypatch):
    """confidence > 100 clamps to 1.0; negative clamps to 0.0."""
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args(confidence=170)))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.direction_confidence == 1.0

    _patch_post(monkeypatch, _stub_envelope(_good_args(confidence=-40)))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.direction_confidence == 0.0


def test_predictor_accepts_unit_scale_confidence(monkeypatch):
    """If the LLM returns 0-1 (despite the prompt asking 0-100), we keep it as-is."""
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args(confidence=0.6)))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.direction_confidence == pytest.approx(0.6)


def test_predictor_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, {}, status=500)
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_returns_none_on_unparseable_content(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "not json at all"}}]})
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_strips_code_fences(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    fenced = "```json\n" + json.dumps(_good_args()) + "\n```"
    _patch_post(monkeypatch, {"choices": [{"message": {"content": fenced}}]})
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "long"


def test_predictor_request_pins_groq_shape(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    fake = _patch_post(monkeypatch, _stub_envelope(_good_args()))
    predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    args, kwargs = fake.post.call_args
    url = args[0] if args else kwargs.get("url")
    body = kwargs["json"]
    headers = kwargs["headers"]
    assert url.endswith("/chat/completions")
    assert headers["Authorization"] == "Bearer test-key"
    assert body["model"] == config.STAGE2_MODEL
    assert body["response_format"] == {"type": "json_object"}
    assert any(m["role"] == "system" for m in body["messages"])
    assert any(m["role"] == "user" for m in body["messages"])


def test_predictor_truncates_long_reason(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_envelope(_good_args(reason="x" * 5000)))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert len(r.thesis) <= 1500
