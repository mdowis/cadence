"""
Risk Manager for Kalshi Arbitrage Trading

Provides position sizing, drawdown kill switches, daily/session loss limits,
exposure caps, and a full audit trail. Designed to gate every trade decision
before it reaches the exchange.

All monetary values are in cents unless noted otherwise.
"""

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Lock


class RiskAction(Enum):
    ALLOW = "allow"
    BLOCK_DRAWDOWN = "block:drawdown_limit"
    BLOCK_DAILY_LOSS = "block:daily_loss_limit"
    BLOCK_POSITION = "block:max_position_size"
    BLOCK_EXPOSURE = "block:max_total_exposure"
    BLOCK_PER_TRADE = "block:max_per_trade"
    BLOCK_KILL_SWITCH = "block:kill_switch_active"
    BLOCK_CONSECUTIVE = "block:consecutive_loss_limit"


@dataclass
class RiskConfig:
    """
    All limits in cents unless noted. Set any to None to disable that check.
    """
    # -- Kill switch (trailing drawdown from peak equity) --
    max_drawdown_pct: float = 10.0          # 10% from peak → kill switch
    max_drawdown_cents: int = None          # Absolute drawdown cap (e.g. 5000 = $50)

    # -- Daily limits --
    # daily_loss_limit_cents: flat cents cap (hardcoded)
    # daily_loss_limit_pct:   % of today's starting balance (dynamic, trails equity)
    # If BOTH are set, the more restrictive (smaller) one wins.
    daily_loss_limit_cents: int = 2000      # Max loss per day in cents ($20)
    daily_loss_limit_pct: float = None      # Max loss per day as % of daily start
    daily_trade_limit: int = 100            # Max number of trades per day

    # -- Per-trade limits --
    # max_per_trade_cents: flat cents cap (hardcoded)
    # max_per_trade_pct:   % of current equity (dynamic, trails equity)
    # If BOTH are set, the more restrictive (smaller) one wins.
    max_per_trade_cents: int = 500          # Max cost per trade in cents ($5)
    max_per_trade_pct: float = None         # Max cost per trade as % of equity
    max_contracts_per_leg: int = 50         # Max contracts on any single leg

    # -- Exposure limits --
    max_total_exposure_cents: int = 10000   # Max total capital at risk ($100)
    max_position_per_event_cents: int = 2000  # Max exposure to one event ($20)

    # -- Consecutive loss circuit breaker --
    max_consecutive_losses: int = 5         # After N losses in a row, pause trading
    cooldown_after_consecutive_secs: int = 300  # 5 min cooldown after breaker trips

    # -- Minimum thresholds --
    # IMPORTANT: min profit should exceed the expected partial-fill unwind
    # cost. If one leg fills and the other doesn't, we sell the filled
    # leg at the bid, paying the bid-ask spread (~2c per leg) plus taker
    # fees on both the buy and the sell (~2c each). That's ~6c per
    # contract in worst-case unwind cost for a binary arb. A 1c "arb"
    # would actually lose money after any partial fill.
    min_net_profit_cents: float = 5.0       # Don't trade arbs below this net profit
    min_roi_pct: float = 1.0                # Don't trade arbs below this ROI%

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TradeRecord:
    timestamp: float
    event_ticker: str
    arb_type: str
    legs: list               # [{"ticker": ..., "side": "yes"/"no", "price": ..., "contracts": ...}]
    total_cost_cents: float
    expected_profit_cents: float
    actual_pnl_cents: float = None  # Filled when position resolves
    status: str = "open"            # open, won, lost, cancelled


