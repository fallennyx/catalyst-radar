"""Stage 2 reasoner (radar/predictor.py) tests.

We never touch the real Gemini API — the HTTP call is monkey-patched. These
tests pin the request shape (model, tool schema, thinking budget), the
response parser (verdict clamps, horizon enum), and the failure modes
(returns None on infrastructure error so caller treats as ALERT_NOW).
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


def _plan():
    return SimpleNamespace(
        direction="long", entry=1.7880, stop=1.7106,
        tp1=1.9041, tp2=2.0203, risk_per_unit=0.0774,
        r_multiple_tp1=1.5, r_multiple_tp2=3.0,
        trail_atr=0.0494,
    )


def _bars(n: int = 60):
    return [
        storage.Bar(
            ticker="PENDLE", ts=i * 3600,
            open=1.7, high=1.71, low=1.69, close=1.7,
            volume=10_000, oi=0, funding=0,
        )
        for i in range(n)
    ]


def _stub_response(args: dict, name: str = "analyze_setup"):
    return {
        "candidates": [
            {"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}},
        ]
    }


def _patch_post(monkeypatch, response_json: dict, status: int = 200):
    """Replace requests.post inside the predictor module."""
    fake_resp = MagicMock()
    fake_resp.status_code = status
    fake_resp.json.return_value = response_json
    fake_resp.text = "(fake)"

    fake_requests = MagicMock()
    fake_requests.post.return_value = fake_resp

    # The predictor module imports `requests` lazily inside `analyze`. We patch
    # sys.modules so the local import inside the function picks up our stub.
    import sys
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return fake_requests


def test_predictor_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", False)
    assert predictor.analyze(_market(), None, _plan(), {}, _bars(), [], []) is None


def test_predictor_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert predictor.analyze(_market(), None, _plan(), {}, _bars(), [], []) is None


def test_predictor_parses_valid_response(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    args = {
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
    _patch_post(monkeypatch, _stub_response(args))
    result = predictor.analyze(_market(), None, _plan(), {"breakout_level": 1.71}, _bars(), [], [])
    assert result is not None
    assert result.verdict == "ALERT_NOW"
    assert result.direction_confidence == 0.82
    assert result.expected_horizon == "1-3_days"
    assert result.expected_r_multiple == 3.0
    assert len(result.risks) == 3


def test_predictor_clamps_invalid_verdict_to_alert_now(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    args = {
        "verdict": "MAYBE",          # not in enum
        "direction_confidence": 0.5,
        "setup_quality": 0.5,
        "thesis": "x",
        "kill_signal": "y",
        "expected_horizon": "intraday",
        "expected_r_multiple": 2.0,
        "entry_guidance": "market",
    }
    _patch_post(monkeypatch, _stub_response(args))
    r = predictor.analyze(_market(), None, _plan(), {}, _bars(), [], [])
    assert r is not None
    assert r.verdict == "ALERT_NOW"


def test_predictor_clamps_confidence_to_unit_range(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    args = {
        "verdict": "ALERT_NOW",
        "direction_confidence": 1.7,    # out of [0,1]
        "setup_quality": -0.4,
        "thesis": "x", "kill_signal": "y",
        "expected_horizon": "intraday",
        "expected_r_multiple": 1.5,
        "entry_guidance": "market",
    }
    _patch_post(monkeypatch, _stub_response(args))
    r = predictor.analyze(_market(), None, _plan(), {}, _bars(), [], [])
    assert r is not None
    assert r.direction_confidence == 1.0
    assert r.setup_quality == 0.0


def test_predictor_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    _patch_post(monkeypatch, {}, status=500)
    assert predictor.analyze(_market(), None, _plan(), {}, _bars(), [], []) is None


def test_predictor_returns_none_when_no_function_call(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    # Response has candidates but no functionCall
    _patch_post(monkeypatch, {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]})
    assert predictor.analyze(_market(), None, _plan(), {}, _bars(), [], []) is None


def test_predictor_request_includes_thinking_budget(monkeypatch):
    """Stage 2 reasoning hinges on Gemini's thinking budget — verify it's sent."""
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    args = {
        "verdict": "ALERT_NOW", "direction_confidence": 0.7, "setup_quality": 0.7,
        "thesis": "x", "kill_signal": "y", "expected_horizon": "intraday",
        "expected_r_multiple": 2.0, "entry_guidance": "market",
    }
    fake_requests = _patch_post(monkeypatch, _stub_response(args))
    predictor.analyze(_market(), None, _plan(), {}, _bars(), [], [])
    _, kwargs = fake_requests.post.call_args
    body = kwargs["json"]
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == config.STAGE2_THINKING_BUDGET
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert body["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == ["analyze_setup"]


def test_predictor_truncates_long_thesis(monkeypatch):
    monkeypatch.setattr(config, "STAGE2_ENABLED", True)
    long_thesis = "x" * 5000
    args = {
        "verdict": "ALERT_NOW", "direction_confidence": 0.7, "setup_quality": 0.7,
        "thesis": long_thesis, "kill_signal": "y" * 1000,
        "expected_horizon": "intraday",
        "expected_r_multiple": 2.0, "entry_guidance": "z" * 500,
    }
    _patch_post(monkeypatch, _stub_response(args))
    r = predictor.analyze(_market(), None, _plan(), {}, _bars(), [], [])
    assert r is not None
    assert len(r.thesis) <= 1500
    assert len(r.kill_signal) <= 500
    assert len(r.entry_guidance) <= 200
