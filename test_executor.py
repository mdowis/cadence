"""Tests for Kalshi order body construction (executor.py)."""

from executor import (
    _cents_to_dollars_str,
    _yes_order,
    _no_order,
    build_orders_for_opportunity,
    TIF_IOC,
    TIF_GTC,
)
from kalshi_arbitrage import ArbitrageOpportunity


def test_cents_to_dollars_basic():
    assert _cents_to_dollars_str(65) == "0.6500"
    assert _cents_to_dollars_str(5) == "0.0500"
    assert _cents_to_dollars_str(99) == "0.9900"
    assert _cents_to_dollars_str(0) == "0.0000"
    assert _cents_to_dollars_str(100) == "1.0000"
    assert _cents_to_dollars_str(None) is None


def test_yes_order_body():
    order = _yes_order("BTC-100K", 47, 5)
    assert order["ticker"] == "BTC-100K"
    assert order["side"] == "yes"
    assert order["action"] == "buy"
    assert order["count"] == 5
    assert order["yes_price_dollars"] == "0.4700"
    # CRITICAL: must be full name, not "ioc"
    assert order["time_in_force"] == "immediate_or_cancel"
    assert "client_order_id" in order
    # Must NOT include legacy fields
    assert "yes_price" not in order
    assert "no_price" not in order
    # Must NOT include invalid "type" field
    assert "type" not in order


def test_no_order_body():
    order = _no_order("BTC-100K", 53, 5)
    assert order["ticker"] == "BTC-100K"
    assert order["side"] == "no"
    assert order["action"] == "buy"
    assert order["count"] == 5
    assert order["no_price_dollars"] == "0.5300"
    assert order["time_in_force"] == "immediate_or_cancel"
    assert "type" not in order


def test_tif_constants():
    """Must match Kalshi's exact expected values."""
    assert TIF_IOC == "immediate_or_cancel"
    assert TIF_GTC == "good_till_cancelled"


def _make_opp(type_, markets):
    return ArbitrageOpportunity(
        type=type_,
        event_title="Test",
        event_ticker="TEST",
        markets=markets,
        total_cost=0,
        guaranteed_payout=100,
        profit_cents=5,
        roi_percent=5,
        fee_cents=0,
        net_profit_cents=5,
    )


def test_binary_arb_builds_yes_and_no():
    opp = _make_opp(
        "binary",
        [{"ticker": "T1", "title": "Test", "yes_ask": 47, "no_ask": 48}],
    )
    orders = build_orders_for_opportunity(opp, contracts=3)
    assert len(orders) == 2
    yes = next(o for o in orders if o["side"] == "yes")
    no = next(o for o in orders if o["side"] == "no")
    assert yes["ticker"] == "T1"
    assert yes["yes_price_dollars"] == "0.4700"
    assert yes["count"] == 3
    assert yes["time_in_force"] == "immediate_or_cancel"
    assert no["ticker"] == "T1"
    assert no["no_price_dollars"] == "0.4800"
    assert no["count"] == 3


def test_multi_yes_builds_all_yes_legs():
    opp = _make_opp(
        "multi_outcome_under (buy all YES)",
        [
            {"ticker": "A", "title": "a", "yes_ask": 30},
            {"ticker": "B", "title": "b", "yes_ask": 25},
            {"ticker": "C", "title": "c", "yes_ask": 20},
        ],
    )
    orders = build_orders_for_opportunity(opp, contracts=1)
    assert len(orders) == 3
    assert all(o["side"] == "yes" for o in orders)
    assert all(o["time_in_force"] == "immediate_or_cancel" for o in orders)
    assert orders[0]["yes_price_dollars"] == "0.3000"
    assert orders[1]["yes_price_dollars"] == "0.2500"
    assert orders[2]["yes_price_dollars"] == "0.2000"


def test_multi_no_builds_all_no_legs():
    opp = _make_opp(
        "multi_outcome_over (buy all NO)",
        [
            {"ticker": "A", "title": "a", "no_ask": 30},
            {"ticker": "B", "title": "b", "no_ask": 28},
            {"ticker": "C", "title": "c", "no_ask": 25},
        ],
    )
    orders = build_orders_for_opportunity(opp, contracts=2)
    assert len(orders) == 3
    assert all(o["side"] == "no" for o in orders)
    assert all(o["count"] == 2 for o in orders)
    assert orders[0]["no_price_dollars"] == "0.3000"
    assert orders[1]["no_price_dollars"] == "0.2800"
    assert orders[2]["no_price_dollars"] == "0.2500"


def test_unique_client_order_ids():
    """Each leg must have a unique client_order_id so the batch endpoint
    doesn't reject duplicates."""
    opp = _make_opp(
        "binary",
        [{"ticker": "T1", "title": "Test", "yes_ask": 47, "no_ask": 48}],
    )
    orders = build_orders_for_opportunity(opp, contracts=1)
    ids = [o["client_order_id"] for o in orders]
    assert len(set(ids)) == len(ids)


if __name__ == "__main__":
    test_cents_to_dollars_basic()
    test_yes_order_body()
    test_no_order_body()
    test_tif_constants()
    test_binary_arb_builds_yes_and_no()
    test_multi_yes_builds_all_yes_legs()
    test_multi_no_builds_all_no_legs()
    test_unique_client_order_ids()
    print("All 8 executor tests passed!")
