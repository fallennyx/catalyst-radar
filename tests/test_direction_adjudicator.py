"""direction_adjudicator tests — the LLM-driven direction layer.

These tests mock the predictor entirely and verify the orchestration logic:
signal-bundle assembly, conviction tier mapping, flip detection, and the
fail-safe fallback to the structural direction when the LLM is unreachable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from radar import config, direction_adjudicator, predictor, storage
from radar.predictor import PredictorResult
from radar.universe import Market


# ---------- fixtures ----------

def _market(price: float = 1.79, ticker: str = "PENDLE") -> Market:
    return Market(
        ticker=ticker, asset_class="crypto_t2", market_id=ticker,
        max_leverage=10.0, price=price, pct_24h=8.0,
    )


def _bars(n: int = 60, base_price: float = 1.70):
    return [
        storage.Bar(
            ticker="PENDLE", ts=i * 3600,
            open=base_price, high=base_price + 0.01, low=base_price - 0.01,
            close=base_price, volume=10_000, oi=1_000_000, funding=0.0001,
        )
        for i in range(n)
    ]


def _metadata(structure_direction: str = "long", breakout_level: float = 1.71) -> dict:
    return {
        "breakout_level": breakout_level,
        "structure_direction": structure_direction,
        "structure_type": "4h",
        "median_bar_range": 0.02,
    }


def _pred(final_direction: str = "long", direction_confidence: float = 0.85,
          setup_quality: float = 0.80) -> PredictorResult:
    return PredictorResult(
        final_direction=final_direction,
        direction_confidence=direction_confidence,
        setup_quality=setup_quality,
        thesis="Clean break with all signals aligned.",
        kill_signal="Below $1.70.",
        expected_horizon="intraday",
        expected_r_multiple=2.5,
        entry_guidance="market",
        verdict="ALERT_NOW",
        risks=["BTC could fade"],
    )


def _disable_book(monkeypatch):
    """Stop the adjudicator from hitting the live Lighter API in tests."""
    monkeypatch.setattr(
        direction_adjudicator, "_fetch_book_safe",
        lambda _market: (None, None, None, "unknown"),
    )


# ---------- tests ----------

def test_disabled_adjudicator_returns_structural_fallback(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", False)
    _disable_book(monkeypatch)
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "long"
    assert out.conviction_tier == "OK"
    assert out.flipped is False
    assert out.fallback is True
    assert out.predictor_result is None


def test_strong_alignment_returns_structural_direction(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)
    monkeypatch.setattr(
        predictor, "analyze",
        lambda **kw: _pred(final_direction="long", direction_confidence=0.9, setup_quality=0.85),
    )
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "long"
    assert out.conviction_tier == "STRONG"
    assert out.flipped is False
    assert out.fallback is False


def test_llm_flip_marked(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)
    monkeypatch.setattr(
        predictor, "analyze",
        lambda **kw: _pred(final_direction="short", direction_confidence=0.72, setup_quality=0.70),
    )
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "short"
    assert out.flipped is True
    # 0.72 × 0.70 → sqrt ≈ 0.71 → OK tier
    assert out.conviction_tier == "OK"


def test_no_trade_collapses_to_no_trade_tier(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)
    monkeypatch.setattr(
        predictor, "analyze",
        lambda **kw: _pred(final_direction="no_trade", direction_confidence=0.4, setup_quality=0.5),
    )
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "no_trade"
    assert out.conviction_tier == "NO_TRADE"
    assert out.flipped is False    # no_trade is not a flip


def test_tentative_tier_when_low_conviction(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)
    monkeypatch.setattr(
        predictor, "analyze",
        lambda **kw: _pred(final_direction="long", direction_confidence=0.40, setup_quality=0.40),
    )
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    # 0.4 × 0.4 → 0.4 → TENTATIVE (>= 0.30 and < 0.50)
    assert out.conviction_tier == "TENTATIVE"
    assert out.direction == "long"


def test_predictor_crash_falls_back_to_structural(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("Gemini down")

    monkeypatch.setattr(predictor, "analyze", _boom)
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("short"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "short"        # structural fallback
    assert out.conviction_tier == "OK"
    assert out.fallback is True
    assert out.predictor_result is None


def test_predictor_returns_none_falls_back_to_structural(monkeypatch):
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)
    monkeypatch.setattr(predictor, "analyze", lambda **kw: None)
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "long"
    assert out.fallback is True


def test_signal_bundle_includes_expected_keys(monkeypatch):
    """Signal bundle must include the structural prior, range ratios, wick
    analysis, OI/funding, book, HTF flag, BTC, and timing fields."""
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)

    captured = {}

    def _capture(**kw):
        captured.update(kw.get("signal_bundle", {}))
        return _pred()

    monkeypatch.setattr(predictor, "analyze", _capture)
    direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=_bars(20),
        btc_history=_bars(30), suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[],
        tier=2, watchlist_age_hours=3.5,
    )
    for key in (
        "structure_direction", "structure_type", "breakout_level", "current_price",
        "wick_1h", "wick_15m", "book_sentiment", "book_ratio",
        "oi_usd", "oi_delta_pct", "funding_pct", "volume_ratio", "volume_z",
        "btc_24h_pct", "tier", "watchlist_age_hours", "utc_hour",
    ):
        assert key in captured, f"missing signal bundle key: {key}"
    assert captured["tier"] == 2
    assert captured["watchlist_age_hours"] == "3.5"


def test_flip_blocked_when_config_disables(monkeypatch):
    """When DIR_ALLOW_FLIP=False, an LLM flip is forced back to structural in
    the predictor (covered by test_predictor_blocks_flip_when_disabled). The
    adjudicator simply propagates whatever the predictor returns — verify
    that path."""
    monkeypatch.setattr(config, "DIRECTION_ADJUDICATOR_ENABLED", True)
    _disable_book(monkeypatch)
    monkeypatch.setattr(
        predictor, "analyze",
        lambda **kw: _pred(final_direction="long"),   # predictor already coerced
    )
    out = direction_adjudicator.decide(
        market=_market(), history=_bars(), history_15m=None,
        btc_history=[], suppression_metadata=_metadata("long"),
        classifier_result=None, news_items=[], prior_alerts=[], tier=1,
    )
    assert out.direction == "long"
    assert out.flipped is False