@dataclass
class RiskState:
    """Mutable state tracked across the session."""
    starting_equity_cents: int = 0
    peak_equity_cents: int = 0
    current_equity_cents: int = 0

    # Real balance tracking (synced from Kalshi /portfolio/balance)
    last_synced_balance_cents: int = 0
    last_balance_sync_time: float = 0.0
    balance_sync_count: int = 0

    # Daily tracking (reset each calendar day)
    daily_date: str = ""
    daily_starting_balance_cents: int = 0  # Balance at start of today
    daily_pnl_cents: float = 0.0
    daily_trade_count: int = 0

    # Open positions
    open_positions: list = field(default_factory=list)  # list of TradeRecord dicts
    total_exposure_cents: float = 0.0
    exposure_by_event: dict = field(default_factory=dict)  # event_ticker → cents

    # Consecutive loss tracking
    consecutive_losses: int = 0
    last_consecutive_breaker_time: float = 0.0

    # Kill switch
    kill_switch_active: bool = False
    kill_switch_reason: str = ""
    kill_switch_time: float = 0.0

    # Audit trail
    trade_history: list = field(default_factory=list)  # list of TradeRecord dicts
    risk_events: list = field(default_factory=list)     # list of risk action logs


class RiskManager:
    """
    Gates every trade through risk checks. Thread-safe.

    Usage:
        rm = RiskManager(config, starting_equity_cents=50000)  # $500
        decision = rm.check_trade(opportunity, contracts=10)
        if decision.action == RiskAction.ALLOW:
            # execute trade
            rm.record_trade_opened(...)
        else:
            print(f"Blocked: {decision.reason}")
    """

    def __init__(self, config: RiskConfig = None, starting_equity_cents: int = 0,
                 state_file: str = None):
        self.config = config or RiskConfig()
        self.state = RiskState(
            starting_equity_cents=starting_equity_cents,
            peak_equity_cents=starting_equity_cents,
            current_equity_cents=starting_equity_cents,
            daily_starting_balance_cents=starting_equity_cents,
            daily_date=time.strftime("%Y-%m-%d"),
        )
        self.state_file = state_file
        self._lock = Lock()

        # Restore state if file exists
        if state_file and os.path.exists(state_file):
            self._load_state()

    # ------------------------------------------------------------------
    # Core risk check — call this before every trade
    # ------------------------------------------------------------------

    def check_trade(self, opportunity, contracts=1):
        """
        Evaluate whether a trade should proceed.

        Args:
            opportunity: ArbitrageOpportunity from the scanner
            contracts: Number of contracts per leg

        Returns:
            RiskDecision with action and reason
        """
        with self._lock:
            self._maybe_reset_daily()

            # 1. Kill switch
            if self.state.kill_switch_active:
                return RiskDecision(
                    RiskAction.BLOCK_KILL_SWITCH,
                    f"Kill switch active since {time.ctime(self.state.kill_switch_time)}: "
                    f"{self.state.kill_switch_reason}"
                )

            # 2. Consecutive loss cooldown
            if self.state.consecutive_losses >= self.config.max_consecutive_losses:
                elapsed = time.time() - self.state.last_consecutive_breaker_time
                if elapsed < self.config.cooldown_after_consecutive_secs:
                    remaining = int(self.config.cooldown_after_consecutive_secs - elapsed)
                    return RiskDecision(
                        RiskAction.BLOCK_CONSECUTIVE,
                        f"{self.state.consecutive_losses} consecutive losses. "
                        f"Cooldown: {remaining}s remaining"
                    )
                else:
                    # Cooldown expired, reset
                    self.state.consecutive_losses = 0

            trade_cost = opportunity.total_cost * contracts
            event_ticker = opportunity.event_ticker

            # 3. Per-trade size limit (dynamic: % of equity, static cents, or both)
            per_trade_limit = self._effective_per_trade_limit()
            if per_trade_limit is not None and trade_cost > per_trade_limit:
                return RiskDecision(
                    RiskAction.BLOCK_PER_TRADE,
                    f"Trade cost {trade_cost}¢ exceeds per-trade limit "
                    f"{int(per_trade_limit)}¢ "
                    f"(basis: {self._per_trade_limit_basis()})"
                )

            # 4. Daily loss limit (dynamic: % of daily start, static cents, or both)
            daily_limit = self._effective_daily_loss_limit()
            if daily_limit is not None and self.state.daily_pnl_cents <= -daily_limit:
                return RiskDecision(
                    RiskAction.BLOCK_DAILY_LOSS,
                    f"Daily P&L {self.state.daily_pnl_cents}¢ hit limit "
                    f"-{int(daily_limit)}¢ "
                    f"(basis: {self._daily_loss_limit_basis()})"
                )

            # 5. Daily trade count
            if self.config.daily_trade_limit:
                if self.state.daily_trade_count >= self.config.daily_trade_limit:
                    return RiskDecision(
                        RiskAction.BLOCK_DAILY_LOSS,
                        f"Daily trade count {self.state.daily_trade_count} hit limit "
                        f"{self.config.daily_trade_limit}"
                    )

            # 6. Total exposure
            new_exposure = self.state.total_exposure_cents + trade_cost
            if self.config.max_total_exposure_cents and \
               new_exposure > self.config.max_total_exposure_cents:
                return RiskDecision(
                    RiskAction.BLOCK_EXPOSURE,
                    f"Total exposure would be {new_exposure}¢, exceeds limit "
                    f"{self.config.max_total_exposure_cents}¢"
                )

            # 7. Per-event exposure
            event_exposure = self.state.exposure_by_event.get(event_ticker, 0) + trade_cost
            if self.config.max_position_per_event_cents and \
               event_exposure > self.config.max_position_per_event_cents:
                return RiskDecision(
                    RiskAction.BLOCK_POSITION,
                    f"Event {event_ticker} exposure would be {event_exposure}¢, "
                    f"exceeds limit {self.config.max_position_per_event_cents}¢"
                )

            # 8. Drawdown check
            drawdown_result = self._check_drawdown()
            if drawdown_result:
                return drawdown_result

            # 9. Minimum profitability thresholds
            if opportunity.net_profit_cents < self.config.min_net_profit_cents:
                return RiskDecision(
                    RiskAction.BLOCK_PER_TRADE,
                    f"Net profit {opportunity.net_profit_cents}¢ below minimum "
                    f"{self.config.min_net_profit_cents}¢"
                )
            if opportunity.roi_percent < self.config.min_roi_pct:
                return RiskDecision(
                    RiskAction.BLOCK_PER_TRADE,
                    f"ROI {opportunity.roi_percent:.2f}% below minimum "
                    f"{self.config.min_roi_pct}%"
                )

            return RiskDecision(RiskAction.ALLOW, "All checks passed")

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------

    def record_trade_opened(self, opportunity, contracts=1):
        """Record that a trade was executed."""
        with self._lock:
            trade_cost = opportunity.total_cost * contracts
            record = {
                "timestamp": time.time(),
                "event_ticker": opportunity.event_ticker,
                "arb_type": opportunity.type,
                "legs": opportunity.markets,
                "contracts": contracts,
                "total_cost_cents": trade_cost,
                "expected_profit_cents": opportunity.net_profit_cents * contracts,
                "actual_pnl_cents": None,
                "status": "open",
            }

            self.state.open_positions.append(record)
            self.state.total_exposure_cents += trade_cost
            event = opportunity.event_ticker
            self.state.exposure_by_event[event] = \
                self.state.exposure_by_event.get(event, 0) + trade_cost
            self.state.daily_trade_count += 1
            self.state.trade_history.append(record)
            self._persist()

    def record_trade_closed(self, event_ticker, actual_pnl_cents):
        """Record that a position resolved (won or lost)."""
        with self._lock:
            # Find and close the position
            for pos in self.state.open_positions:
                if pos["event_ticker"] == event_ticker and pos["status"] == "open":
                    pos["actual_pnl_cents"] = actual_pnl_cents
                    pos["status"] = "won" if actual_pnl_cents > 0 else "lost"

                    # Update exposure
                    cost = pos["total_cost_cents"]
                    self.state.total_exposure_cents = max(
                        0, self.state.total_exposure_cents - cost)
                    self.state.exposure_by_event[event_ticker] = max(
                        0, self.state.exposure_by_event.get(event_ticker, 0) - cost)

                    # Update equity
                    self.state.current_equity_cents += actual_pnl_cents
                    if self.state.current_equity_cents > self.state.peak_equity_cents:
                        self.state.peak_equity_cents = self.state.current_equity_cents

                    # Update daily P&L
                    self.state.daily_pnl_cents += actual_pnl_cents

                    # Consecutive loss tracking
                    if actual_pnl_cents < 0:
                        self.state.consecutive_losses += 1
                        if self.state.consecutive_losses >= self.config.max_consecutive_losses:
                            self.state.last_consecutive_breaker_time = time.time()
                            self._log_risk_event(
                                "consecutive_loss_breaker",
                                f"{self.state.consecutive_losses} consecutive losses"
                            )
                    else:
                        self.state.consecutive_losses = 0

                    # Update history
                    for h in self.state.trade_history:
                        if h is pos:
                            h["actual_pnl_cents"] = actual_pnl_cents
                            h["status"] = pos["status"]

                    # Check drawdown after P&L update
                    self._check_drawdown()
                    break

            # Remove closed positions
            self.state.open_positions = [
                p for p in self.state.open_positions if p["status"] == "open"
            ]
            self._persist()

    # ------------------------------------------------------------------
    # Balance sync (from Kalshi /portfolio/balance)
    # ------------------------------------------------------------------

    def sync_actual_balance(self, balance_cents, portfolio_value_cents=None):
        """
        Sync current equity from the exchange (source of truth).

        Kalshi's /portfolio/balance returns:
          - balance: available cash only (cents)
          - portfolio_value: total equity = cash + mark-to-market position value

        If portfolio_value_cents is provided, we use it directly as the equity.
        That's Kalshi's actual total account value and the correct number to
        display and use for drawdown / P&L.

        If portfolio_value is not provided (legacy or old API), fall back to
        just the cash balance (undercount but safer than double-counting).

        Also derives real open-position exposure as (portfolio_value - balance)
        and uses that for the exposure cap instead of the internally tracked
        cost basis (which can drift from reality).

        On first call, initializes starting and peak equity.
        On subsequent calls, reconciles daily P&L and checks drawdown.
        """
        with self._lock:
            self._maybe_reset_daily_internal()

            if portfolio_value_cents is not None and portfolio_value_cents > 0:
                total_equity = int(portfolio_value_cents)
                # Derive the real position value directly from Kalshi's numbers
                real_exposure = max(0, total_equity - int(balance_cents))
                self.state.total_exposure_cents = real_exposure
            else:
                # Fallback: no portfolio_value, just use cash. Undercounts
                # equity if there are open positions but doesn't double-count.
                total_equity = int(balance_cents)

            # First-time initialization: treat current total as baseline
            if self.state.starting_equity_cents == 0:
                self.state.starting_equity_cents = total_equity
                self.state.peak_equity_cents = total_equity
                self.state.daily_starting_balance_cents = total_equity
                self.state.current_equity_cents = total_equity
                self._log_risk_event(
                    "balance_initialized",
                    f"Starting equity set from Kalshi: {total_equity}c "
                    f"(cash={balance_cents}c, "
                    f"portfolio_value={portfolio_value_cents}c)"
                )
            else:
                self.state.current_equity_cents = total_equity

                # Update peak
                if total_equity > self.state.peak_equity_cents:
                    self.state.peak_equity_cents = total_equity

                # Daily P&L relative to today's opening balance
                if self.state.daily_starting_balance_cents > 0:
                    self.state.daily_pnl_cents = \
                        total_equity - self.state.daily_starting_balance_cents

            self.state.last_synced_balance_cents = balance_cents
            self.state.last_balance_sync_time = time.time()
            self.state.balance_sync_count += 1

            # Check drawdown after sync - may trigger kill switch
            self._check_drawdown()
            self._persist()

    # ------------------------------------------------------------------
    # Kill switch controls
    # ------------------------------------------------------------------

    def activate_kill_switch(self, reason="Manual activation"):
        """Immediately halt all trading."""
        with self._lock:
            self.state.kill_switch_active = True
            self.state.kill_switch_reason = reason
            self.state.kill_switch_time = time.time()
            self._log_risk_event("kill_switch_activated", reason)
            self._persist()

    def deactivate_kill_switch(self):
        """Resume trading (requires explicit action)."""
        with self._lock:
            self.state.kill_switch_active = False
            self.state.kill_switch_reason = ""
            self._log_risk_event("kill_switch_deactivated", "Manual reset")
            self._persist()

    def reset_daily(self):
        """
        Manually reset daily counters AND re-baseline to the current equity.

        Use this after a bug fix or config change that makes the old
        baseline stale (e.g., equity is now computed differently).
        """
        with self._lock:
            self.state.daily_pnl_cents = 0.0
            self.state.daily_trade_count = 0
            self.state.daily_date = time.strftime("%Y-%m-%d")
            # Re-baseline: today's starting balance and peak now match
            # whatever the current equity actually is. This clears stale
            # peaks from before the last equity recalculation bug fix.
            current = self.state.current_equity_cents
            self.state.daily_starting_balance_cents = current
            self.state.peak_equity_cents = current
            if self.state.starting_equity_cents == 0:
                self.state.starting_equity_cents = current
            self._log_risk_event(
                "daily_reset",
                f"Baseline reset to ${current/100:.2f}"
            )
            self._persist()

    # ------------------------------------------------------------------
    # Status / reporting
    # ------------------------------------------------------------------

    def get_status(self):
        """Return a snapshot of current risk state for the dashboard."""
        with self._lock:
            equity = self.state.current_equity_cents
            peak = self.state.peak_equity_cents
            drawdown_cents = peak - equity
            drawdown_pct = (drawdown_cents / peak * 100) if peak > 0 else 0

            per_trade_limit = self._effective_per_trade_limit()
            daily_limit = self._effective_daily_loss_limit()

            return {
                "equity_cents": equity,
                "peak_equity_cents": peak,
                "starting_equity_cents": self.state.starting_equity_cents,
                "cash_balance_cents": self.state.last_synced_balance_cents,
                "last_balance_sync_time": self.state.last_balance_sync_time,
                "balance_sync_count": self.state.balance_sync_count,
                "daily_starting_balance_cents": self.state.daily_starting_balance_cents,
                "drawdown_cents": drawdown_cents,
                "drawdown_pct": round(drawdown_pct, 2),
                "daily_pnl_cents": self.state.daily_pnl_cents,
                "daily_trade_count": self.state.daily_trade_count,
                "total_exposure_cents": self.state.total_exposure_cents,
                "exposure_by_event": dict(self.state.exposure_by_event),
                "open_position_count": len(self.state.open_positions),
                "consecutive_losses": self.state.consecutive_losses,
                "kill_switch_active": self.state.kill_switch_active,
                "kill_switch_reason": self.state.kill_switch_reason,
                "total_trades": len(self.state.trade_history),
                "config": self.config.to_dict(),
                "risk_events": self.state.risk_events[-20:],  # last 20
                # Effective (dynamic) limits - what's actually being enforced now
                "effective_per_trade_limit_cents": (
                    int(per_trade_limit) if per_trade_limit is not None else None
                ),
                "per_trade_limit_basis": self._per_trade_limit_basis(),
                "effective_daily_loss_limit_cents": (
                    int(daily_limit) if daily_limit is not None else None
                ),
                "daily_loss_limit_basis": self._daily_loss_limit_basis(),
            }

    # ------------------------------------------------------------------
    # Dynamic limit computation
    # ------------------------------------------------------------------

    def _effective_per_trade_limit(self):
        """
        Compute the effective max per-trade cost in cents.

        Rules:
          - If only max_per_trade_cents is set: return it (flat)
          - If only max_per_trade_pct is set: return pct * current_equity / 100
          - If BOTH are set: return the smaller (more restrictive) value
          - If NEITHER is set: return None (no per-trade limit)
        """
        cents_limit = self.config.max_per_trade_cents
        pct = self.config.max_per_trade_pct
        pct_limit = None
        if pct and pct > 0:
            equity = self.state.current_equity_cents
            if equity > 0:
                pct_limit = (pct / 100.0) * equity

        if cents_limit and pct_limit is not None:
            return min(cents_limit, pct_limit)
        if cents_limit:
            return float(cents_limit)
        if pct_limit is not None:
            return pct_limit
        return None

    def _per_trade_limit_basis(self):
        """Human-readable description of which limit is active right now."""
        cents_limit = self.config.max_per_trade_cents
        pct = self.config.max_per_trade_pct
        equity = self.state.current_equity_cents
        pct_val = (pct / 100.0) * equity if (pct and equity > 0) else None

        if cents_limit and pct_val is not None:
            return (f"{pct}% of equity" if pct_val < cents_limit
                    else f"{cents_limit}c flat")
        if pct_val is not None:
            return f"{pct}% of equity"
        if cents_limit:
            return f"{cents_limit}c flat"
        return "none"

    def _effective_daily_loss_limit(self):
        """
        Compute the effective daily loss limit in cents.

        Rules mirror per-trade: if both cents and pct set, use the smaller.
        pct is computed against today's STARTING balance (captured at midnight).
        """
        cents_limit = self.config.daily_loss_limit_cents
        pct = self.config.daily_loss_limit_pct
        pct_limit = None
        if pct and pct > 0:
            daily_start = self.state.daily_starting_balance_cents
            if daily_start > 0:
                pct_limit = (pct / 100.0) * daily_start

        if cents_limit and pct_limit is not None:
            return min(cents_limit, pct_limit)
        if cents_limit:
            return float(cents_limit)
        if pct_limit is not None:
            return pct_limit
        return None

    def _daily_loss_limit_basis(self):
        """Human-readable description of which limit is active right now."""
        cents_limit = self.config.daily_loss_limit_cents
        pct = self.config.daily_loss_limit_pct
        daily_start = self.state.daily_starting_balance_cents
        pct_val = ((pct / 100.0) * daily_start
                   if (pct and daily_start > 0) else None)

        if cents_limit and pct_val is not None:
            return (f"{pct}% of daily start" if pct_val < cents_limit
                    else f"{cents_limit}c flat")
        if pct_val is not None:
            return f"{pct}% of daily start"
        if cents_limit:
            return f"{cents_limit}c flat"
        return "none"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_drawdown(self):
        """Check if drawdown exceeds limits. Activates kill switch if so."""
        equity = self.state.current_equity_cents
        peak = self.state.peak_equity_cents

        if peak <= 0:
            return None

        drawdown_cents = peak - equity
        drawdown_pct = (drawdown_cents / peak) * 100

        # Percentage drawdown
        if self.config.max_drawdown_pct and drawdown_pct >= self.config.max_drawdown_pct:
            reason = (f"Drawdown {drawdown_pct:.1f}% hit limit "
                      f"{self.config.max_drawdown_pct}% "
                      f"(peak: {peak}¢, current: {equity}¢)")
            self.state.kill_switch_active = True
            self.state.kill_switch_reason = reason
            self.state.kill_switch_time = time.time()
            self._log_risk_event("kill_switch_drawdown_pct", reason)
            self._persist()
            return RiskDecision(RiskAction.BLOCK_DRAWDOWN, reason)

        # Absolute drawdown
        if self.config.max_drawdown_cents and drawdown_cents >= self.config.max_drawdown_cents:
            reason = (f"Drawdown {drawdown_cents}¢ hit limit "
                      f"{self.config.max_drawdown_cents}¢")
            self.state.kill_switch_active = True
            self.state.kill_switch_reason = reason
            self.state.kill_switch_time = time.time()
            self._log_risk_event("kill_switch_drawdown_abs", reason)
            self._persist()
            return RiskDecision(RiskAction.BLOCK_DRAWDOWN, reason)

        return None

    def _maybe_reset_daily(self):
        """Auto-reset daily counters at midnight."""
        self._maybe_reset_daily_internal()

    def _maybe_reset_daily_internal(self):
        """Auto-reset daily counters at midnight (no lock - caller must hold)."""
        today = time.strftime("%Y-%m-%d")
        if self.state.daily_date != today:
            self.state.daily_date = today
            self.state.daily_pnl_cents = 0.0
            self.state.daily_trade_count = 0
            # Capture today's starting balance for accurate daily P&L
            self.state.daily_starting_balance_cents = self.state.current_equity_cents

    def _log_risk_event(self, event_type, detail):
        self.state.risk_events.append({
            "time": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            "detail": detail,
        })

    def _persist(self):
        """Save state to disk if state_file is configured."""
        if not self.state_file:
            return
        try:
            data = {
                "starting_equity_cents": self.state.starting_equity_cents,
                "peak_equity_cents": self.state.peak_equity_cents,
                "current_equity_cents": self.state.current_equity_cents,
                "last_synced_balance_cents": self.state.last_synced_balance_cents,
                "last_balance_sync_time": self.state.last_balance_sync_time,
                "balance_sync_count": self.state.balance_sync_count,
                "daily_starting_balance_cents": self.state.daily_starting_balance_cents,
                "daily_date": self.state.daily_date,
                "daily_pnl_cents": self.state.daily_pnl_cents,
                "daily_trade_count": self.state.daily_trade_count,
                "total_exposure_cents": self.state.total_exposure_cents,
                "exposure_by_event": self.state.exposure_by_event,
                "consecutive_losses": self.state.consecutive_losses,
                "kill_switch_active": self.state.kill_switch_active,
                "kill_switch_reason": self.state.kill_switch_reason,
                "kill_switch_time": self.state.kill_switch_time,
                "open_positions": self.state.open_positions,
                "trade_history": self.state.trade_history[-500:],  # cap history
                "risk_events": self.state.risk_events[-200:],
            }
            path = Path(self.state_file)
            path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"  WARNING: Failed to persist risk state: {e}")

    def _load_state(self):
        """Load state from disk."""
        try:
            data = json.loads(Path(self.state_file).read_text())
            self.state.starting_equity_cents = data.get("starting_equity_cents", 0)
            self.state.peak_equity_cents = data.get("peak_equity_cents", 0)
            self.state.current_equity_cents = data.get("current_equity_cents", 0)
            self.state.last_synced_balance_cents = data.get("last_synced_balance_cents", 0)
            self.state.last_balance_sync_time = data.get("last_balance_sync_time", 0)
            self.state.balance_sync_count = data.get("balance_sync_count", 0)
            self.state.daily_starting_balance_cents = data.get("daily_starting_balance_cents", 0)
            self.state.daily_date = data.get("daily_date", "")
            self.state.daily_pnl_cents = data.get("daily_pnl_cents", 0)
            self.state.daily_trade_count = data.get("daily_trade_count", 0)
            self.state.total_exposure_cents = data.get("total_exposure_cents", 0)
            self.state.exposure_by_event = data.get("exposure_by_event", {})
            self.state.consecutive_losses = data.get("consecutive_losses", 0)
            self.state.kill_switch_active = data.get("kill_switch_active", False)
            self.state.kill_switch_reason = data.get("kill_switch_reason", "")
            self.state.kill_switch_time = data.get("kill_switch_time", 0)
            self.state.open_positions = data.get("open_positions", [])
            self.state.trade_history = data.get("trade_history", [])
            self.state.risk_events = data.get("risk_events", [])
            print(f"  Restored risk state from {self.state_file} "
                  f"(equity: {self.state.current_equity_cents}¢, "
                  f"kill_switch: {self.state.kill_switch_active})")
        except Exception as e:
            print(f"  WARNING: Failed to load risk state: {e}")


@dataclass
class RiskDecision:
    action: RiskAction
    reason: str

    @property
    def allowed(self):
        return self.action == RiskAction.ALLOW

    def __str__(self):
        symbol = "ALLOW" if self.allowed else "BLOCK"
        return f"[{symbol}] {self.reason}"
