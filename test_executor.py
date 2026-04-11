"""Tests for Kalshi order construction and execution safety (executor.py)."""

from executor import (
    _cents_to_dollars_str,
    _yes_order,
    _no_order,
    build_orders_for_opportunity,
    execute_opportunity,
    _parse_order_status,
    _unwind_filled_legs,
    FILLED_STATUSES,
    TIF_IOC,
    TIF_GTC,
)
from kalshi_arbitrage import ArbitrageOpportunity
from risk_manager import RiskManager, RiskConfig


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


# ---------------------------------------------------------------------------
# Fake Kalshi trader for execution safety tests
# ---------------------------------------------------------------------------

class FakeTrader:
    """
    Minimal in-memory trader that records orders and returns scriptable
    batch responses. Used to test execute_opportunity paths without
    hitting the real Kalshi API.
    """
    def __init__(self, batch_response=None, positions_response=None,
                 orderbook_response=None, raise_on_batch=False):
        self.authenticated = True
        self.base_url = "https://fake"
        self.calls = []
        self.unwind_calls = []
        self.batch_response = batch_response or {"orders": []}
        self.positions_response = positions_response or {"positions": []}
        # Default orderbook: yes bid at 40c, no bid at 40c
        self.orderbook_response = orderbook_response or {
            "orderbook_fp": {
                "yes_dollars": [["0.4000", "5.00"]],
                "no_dollars": [["0.4000", "5.00"]],
            }
        }
        self.raise_on_batch = raise_on_batch

    def batch_create_orders(self, orders):
        self.calls.append(("batch", list(orders)))
        if self.raise_on_batch:
            from kalshi_arbitrage import HTTPError
            raise HTTPError("fake 400")
        return self.batch_response

    def create_order(self, **kwargs):
        self.calls.append(("single", kwargs))
        return {"status": "executed"}

    def get_positions(self):
        return self.positions_response

    def get_balance(self):
        # balance=cash, portfolio_value=total equity.
        # Default: $1000 cash, $1000 portfolio (matches default test RM equity).
        return {"balance": 100000, "portfolio_value": 100000}

    def get_orderbook(self, ticker):
        return self.orderbook_response

    def _request(self, method, url, json=None):
        self.unwind_calls.append(json)
        return {"order": {"status": "executed"}}


def _make_binary_opp(yes_ask=40, no_ask=50, event="EVT", ticker="T1"):
    """Default: 40c + 50c = 90c cost → 10c gross, well above 5c min."""
    return ArbitrageOpportunity(
        type="binary",
        event_title="Test",
        event_ticker=event,
        markets=[{"ticker": ticker, "title": "Test",
                  "yes_ask": yes_ask, "no_ask": no_ask}],
        total_cost=yes_ask + no_ask,
        guaranteed_payout=100,
        profit_cents=100 - yes_ask - no_ask,
        roi_percent=20,
        fee_cents=2,
        net_profit_cents=100 - yes_ask - no_ask - 2,
    )


def _make_multi_opp():
    """Multi-outcome YES arb, well above 5c min profit."""
    return ArbitrageOpportunity(
        type="multi_outcome_under (buy all YES)",
        event_title="Test",
        event_ticker="MULTI",
        markets=[
            {"ticker": "A", "title": "a", "yes_ask": 20},
            {"ticker": "B", "title": "b", "yes_ask": 20},
            {"ticker": "C", "title": "c", "yes_ask": 20},
        ],
        total_cost=60,
        guaranteed_payout=100,
        profit_cents=40,
        roi_percent=66,
        fee_cents=3,
        net_profit_cents=37,
    )


# ---------------------------------------------------------------------------
# Safety: fill status parsing
# ---------------------------------------------------------------------------

def test_only_executed_counts_as_filled():
    """'resting' is NOT filled; only 'executed' counts."""
    assert "executed" in FILLED_STATUSES
    assert "resting" not in FILLED_STATUSES
    assert "cancelled" not in FILLED_STATUSES
    assert "rejected" not in FILLED_STATUSES


def test_parse_order_status_flat():
    status, order = _parse_order_status({"status": "executed", "ticker": "X"})
    assert status == "executed"
    assert order["ticker"] == "X"


def test_parse_order_status_wrapped():
    status, order = _parse_order_status(
        {"order": {"status": "cancelled", "ticker": "Y"}}
    )
    assert status == "cancelled"
    assert order["ticker"] == "Y"


# ---------------------------------------------------------------------------
# Safety: multi-leg arbs are opt-in
# ---------------------------------------------------------------------------

def test_multi_leg_blocked_by_default():
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader()
    opp = _make_multi_opp()
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=True,
        allow_multi_leg=False,
    )
    assert not success
    assert "multi-leg" in detail.lower()
    # No orders should have been placed
    assert len(trader.calls) == 0


def test_multi_leg_allowed_when_opted_in():
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader()
    opp = _make_multi_opp()
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=True,
        allow_multi_leg=True,
    )
    assert success  # dry-run path, should succeed


# ---------------------------------------------------------------------------
# Safety: partial fills are unwound
# ---------------------------------------------------------------------------

def test_zero_fills_returns_failure():
    """If no legs fill, don't track exposure, don't say success."""
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader(batch_response={
        "orders": [
            {"order": {"status": "cancelled"}},
            {"order": {"status": "cancelled"}},
        ]
    })
    opp = _make_binary_opp()
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=False,
    )
    assert not success
    assert "no legs filled" in detail.lower() or "0" in detail
    # Exposure must not have been tracked
    assert rm.state.total_exposure_cents == 0


