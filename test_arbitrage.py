"""Tests for Kalshi arbitrage detection logic."""

import math
from kalshi_arbitrage import (
    find_binary_arbitrage,
    find_multi_outcome_arbitrage,
    find_near_misses,
    kalshi_fee_per_contract,
    total_arb_fee,
)


# ---------------------------------------------------------------------------
# Fee calculation tests
# ---------------------------------------------------------------------------

def test_fee_at_50_cents():
    """At P=50¢, taker fee = ceil(0.07 * 0.5 * 0.5 * 100) = ceil(1.75) = 2¢."""
    fee = kalshi_fee_per_contract(50)
    assert fee == 2, f"Expected 2¢, got {fee}¢"


def test_fee_at_extremes():
    """Fees should be very small at price extremes (5¢ and 95¢)."""
    fee_low = kalshi_fee_per_contract(5)
    fee_high = kalshi_fee_per_contract(95)
    # 0.07 * 0.05 * 0.95 = 0.003325 → ceil($0.003325 * 100) = ceil(0.3325) = 1¢
    assert fee_low == 1
    assert fee_high == 1  # symmetric: P*(1-P) is same for 5 and 95


def test_fee_symmetry():
    """Fee at P and (100-P) should be identical."""
    for p in [10, 20, 30, 40, 50, 60, 70, 80, 90]:
        assert kalshi_fee_per_contract(p) == kalshi_fee_per_contract(100 - p)


def test_maker_fee_lower():
    """Maker fee should be lower than taker fee at every price."""
    for p in range(1, 100):
        maker = kalshi_fee_per_contract(p, maker=True)
        taker = kalshi_fee_per_contract(p, maker=False)
        assert maker <= taker, f"Maker ({maker}) > Taker ({taker}) at {p}¢"


def test_total_arb_fee():
    """Total fee across legs should be sum of individual leg fees."""
    prices = [47, 48]
    total = total_arb_fee(prices)
    expected = kalshi_fee_per_contract(47) + kalshi_fee_per_contract(48)
    assert abs(total - expected) < 0.001


# ---------------------------------------------------------------------------
# Binary arbitrage tests
# ---------------------------------------------------------------------------

def test_binary_arb_detected():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 45, "no_ask": 50},  # 95 < 100 → 5¢ gross
    ]
    opps = find_binary_arbitrage(markets, min_profit=0)
    assert len(opps) == 1
    assert opps[0].profit_cents == 5
    assert opps[0].total_cost == 95
    assert opps[0].fee_cents > 0  # fees are nonzero
    assert opps[0].net_profit_cents < opps[0].profit_cents  # net < gross


def test_binary_no_arb():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 55, "no_ask": 47},  # 102 > 100
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 0


def test_binary_exact_100():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 50, "no_ask": 50},  # 100 = 100
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 0


def test_binary_fees_can_kill_arb():
    """A small gross profit can become negative after fees."""
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 49, "no_ask": 50},  # 99 → 1¢ gross, but fees eat it
    ]
    # Fees: fee(49) + fee(50) = ceil(0.07*0.49*0.51*100)/100 + ceil(0.07*0.5*0.5*100)/100
    # = ceil(1.7493)/100 + ceil(1.75)/100 = 0.02 + 0.02 = 0.04
    # Net: 1 - 4 = -3¢ → should not be reported with min_profit=1
    opps = find_binary_arbitrage(markets, min_profit=1)
    assert len(opps) == 0


# ---------------------------------------------------------------------------
# Multi-outcome arbitrage tests
# ---------------------------------------------------------------------------

def test_multi_outcome_yes_arb():
    markets = [
        {"ticker": "T1", "title": "A", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 30, "no_ask": 72},
        {"ticker": "T2", "title": "B", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 25, "no_ask": 77},
        {"ticker": "T3", "title": "C", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 20, "no_ask": 82},
    ]
    opps = find_multi_outcome_arbitrage(markets, min_profit=0)
    yes_opps = [o for o in opps if "YES" in o.type]
    assert len(yes_opps) == 1
    assert yes_opps[0].profit_cents == 25  # 100 - 75
    assert yes_opps[0].fee_cents > 0
    assert yes_opps[0].net_profit_cents < 25


def test_multi_outcome_no_arb():
    markets = [
        {"ticker": "T1", "title": "A", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 40, "no_ask": 30},
        {"ticker": "T2", "title": "B", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 35, "no_ask": 25},
        {"ticker": "T3", "title": "C", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 30, "no_ask": 20},
    ]
    opps = find_multi_outcome_arbitrage(markets, min_profit=0)
    no_opps = [o for o in opps if "NO" in o.type]
    assert len(no_opps) == 1
    assert no_opps[0].profit_cents == 125  # (3-1)*100 - 75


def test_multi_outcome_no_arb_when_efficient():
    markets = [
        {"ticker": "T1", "title": "A", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 50, "no_ask": 52},
        {"ticker": "T2", "title": "B", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 50, "no_ask": 52},
    ]
    opps = find_multi_outcome_arbitrage(markets)
    assert len(opps) == 0


# ---------------------------------------------------------------------------
# Near-miss tests
# ---------------------------------------------------------------------------

def test_near_misses():
    markets = [
        {"ticker": "T1", "title": "A", "yes_ask": 51, "no_ask": 50},  # 101
        {"ticker": "T2", "title": "B", "yes_ask": 55, "no_ask": 47},  # 102
        {"ticker": "T3", "title": "C", "yes_ask": 60, "no_ask": 44},  # 104
    ]
    near = find_near_misses(markets, threshold=3)
    assert len(near) == 2


def test_skips_missing_prices():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1",
         "yes_ask": None, "no_ask": 50},
        {"ticker": "T2", "title": "Test2", "event_ticker": "E2",
         "yes_ask": 50, "no_ask": 0},
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 0


if __name__ == "__main__":
    test_fee_at_50_cents()
    test_fee_at_extremes()
    test_fee_symmetry()
    test_maker_fee_lower()
    test_total_arb_fee()
    test_binary_arb_detected()
    test_binary_no_arb()
    test_binary_exact_100()
    test_binary_fees_can_kill_arb()
    test_multi_outcome_yes_arb()
    test_multi_outcome_no_arb()
    test_multi_outcome_no_arb_when_efficient()
    test_near_misses()
    test_skips_missing_prices()
    print("All 14 tests passed!")
