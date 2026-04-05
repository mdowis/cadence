"""Tests for risk management system."""

import time
from risk_manager import RiskManager, RiskConfig, RiskAction, RiskDecision
from kalshi_arbitrage import ArbitrageOpportunity


def make_opp(event_ticker="EVT", total_cost=90, payout=100, net_profit=5, roi=5.5):
    """Helper to create a test opportunity."""
    return ArbitrageOpportunity(
        type="binary",
        event_title="Test Event",
        event_ticker=event_ticker,
        markets=[{"title": "test", "ticker": "T1", "yes_ask": 45, "no_ask": 45}],
        total_cost=total_cost,
        guaranteed_payout=payout,
        profit_cents=net_profit + 2,
        roi_percent=roi,
        fee_cents=2,
        net_profit_cents=net_profit,
    )


# --- Kill Switch ---

def test_kill_switch_blocks_trades():
    rm = RiskManager(starting_equity_cents=10000)
    rm.activate_kill_switch("test")
    opp = make_opp()
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_KILL_SWITCH


def test_kill_switch_can_be_deactivated():
    rm = RiskManager(starting_equity_cents=10000)
    rm.activate_kill_switch("test")
    rm.deactivate_kill_switch()
    decision = rm.check_trade(make_opp())
    assert decision.allowed


# --- Drawdown ---

def test_drawdown_pct_triggers_kill_switch():
    config = RiskConfig(max_drawdown_pct=10.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    # Simulate losses bringing equity down 10%+
    rm.state.current_equity_cents = 8900  # 11% drawdown from peak of 10000
    decision = rm.check_trade(make_opp())
    assert decision.action == RiskAction.BLOCK_DRAWDOWN
    assert rm.state.kill_switch_active


def test_drawdown_absolute_triggers_kill_switch():
    config = RiskConfig(max_drawdown_pct=None, max_drawdown_cents=500)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.current_equity_cents = 9400  # 600¢ drawdown, limit is 500
    decision = rm.check_trade(make_opp())
    assert decision.action == RiskAction.BLOCK_DRAWDOWN


def test_no_drawdown_allows():
    rm = RiskManager(starting_equity_cents=10000)
    rm.state.current_equity_cents = 9500  # 5% drawdown, limit is 10%
    decision = rm.check_trade(make_opp())
    assert decision.allowed


# --- Daily Loss Limit ---

def test_daily_loss_limit_blocks():
    config = RiskConfig(daily_loss_limit_cents=1000)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_pnl_cents = -1000
    rm.state.daily_date = time.strftime("%Y-%m-%d")
    decision = rm.check_trade(make_opp())
    assert decision.action == RiskAction.BLOCK_DAILY_LOSS


# --- Per-Trade Limit ---

def test_per_trade_limit_blocks():
    config = RiskConfig(max_per_trade_cents=100)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    opp = make_opp(total_cost=200)  # over limit
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_PER_TRADE


def test_per_trade_limit_allows():
    config = RiskConfig(max_per_trade_cents=500)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    opp = make_opp(total_cost=90)
    decision = rm.check_trade(opp)
    assert decision.allowed


# --- Exposure Limits ---

def test_total_exposure_blocks():
    config = RiskConfig(max_total_exposure_cents=500)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.total_exposure_cents = 450
    opp = make_opp(total_cost=100)  # would push to 550
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_EXPOSURE


def test_per_event_exposure_blocks():
    config = RiskConfig(max_position_per_event_cents=200)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.exposure_by_event["EVT"] = 150
    opp = make_opp(event_ticker="EVT", total_cost=100)  # would push to 250
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_POSITION


# --- Consecutive Losses ---

def test_consecutive_losses_breaker():
    config = RiskConfig(max_consecutive_losses=3, cooldown_after_consecutive_secs=1)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.consecutive_losses = 3
    rm.state.last_consecutive_breaker_time = time.time()
    decision = rm.check_trade(make_opp())
    assert decision.action == RiskAction.BLOCK_CONSECUTIVE


def test_consecutive_losses_resets_on_win():
    config = RiskConfig(max_consecutive_losses=3)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.consecutive_losses = 2

    opp = make_opp()
    rm.record_trade_opened(opp)
    rm.record_trade_closed("EVT", actual_pnl_cents=5)  # win
    assert rm.state.consecutive_losses == 0


# --- Trade Lifecycle ---

def test_trade_open_updates_exposure():
    rm = RiskManager(starting_equity_cents=10000)
    opp = make_opp(total_cost=90, event_ticker="EV1")
    rm.record_trade_opened(opp)
    assert rm.state.total_exposure_cents == 90
    assert rm.state.exposure_by_event["EV1"] == 90
    assert rm.state.daily_trade_count == 1
    assert len(rm.state.open_positions) == 1


def test_trade_close_updates_equity():
    rm = RiskManager(starting_equity_cents=10000)
    opp = make_opp(total_cost=90, event_ticker="EV1")
    rm.record_trade_opened(opp)
    rm.record_trade_closed("EV1", actual_pnl_cents=10)  # won 10¢
    assert rm.state.current_equity_cents == 10010
    assert rm.state.peak_equity_cents == 10010
    assert rm.state.total_exposure_cents == 0
    assert len(rm.state.open_positions) == 0


def test_losing_trade_tracks_drawdown():
    rm = RiskManager(starting_equity_cents=10000)
    opp = make_opp(total_cost=90, event_ticker="EV1")
    rm.record_trade_opened(opp)
    rm.record_trade_closed("EV1", actual_pnl_cents=-90)
    assert rm.state.current_equity_cents == 9910
    assert rm.state.peak_equity_cents == 10000  # peak unchanged
    assert rm.state.consecutive_losses == 1


# --- Minimum Profitability ---

def test_min_profit_blocks():
    config = RiskConfig(min_net_profit_cents=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    opp = make_opp(net_profit=2)  # below min
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_PER_TRADE


def test_min_roi_blocks():
    config = RiskConfig(min_roi_pct=3.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    opp = make_opp(roi=1.0)  # below min
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_PER_TRADE


# --- Status Report ---

def test_status_report():
    rm = RiskManager(starting_equity_cents=10000)
    status = rm.get_status()
    assert status["equity_cents"] == 10000
    assert status["drawdown_pct"] == 0
    assert "config" in status


if __name__ == "__main__":
    test_kill_switch_blocks_trades()
    test_kill_switch_can_be_deactivated()
    test_drawdown_pct_triggers_kill_switch()
    test_drawdown_absolute_triggers_kill_switch()
    test_no_drawdown_allows()
    test_daily_loss_limit_blocks()
    test_per_trade_limit_blocks()
    test_per_trade_limit_allows()
    test_total_exposure_blocks()
    test_per_event_exposure_blocks()
    test_consecutive_losses_breaker()
    test_consecutive_losses_resets_on_win()
    test_trade_open_updates_exposure()
    test_trade_close_updates_equity()
    test_losing_trade_tracks_drawdown()
    test_min_profit_blocks()
    test_min_roi_blocks()
    test_status_report()
    print("All 18 risk manager tests passed!")
