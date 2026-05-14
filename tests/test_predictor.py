"""Predictor (radar/predictor.py) tests.

We never touch the real Gemini API — the HTTP call is monkey-patched. These
tests pin the request shape (model, tool schema, thinking budget), the
response parser (direction enum, verdict clamps, horizon enum), and the
failure modes (returns None on infrastructure error so caller can fall back
to the structural direction).
"""

from __future__ import annotations

from types import SimpleNamespace
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


def _stub_response(args: dict, name: str = "decide_direction"):
    return {
        "candidates": [
            {"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}},
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
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return fake_requests


def _good_args(**overrides):
    base = {
        "final_direction": "long",
        "verdict": "ALERT_NOW",
        "direction_confidence": 0.82,
        "setup_quality": 0.78,
        "thesis": "Clean 4h break above $1.71 with confirming volume. ETF flows positive.",
        "kill_signal": "Below $1.71 — breakout fails and structure reverses.",
        "expected_horizon": "1-3_days",
        "expected_r_multiple": 3.0,
        "entry_guidance": "Pullback to $1.78",
        "risks": ["BTC could reverse", "ETF flows could cool", "Token unlock in 5d"],
    }
    base.update(overrides)
    return base


def test_predictor_returns_none_when_both_flags_off(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", False)
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", False)
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_parses_valid_long_response(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_response(_good_args()))
    result = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert result is not None
    assert result.final_direction == "long"
    assert result.direction_confidence == 0.82
    assert result.verdict == "ALERT_NOW"
    assert result.expected_horizon == "1-3_days"
    assert len(result.risks) == 3


def test_predictor_accepts_no_trade(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.setattr(config, "DIR_ALLOW_NO_TRADE", True)
    _patch_post(monkeypatch, _stub_response(_good_args(final_direction="no_trade")))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "no_trade"


def test_predictor_accepts_flip_when_allowed(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.setattr(config, "DIR_ALLOW_FLIP", True)
    _patch_post(monkeypatch, _stub_response(_good_args(final_direction="short")))
    r = predictor.analyze(_market(), None, _bundle(structure_direction="long"),
                          _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "short"


def test_predictor_blocks_flip_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.setattr(config, "DIR_ALLOW_FLIP", False)
    _patch_post(monkeypatch, _stub_response(_good_args(final_direction="short")))
    r = predictor.analyze(_market(), None, _bundle(structure_direction="long"),
                          _bars(), [], [], [])
    assert r is not None
    # Flip blocked → falls back to structural direction
    assert r.final_direction == "long"


def test_predictor_falls_back_to_structural_on_garbage_direction(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_response(_good_args(final_direction="moonshot")))
    r = predictor.analyze(_market(), None, _bundle(structure_direction="short"),
                          _bars(), [], [], [])
    assert r is not None
    assert r.final_direction == "short"


def test_predictor_clamps_confidence_to_unit_range(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_response(
        _good_args(direction_confidence=1.7, setup_quality=-0.4)
    ))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert r.direction_confidence == 1.0
    assert r.setup_quality == 0.0


def test_predictor_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, {}, status=500)
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_returns_none_when_no_function_call(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    assert predictor.analyze(_market(), None, _bundle(), _bars(), [], [], []) is None


def test_predictor_request_pins_tool_schema(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    fake = _patch_post(monkeypatch, _stub_response(_good_args()))
    predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    _, kwargs = fake.post.call_args
    body = kwargs["json"]
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == config.STAGE2_THINKING_BUDGET
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert body["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == ["decide_direction"]


def test_predictor_truncates_long_thesis(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, _stub_response(
        _good_args(thesis="x" * 5000, kill_signal="y" * 1000, entry_guidance="z" * 500)
    ))
    r = predictor.analyze(_market(), None, _bundle(), _bars(), [], [], [])
    assert r is not None
    assert len(r.thesis) <= 1500
    assert len(r.kill_signal) <= 500
    assert len(r.entry_guidance) <= 200
