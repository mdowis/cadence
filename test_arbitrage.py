"""Tests for Kalshi arbitrage detection logic."""

from kalshi_arbitrage import (
    find_binary_arbitrage,
    find_multi_outcome_arbitrage,
    find_near_misses,
    compute_fee_adjusted_profit,
)


def test_binary_arb_detected():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 45, "no_ask": 50},  # 95 < 100 → arb
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 1
    assert opps[0].profit_cents == 5
    assert opps[0].total_cost == 95


def test_binary_no_arb():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 55, "no_ask": 47},  # 102 > 100 → no arb
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 0


def test_binary_exact_100():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 50, "no_ask": 50},  # 100 = 100 → no arb
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 0


def test_multi_outcome_yes_arb():
    markets = [
        {"ticker": "T1", "title": "A", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 30, "no_ask": 72},
        {"ticker": "T2", "title": "B", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 25, "no_ask": 77},
        {"ticker": "T3", "title": "C", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 20, "no_ask": 82},
    ]
    opps = find_multi_outcome_arbitrage(markets)
    yes_opps = [o for o in opps if "YES" in o.type]
    assert len(yes_opps) == 1
    assert yes_opps[0].profit_cents == 25  # 100 - 75


def test_multi_outcome_no_arb():
    markets = [
        {"ticker": "T1", "title": "A", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 40, "no_ask": 30},
        {"ticker": "T2", "title": "B", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 35, "no_ask": 25},
        {"ticker": "T3", "title": "C", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 30, "no_ask": 20},
    ]
    # NO: 30+25+20=75, payout=(3-1)*100=200, profit=125
    opps = find_multi_outcome_arbitrage(markets)
    no_opps = [o for o in opps if "NO" in o.type]
    assert len(no_opps) == 1
    assert no_opps[0].profit_cents == 125


def test_multi_outcome_no_arb_when_efficient():
    markets = [
        {"ticker": "T1", "title": "A", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 50, "no_ask": 52},
        {"ticker": "T2", "title": "B", "event_ticker": "E1", "event_title": "Ev", "yes_ask": 50, "no_ask": 52},
    ]
    # YES: 100 = 100 → no arb. NO: 104, payout=100 → no arb
    opps = find_multi_outcome_arbitrage(markets)
    assert len(opps) == 0


def test_fee_adjustment():
    assert abs(compute_fee_adjusted_profit(10) - 9.3) < 0.01
    assert compute_fee_adjusted_profit(0) == 0.0
    assert compute_fee_adjusted_profit(-5) == 0.0


def test_near_misses():
    markets = [
        {"ticker": "T1", "title": "A", "yes_ask": 51, "no_ask": 50},  # 101
        {"ticker": "T2", "title": "B", "yes_ask": 55, "no_ask": 47},  # 102
        {"ticker": "T3", "title": "C", "yes_ask": 60, "no_ask": 44},  # 104 → beyond threshold
    ]
    near = find_near_misses(markets, threshold=3)
    assert len(near) == 2


def test_min_profit_filter():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "event_title": "Ev",
         "yes_ask": 49, "no_ask": 50},  # profit=1¢, net=0.93¢
    ]
    # With min_profit=1, net 0.93 < 1 → filtered out
    opps = find_binary_arbitrage(markets, min_profit=1)
    assert len(opps) == 0
    # With min_profit=0.5 → included
    opps = find_binary_arbitrage(markets, min_profit=0.5)
    assert len(opps) == 1


def test_skips_missing_prices():
    markets = [
        {"ticker": "T1", "title": "Test", "event_ticker": "E1", "yes_ask": None, "no_ask": 50},
        {"ticker": "T2", "title": "Test2", "event_ticker": "E2", "yes_ask": 50, "no_ask": 0},
    ]
    opps = find_binary_arbitrage(markets)
    assert len(opps) == 0


if __name__ == "__main__":
    test_binary_arb_detected()
    test_binary_no_arb()
    test_binary_exact_100()
    test_multi_outcome_yes_arb()
    test_multi_outcome_no_arb()
    test_multi_outcome_no_arb_when_efficient()
    test_fee_adjustment()
    test_near_misses()
    test_min_profit_filter()
    test_skips_missing_prices()
    print("All tests passed!")
