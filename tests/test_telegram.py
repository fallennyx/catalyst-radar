"""Tests for telegram payload formatting.

We never hit the actual Telegram API here — we patch ``_send_main`` and
inspect the body it would have shipped.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from radar import telegram, trade_plan
from radar.classifier import ClassifierResult
from radar.universe import Market


def _market(ticker: str = "AMD", asset_class: str = "equity",
            price: float = 110.0) -> Market:
    return Market(
        ticker=ticker,
        asset_class=asset_class,
        market_id=ticker,
        max_leverage=10.0,
        price=price,
        volume_24h_usd=1_500_000,
        oi_usd=500_000,
        funding_1h=0.0001,
        pct_24h=4.5,
        pct_1h=1.2,
    )


def _classifier(direction: str = "long", horizon: str = "days") -> ClassifierResult:
    return ClassifierResult(
        catalyst_type="earnings",
        direction=direction,
        confidence=0.8,
        summary="Strong earnings beat",
        evidence_quotes=[],
        is_actionable=True,
        primary_catalyst="Strong earnings beat",
        conviction=0.8,
        horizon=horizon,
        continuation_thesis="Trend continuation expected",
        kill_signal="Loss of breakout level",
    )


def _capture_body() -> tuple[list[str], "patch._patch"]:
    """Patch ``_send_main`` to capture the payload string instead of sending."""
    sent: list[str] = []

    def fake_send(text: str, market_label: str = "?") -> bool:
        sent.append(text)
        return True

    return sent, patch.object(telegram, "_send_main", side_effect=fake_send)


# ============================================================================
# Plan: block — appears when a plan is provided
# ============================================================================

def test_alert_payload_contains_plan_block():
    market = _market(price=110.0)
    cls = _classifier(direction="long")
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.TradePlan(
        direction="long",
        entry=110.0,
        stop=99.8,
        tp1=125.3,
        tp2=140.6,
        risk_per_unit=10.2,
        r_multiple_tp1=1.5,
        r_multiple_tp2=3.0,
    )

    sent, p = _capture_body()
    with p:
        ok = telegram.send_bos_alert(market, cls, metadata, plan=plan)
    assert ok is True
    assert sent, "send_bos_alert never produced a body"
    body = sent[0]

    assert "Plan:" in body
    assert "LONG" in body
    # Entry/stop/TP1/TP2 prices appear in the body
    assert "110.00" in body
    assert "99.80" in body or "99.8" in body
    assert "125.30" in body or "125.3" in body
    assert "140.60" in body or "140.6" in body
    # R-multiples are rendered with one decimal + the trailing R
    assert "1.5R" in body
    assert "3.0R" in body
    # Risk amount is shown
    assert "Risk:" in body


def test_alert_payload_renders_short_plan():
    market = _market(price=90.0)
    cls = _classifier(direction="short")
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.TradePlan(
        direction="short",
        entry=90.0,
        stop=100.2,
        tp1=74.7,
        tp2=59.4,
        risk_per_unit=10.2,
        r_multiple_tp1=1.5,
        r_multiple_tp2=3.0,
    )

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=plan)
    body = sent[0]

    assert "Plan:" in body
    assert "SHORT" in body
    # Stop is above entry on a short
    assert "100.20" in body or "100.2" in body
    assert "74.70" in body or "74.7" in body


def test_alert_payload_omits_plan_block_when_none():
    market = _market(price=110.0)
    cls = _classifier(direction="long")
    metadata = {"breakout_level": 100.0}

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    # The "Plan:" header is exclusively from the trade-plan block; without
    # a plan, it must not appear.
    assert "Plan:" not in body
    # The rest of the alert still landed
    assert "BOS confirmed" in body
    assert "AMD" in body


# ============================================================================
# Stage 2 enrichment — predictor_result in metadata supersedes classifier
# ============================================================================

def test_alert_renders_stage2_thesis_and_risks_when_present():
    """When metadata carries predictor_result, the Telegram body uses its
    thesis, kill signal, horizon, entry guidance, and risks instead of the
    classifier's defaults."""
    from radar.predictor import PredictorResult

    market = _market(price=110.0)
    cls = _classifier(direction="long")
    pred = PredictorResult(
        verdict="ALERT_NOW",
        direction_confidence=0.85,
        setup_quality=0.78,
        thesis="Clean 4h breakout with confirming ETF flow news — bullish continuation likely.",
        kill_signal="Below $99.80 — that's where the breakout fails.",
        expected_horizon="1-3_days",
        expected_r_multiple=3.0,
        entry_guidance="Pullback to $110.00",
        risks=["BTC reversal", "Fed speak on Wednesday", "Volume could fade"],
    )
    metadata = {"breakout_level": 100.0, "predictor_result": pred}

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    # Stage 2 thesis and kill rendered
    assert "Clean 4h breakout" in body
    assert "Below $99.80" in body
    # Stage 2 horizon used, not classifier's "unknown" (underscore is md-escaped)
    assert "1-3" in body and "days" in body
    # Entry guidance line
    assert "Pullback to $110.00" in body
    # Setup quality line
    assert "Setup:" in body
    assert "78/100" in body
    # Risks rendered as a bulleted block
    assert "Risks:" in body
    assert "BTC reversal" in body
    assert "Fed speak on Wednesday" in body


def test_alert_falls_back_to_classifier_when_no_predictor():
    market = _market(price=110.0)
    cls = _classifier(direction="long", horizon="swing")
    metadata = {"breakout_level": 100.0}  # no predictor_result

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    # Classifier's horizon shown
    assert "swing" in body
    # No stage-2-specific labels
    assert "Setup:" not in body
    assert "Risks:" not in body
    assert "Entry:" not in body