def test_partial_fill_triggers_unwind():
    """One leg filled, other cancelled → unwind the filled leg."""
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader(batch_response={
        "orders": [
            {"order": {"status": "executed", "filled_quantity": 1}},
            {"order": {"status": "cancelled"}},
        ]
    })
    opp = _make_binary_opp()  # yes_ask=40, no_ask=50
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=False,
    )
    assert not success  # partial fill is never success
    assert "partial" in detail.lower()
    # Unwind should have been attempted
    assert len(trader.unwind_calls) == 1
    unwind = trader.unwind_calls[0]
    assert unwind["action"] == "sell"
    assert unwind["side"] == "yes"  # we filled the yes leg
    # CRITICAL: unwind price must match the real best bid from the
    # orderbook (40c default in FakeTrader), NOT 1c
    assert unwind["yes_price_dollars"] == "0.4000"
    # Exposure should NOT be recorded for partial fills
    assert rm.state.total_exposure_cents == 0


def test_unwind_uses_best_bid_from_orderbook():
    """Verify unwind fetches the actual orderbook and uses the top bid."""
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader(
        batch_response={
            "orders": [
                {"order": {"status": "executed", "filled_quantity": 2}},
                {"order": {"status": "cancelled"}},
            ]
        },
        orderbook_response={
            "orderbook_fp": {
                "yes_dollars": [
                    ["0.3200", "10.00"],
                    ["0.3500", "5.00"],   # highest bid, should be picked
                    ["0.3000", "20.00"],
                ],
                "no_dollars": [["0.6000", "10.00"]],
            }
        },
    )
    opp = _make_binary_opp()
    execute_opportunity(trader, rm, opp, contracts=2, dry_run=False)
    assert len(trader.unwind_calls) == 1
    unwind = trader.unwind_calls[0]
    # Must pick the MAX bid across all levels, not just the first one
    assert unwind["yes_price_dollars"] == "0.3500"
    assert unwind["count"] == 2


def test_unwind_fails_when_no_bids_trips_kill_switch():
    """If no bids exist, unwind can't execute → kill switch trips."""
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader(
        batch_response={
            "orders": [
                {"order": {"status": "executed", "filled_quantity": 1}},
                {"order": {"status": "cancelled"}},
            ]
        },
        orderbook_response={
            "orderbook_fp": {"yes_dollars": [], "no_dollars": []}
        },
    )
    opp = _make_binary_opp()
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=False,
    )
    assert not success
    assert "KILL SWITCH" in detail or "kill" in detail.lower()
    assert rm.state.kill_switch_active


def test_full_fill_records_exposure():
    """All legs filled → record exposure, report success."""
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader(batch_response={
        "orders": [
            {"order": {"status": "executed", "filled_quantity": 1}},
            {"order": {"status": "executed", "filled_quantity": 1}},
        ]
    })
    opp = _make_binary_opp(yes_ask=40, no_ask=50)
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=False,
    )
    assert success
    # Exposure tracked = full cost of the arb
    assert rm.state.total_exposure_cents == 90
    # No unwind calls
    assert len(trader.unwind_calls) == 0


def test_http_error_does_not_track_exposure():
    """If the batch call raises, nothing was placed, track nothing."""
    rm = RiskManager(starting_equity_cents=100000)
    trader = FakeTrader(raise_on_batch=True)
    opp = _make_binary_opp()
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=False,
    )
    assert not success
    assert "ORDER ERROR" in detail
    assert rm.state.total_exposure_cents == 0


# ---------------------------------------------------------------------------
# Safety: risk check blocks still work
# ---------------------------------------------------------------------------

def test_risk_block_prevents_order():
    rm = RiskManager(
        config=RiskConfig(max_per_trade_cents=10),  # $0.10 cap
        starting_equity_cents=100000,
    )
    trader = FakeTrader()
    opp = _make_binary_opp(yes_ask=47, no_ask=48)  # cost = 95c, over limit
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=False,
    )
    assert not success
    assert "RISK BLOCKED" in detail
    # No orders submitted
    assert len(trader.calls) == 0


def test_kill_switch_blocks_even_in_dry_run():
    rm = RiskManager(starting_equity_cents=100000)
    rm.activate_kill_switch("test")
    trader = FakeTrader()
    opp = _make_binary_opp()
    success, detail = execute_opportunity(
        trader, rm, opp, contracts=1, dry_run=True,
    )
    assert not success
    assert "BLOCKED" in detail


if __name__ == "__main__":
    test_cents_to_dollars_basic()
    test_yes_order_body()
    test_no_order_body()
    test_tif_constants()
    test_binary_arb_builds_yes_and_no()
    test_multi_yes_builds_all_yes_legs()
    test_multi_no_builds_all_no_legs()
    test_unique_client_order_ids()
    test_only_executed_counts_as_filled()
    test_parse_order_status_flat()
    test_parse_order_status_wrapped()
    test_multi_leg_blocked_by_default()
    test_multi_leg_allowed_when_opted_in()
    test_zero_fills_returns_failure()
    test_partial_fill_triggers_unwind()
    test_unwind_uses_best_bid_from_orderbook()
    test_unwind_fails_when_no_bids_trips_kill_switch()
    test_full_fill_records_exposure()
    test_http_error_does_not_track_exposure()
    test_risk_block_prevents_order()
    test_kill_switch_blocks_even_in_dry_run()
    print("All 21 executor tests passed!")
