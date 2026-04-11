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


def test_drawdown_default_is_session_start_not_peak():
    """
    REGRESSION: user reported kill switch triggering even without any
    Cadence trades. Root cause was peak-based drawdown tripping from
    portfolio fluctuations of existing positions. Default is now
    session_start which resets daily and on manual reset.
    """
    config = RiskConfig(max_drawdown_pct=10.0)
    rm = RiskManager(config=config, starting_equity_cents=0)
    # First sync: user opens Cadence with $600 portfolio (existing positions)
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=60000)
    assert not rm.state.kill_switch_active
    # Portfolio value spikes up to $660, peak tracks it
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=66000)
    assert rm.state.peak_equity_cents == 66000
    # Now portfolio dips 11% from peak but only 6.7% from session start
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=58700)
    # Under OLD peak behavior: 11.1% drawdown from 66000 → kill switch
    # Under NEW session_start: 2.2% drawdown from 60000 → allowed
    assert not rm.state.kill_switch_active


def test_drawdown_peak_mode_still_works_when_configured():
    """Opt-in strict mode: CADENCE_DRAWDOWN_REFERENCE=peak."""
    config = RiskConfig(max_drawdown_pct=10.0, drawdown_reference="peak")
    rm = RiskManager(config=config, starting_equity_cents=0)
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=60000)
    # Push peak to $660
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=66000)
    # Dip 11% from the peak
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=58700)
    # Peak mode: triggers because 58700 is 11% below 66000
    assert rm.state.kill_switch_active


def test_drawdown_session_start_triggers_only_on_real_losses():
    """Session_start mode: must be below starting balance, not just peak."""
    config = RiskConfig(max_drawdown_pct=10.0)  # session_start by default
    rm = RiskManager(config=config, starting_equity_cents=0)
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=60000)
    # Drop 11% below session start ($60 → $53.40)
    rm.sync_actual_balance(balance_cents=10000, portfolio_value_cents=53400)
    assert rm.state.kill_switch_active
    assert "session_start" in rm.state.kill_switch_reason


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


# --- Balance Sync ---

def test_balance_sync_initializes_equity_with_portfolio_value():
    """First sync uses portfolio_value as authoritative equity."""
    rm = RiskManager(starting_equity_cents=0)
    rm.sync_actual_balance(balance_cents=3000, portfolio_value_cents=7500)
    assert rm.state.current_equity_cents == 7500  # portfolio_value wins
    assert rm.state.starting_equity_cents == 7500
    assert rm.state.peak_equity_cents == 7500
    # Derived exposure = portfolio - balance = 4500
    assert rm.state.total_exposure_cents == 4500


def test_balance_sync_falls_back_to_balance_if_no_portfolio_value():
    """If portfolio_value is missing/zero, fall back to cash balance."""
    rm = RiskManager(starting_equity_cents=0)
    rm.sync_actual_balance(7500)  # no portfolio_value
    assert rm.state.current_equity_cents == 7500
    # No portfolio_value means no derived exposure
    assert rm.state.total_exposure_cents == 0


def test_balance_sync_never_double_counts():
    """
    Regression for the user-reported bug: with the old code we'd compute
    equity = balance + internally_tracked_exposure, which could
    double-count or mis-count relative to Kalshi's actual account.
    """
    rm = RiskManager(starting_equity_cents=0)
    # Set internal tracking to a stale value
    rm.state.total_exposure_cents = 9999
    # New sync with portfolio_value: should IGNORE internal tracking
    rm.sync_actual_balance(balance_cents=2000, portfolio_value_cents=5000)
    assert rm.state.current_equity_cents == 5000  # not 5000 + 9999
    # And internal tracking should be replaced with the real derived value
    assert rm.state.total_exposure_cents == 3000  # 5000 - 2000


def test_balance_sync_updates_peak():
    rm = RiskManager(starting_equity_cents=5000)
    rm.state.peak_equity_cents = 5000
    rm.state.current_equity_cents = 5000
    rm.sync_actual_balance(
        balance_cents=6000, portfolio_value_cents=6000,
    )
    assert rm.state.peak_equity_cents == 6000
    rm.sync_actual_balance(
        balance_cents=5500, portfolio_value_cents=5500,
    )
    assert rm.state.peak_equity_cents == 6000  # peak preserved


