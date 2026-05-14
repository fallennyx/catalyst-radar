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
# Compact alert format — ticker, price, direction, TP/SL, thesis (v3.2)
# ============================================================================

def test_alert_renders_ticker_price_direction_and_plan():
    market = _market(price=110.0)
    cls = _classifier(direction="long")
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.TradePlan(
        direction="long", entry=110.0, stop=99.8, tp1=125.3, tp2=140.6,
        risk_per_unit=10.2, r_multiple_tp1=1.5, r_multiple_tp2=3.0,
    )

    sent, p = _capture_body()
    with p:
        ok = telegram.send_bos_alert(market, cls, metadata, plan=plan)
    assert ok is True
    body = sent[0]

    assert "AMD" in body
    assert "LONG" in body
    assert "110.00" in body                       # entry
    assert "99.80" in body or "99.8" in body      # stop
    assert "125.30" in body or "125.3" in body    # tp1
    assert "140.60" in body or "140.6" in body    # tp2
    assert "1.5R" in body
    assert "3.0R" in body
    assert "+4.50%" in body                        # 24h pct from market


def test_alert_renders_short_direction_and_plan():
    market = _market(price=90.0)
    cls = _classifier(direction="short")
    metadata = {"breakout_level": 100.0}
    plan = trade_plan.TradePlan(
        direction="short", entry=90.0, stop=100.2, tp1=74.7, tp2=59.4,
        risk_per_unit=10.2, r_multiple_tp1=1.5, r_multiple_tp2=3.0,
    )

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=plan)
    body = sent[0]

    assert "SHORT" in body
    assert "100.20" in body or "100.2" in body
    assert "74.70" in body or "74.7" in body


def test_alert_omits_plan_line_when_no_plan():
    market = _market(price=110.0)
    cls = _classifier(direction="long")
    metadata = {"breakout_level": 100.0}

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    # Without a plan, the Entry/Stop/TP line should not appear.
    assert "*Entry*" not in body
    assert "*Stop*" not in body
    # But ticker + price still render.
    assert "AMD" in body
    assert "Price" in body


def test_alert_uses_predictor_thesis_when_present():
    """Predictor thesis (the WHY) is rendered as the body of the alert."""
    from radar.predictor import PredictorResult

    market = _market(price=110.0)
    cls = _classifier(direction="long")
    pred = PredictorResult(
        final_direction="long",
        verdict="ALERT_NOW",
        direction_confidence=0.85,
        setup_quality=0.78,
        thesis="Clean 4h breakout with confirming ETF flow news. Bid-heavy book and OI rising.",
        kill_signal="Below $99.80 — that's where the breakout fails.",
        expected_horizon="1-3_days",
        expected_r_multiple=3.0,
        entry_guidance="Pullback to $110.00",
        risks=["BTC reversal"],
    )
    metadata = {"breakout_level": 100.0, "predictor_result": pred}

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    assert "Clean 4h breakout" in body
    assert "Bid-heavy book" in body


def test_alert_falls_back_to_classifier_thesis_when_no_predictor():
    market = _market(price=110.0)
    cls = _classifier(direction="long", horizon="swing")
    metadata = {"breakout_level": 100.0}

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    # Classifier's continuation_thesis text appears as the body.
    assert "Trend continuation expected" in body
    # Old verbose fields should not appear.
    assert "Setup:" not in body
    assert "Risks:" not in body
    assert "Horizon:" not in body
    assert "Catalyst:" not in body
    assert "BOS confirmed" not in body


def test_alert_renders_flip_badge_when_llm_flipped():
    """When the adjudicator flips direction vs structure, alert shows the flip."""
    from radar.direction_adjudicator import AdjudicatedDirection
    from radar.predictor import PredictorResult

    market = _market(price=110.0)
    cls = _classifier(direction="long")
    pred = PredictorResult(
        final_direction="short",
        verdict="ALERT_NOW",
        direction_confidence=0.7, setup_quality=0.7,
        thesis="Sweep above pivot then rejection — fade.",
        kill_signal="Above $111", expected_horizon="intraday",
        expected_r_multiple=2.0, entry_guidance="market",
    )
    adj = AdjudicatedDirection(
        direction="short", conviction_tier="OK", flipped=True,
        predictor_result=pred, signal_bundle={}, fallback=False,
    )
    metadata = {
        "breakout_level": 100.0,
        "structure_direction": "long",
        "adjudicated": adj,
        "predictor_result": pred,
    }

    sent, p = _capture_body()
    with p:
        telegram.send_bos_alert(market, cls, metadata, plan=None)
    body = sent[0]

    assert "flipped from structural LONG" in body
    assert "SHORT" in body
