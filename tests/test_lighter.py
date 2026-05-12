"""Tests for radar/lighter.py — focused on the v3 order-book sentiment path.

The Lighter API endpoint shape was verified against mainnet in May 2026
(`/orderBookOrders?market_id=N&limit=N` → `{asks, bids: [{remaining_base_amount,
price, ...}]}`). These tests pin the parser + the sentiment buckets so a
schema change at the API side gets caught immediately."""

from unittest.mock import MagicMock, patch

from radar import lighter


def test_sum_side_usd_handles_strings_and_malformed():
    """Lighter returns base_amount and price as strings. Verify cast + skip."""
    side = [
        {"remaining_base_amount": "1.5", "price": "100.0"},     # 150
        {"remaining_base_amount": "2.0", "price": "50.0"},      # 100
        {"remaining_base_amount": "bad", "price": "10.0"},      # skipped
        {"remaining_base_amount": "0.5"},                       # skipped (no price)
    ]
    assert lighter._sum_side_usd(side) == 250.0


def test_imbalance_sentiment_buckets():
    """Bucket boundaries — verify both the ratio and label."""
    cases = [
        # (bid_usd, ask_usd, expected_label)
        (300_000.0, 100_000.0, "strongly bullish"),    # 3.0 > 2.0
        (200_000.0, 100_000.0, "bullish"),             # 2.0 = boundary, > 1.3
        (140_000.0, 100_000.0, "bullish"),             # 1.4 > 1.3
        (100_000.0, 100_000.0, "neutral"),             # 1.0
        (90_000.0, 100_000.0, "neutral"),              # 0.9 in [0.77, 1.3]
        (70_000.0, 100_000.0, "bearish"),              # 0.7 < 0.77
        (40_000.0, 100_000.0, "strongly bearish"),     # 0.4 < 0.5
    ]
    for bid, ask, want in cases:
        ratio, label = lighter.imbalance_sentiment(bid, ask)
        assert label == want, f"bid={bid} ask={ask}: got '{label}', want '{want}' (ratio={ratio:.3f})"


def test_imbalance_sentiment_handles_zero_ask():
    """An empty ask side shouldn't divide-by-zero."""
    ratio, label = lighter.imbalance_sentiment(100.0, 0.0)
    assert label == "strongly bullish"
    assert ratio == float("inf")


def test_imbalance_sentiment_handles_both_zero():
    """No depth on either side → neutral fallback (not a crash)."""
    ratio, label = lighter.imbalance_sentiment(0.0, 0.0)
    assert label == "neutral"
    assert ratio == 1.0


def test_fetch_order_book_depth_parses_response():
    """Mock the /orderBookOrders response and verify (bid_usd, ask_usd) math."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "code": 200,
        "total_asks": 2, "total_bids": 2,
        "asks": [
            {"remaining_base_amount": "1.0", "price": "100.0"},   # $100
            {"remaining_base_amount": "2.0", "price": "101.0"},   # $202
        ],
        "bids": [
            {"remaining_base_amount": "0.5", "price": "99.0"},    # $49.50
            {"remaining_base_amount": "1.5", "price": "98.0"},    # $147
        ],
    }
    with patch("radar.lighter.requests.get",
               return_value=fake_response) as mock_get:
        result = lighter.fetch_order_book_depth(market_id=1, levels=10)
    assert result is not None
    bid_usd, ask_usd = result
    assert abs(bid_usd - 196.5) < 1e-6
    assert abs(ask_usd - 302.0) < 1e-6
    mock_get.assert_called_once()
    # Verify the right params went out
    kwargs = mock_get.call_args.kwargs
    assert kwargs["params"]["market_id"] == 1
    assert kwargs["params"]["limit"] == 10


def test_fetch_order_book_depth_returns_none_on_http_error():
    fake_response = MagicMock()
    fake_response.status_code = 500
    with patch("radar.lighter.requests.get", return_value=fake_response):
        assert lighter.fetch_order_book_depth(market_id=1) is None


def test_fetch_order_book_depth_returns_none_on_request_exception():
    with patch("radar.lighter.requests.get",
               side_effect=Exception("network down")):
        assert lighter.fetch_order_book_depth(market_id=1) is None


def test_market_id_for_returns_none_for_unlisted():
    """Reverse-lookup must not crash on an unknown ticker."""
    with patch.object(lighter, "fetch_universe", return_value=[]):
        assert lighter.market_id_for("NOPE") is None
        assert lighter.market_id_for("") is None