def test_balance_sync_triggers_drawdown_kill():
    config = RiskConfig(max_drawdown_pct=10.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.peak_equity_cents = 10000
    rm.sync_actual_balance(
        balance_cents=8900, portfolio_value_cents=8900,
    )
    assert rm.state.kill_switch_active


def test_balance_sync_computes_daily_pnl_from_portfolio():
    rm = RiskManager(starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000
    rm.state.daily_date = time.strftime("%Y-%m-%d")
    rm.sync_actual_balance(
        balance_cents=5000, portfolio_value_cents=10300,
    )
    assert rm.state.daily_pnl_cents == 300


def test_reset_daily_rebaselines_peak():
    """reset_daily should bring peak back to current equity."""
    rm = RiskManager(starting_equity_cents=10000)
    rm.state.peak_equity_cents = 15000  # stale high from before
    rm.state.current_equity_cents = 10800
    rm.reset_daily()
    assert rm.state.peak_equity_cents == 10800
    assert rm.state.daily_starting_balance_cents == 10800
    assert rm.state.daily_pnl_cents == 0


# --- Dynamic per-trade limit (% of equity) ---

def test_per_trade_pct_only():
    """Only pct set: limit = pct * equity / 100."""
    config = RiskConfig(max_per_trade_cents=None, max_per_trade_pct=2.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    assert rm._effective_per_trade_limit() == 200.0  # 2% of 10000c = 200c


def test_per_trade_pct_scales_with_equity():
    """As equity grows, the per-trade limit grows with it."""
    config = RiskConfig(max_per_trade_cents=None, max_per_trade_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    assert rm._effective_per_trade_limit() == 500.0  # 5% of 10000

    # Simulate equity growing to 20000
    rm.sync_actual_balance(20000)
    assert rm._effective_per_trade_limit() == 1000.0  # 5% of 20000


def test_per_trade_pct_shrinks_with_equity():
    """As equity shrinks, the per-trade limit shrinks with it."""
    config = RiskConfig(max_per_trade_cents=None, max_per_trade_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.sync_actual_balance(6000)  # lost $40
    assert rm._effective_per_trade_limit() == 300.0  # 5% of 6000


def test_per_trade_both_uses_smaller():
    """If both set, use the more restrictive (smaller) value."""
    # pct gives 200, cents gives 500 → should use 200
    config = RiskConfig(max_per_trade_cents=500, max_per_trade_pct=2.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    assert rm._effective_per_trade_limit() == 200.0

    # pct gives 500, cents gives 300 → should use 300
    config = RiskConfig(max_per_trade_cents=300, max_per_trade_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    assert rm._effective_per_trade_limit() == 300.0


def test_per_trade_pct_blocks_oversized_trade():
    """A trade larger than the dynamic pct limit should be blocked."""
    config = RiskConfig(max_per_trade_cents=None, max_per_trade_pct=2.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    # pct limit = 200c. Trade cost = 300c → blocked
    opp = make_opp(total_cost=300)
    decision = rm.check_trade(opp)
    assert decision.action == RiskAction.BLOCK_PER_TRADE
    assert "2.0% of equity" in decision.reason


def test_per_trade_pct_allows_fitting_trade():
    config = RiskConfig(max_per_trade_cents=None, max_per_trade_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    opp = make_opp(total_cost=300)  # under 500 limit
    decision = rm.check_trade(opp)
    assert decision.allowed


# --- Dynamic daily loss limit (% of daily start) ---

def test_daily_loss_pct_only():
    config = RiskConfig(daily_loss_limit_cents=None, daily_loss_limit_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000
    assert rm._effective_daily_loss_limit() == 500.0  # 5% of 10000


def test_daily_loss_pct_uses_daily_start_not_current():
    """
    Daily loss limit should be computed from TODAY'S STARTING balance,
    not current equity. This prevents the limit from shrinking mid-day
    as you take losses.
    """
    config = RiskConfig(daily_loss_limit_cents=None, daily_loss_limit_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000
    rm.state.current_equity_cents = 9500  # lost 500 today

    # Limit is still 5% of 10000 = 500, not 5% of 9500
    assert rm._effective_daily_loss_limit() == 500.0


def test_daily_loss_pct_blocks_when_hit():
    config = RiskConfig(daily_loss_limit_cents=None, daily_loss_limit_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000
    rm.state.daily_date = time.strftime("%Y-%m-%d")
    rm.state.daily_pnl_cents = -500  # exactly at 5% loss

    decision = rm.check_trade(make_opp())
    assert decision.action == RiskAction.BLOCK_DAILY_LOSS
    assert "5.0% of daily start" in decision.reason


def test_daily_loss_both_uses_smaller():
    # pct gives 500, cents gives 2000 → use 500
    config = RiskConfig(daily_loss_limit_cents=2000, daily_loss_limit_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000
    assert rm._effective_daily_loss_limit() == 500.0

    # pct gives 500, cents gives 300 → use 300
    config = RiskConfig(daily_loss_limit_cents=300, daily_loss_limit_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000
    assert rm._effective_daily_loss_limit() == 300.0


def test_dynamic_limits_in_status():
    """get_status should expose effective limits for dashboard display."""
    config = RiskConfig(max_per_trade_cents=None, max_per_trade_pct=2.0,
                        daily_loss_limit_cents=None, daily_loss_limit_pct=5.0)
    rm = RiskManager(config=config, starting_equity_cents=10000)
    rm.state.daily_starting_balance_cents = 10000

    status = rm.get_status()
    assert status["effective_per_trade_limit_cents"] == 200  # 2% of 10000
    assert status["effective_daily_loss_limit_cents"] == 500  # 5% of 10000
    assert "2.0% of equity" in status["per_trade_limit_basis"]
    assert "5.0% of daily start" in status["daily_loss_limit_basis"]


if __name__ == "__main__":
    test_kill_switch_blocks_trades()
    test_kill_switch_can_be_deactivated()
    test_drawdown_pct_triggers_kill_switch()
    test_drawdown_absolute_triggers_kill_switch()
    test_drawdown_default_is_session_start_not_peak()
    test_drawdown_peak_mode_still_works_when_configured()
    test_drawdown_session_start_triggers_only_on_real_losses()
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
    test_balance_sync_initializes_equity_with_portfolio_value()
    test_balance_sync_falls_back_to_balance_if_no_portfolio_value()
    test_balance_sync_never_double_counts()
    test_balance_sync_updates_peak()
    test_balance_sync_triggers_drawdown_kill()
    test_balance_sync_computes_daily_pnl_from_portfolio()
    test_reset_daily_rebaselines_peak()
    test_per_trade_pct_only()
    test_per_trade_pct_scales_with_equity()
    test_per_trade_pct_shrinks_with_equity()
    test_per_trade_both_uses_smaller()
    test_per_trade_pct_blocks_oversized_trade()
    test_per_trade_pct_allows_fitting_trade()
    test_daily_loss_pct_only()
    test_daily_loss_pct_uses_daily_start_not_current()
    test_daily_loss_pct_blocks_when_hit()
    test_daily_loss_both_uses_smaller()
    test_dynamic_limits_in_status()
    print("All 39 risk manager tests passed!")
