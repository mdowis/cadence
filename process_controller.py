"""
Process controller for the Cadence dashboard.

Manages background threads for:
  - The market scanner (fetches Kalshi markets and runs arb detection)
  - The auto-executor (places trades on opportunities, gated by risk manager)

Both are controlled via dashboard API endpoints (start/stop) and expose
their status for display.
"""

import threading
import time
import traceback
from dataclasses import dataclass, field


@dataclass
class ProcessStatus:
    status: str = "stopped"          # stopped, starting, running, stopping, error
    started_at: float = 0.0
    last_run_at: float = 0.0
    last_error: str = ""
    run_count: int = 0
    trades_placed: int = 0           # executor only
    last_detail: str = ""

    def to_dict(self):
        return {
            "status": self.status,
            "started_at": self.started_at,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "trades_placed": self.trades_placed,
            "last_detail": self.last_detail,
            "uptime_seconds": (time.time() - self.started_at) if self.started_at else 0,
        }


class ProcessController:
    """
    Manages scanner and executor background threads for the dashboard.

    The scanner runs `scan_callback(markets)` with fetched market data.
    The executor runs `execute_callback(opportunities)` with scan results.
    """

    def __init__(self, trader, risk_mgr, scan_callback, execute_callback,
                 min_profit=1, interval=15, dry_run=True):
        self.trader = trader
        self.risk_mgr = risk_mgr
        self.scan_callback = scan_callback      # fn(markets_list) -> list of opps
        self.execute_callback = execute_callback  # fn(opp, contracts) -> (success, detail)
        self.min_profit = min_profit
        self.interval = interval
        self.dry_run = dry_run
        self.contracts_per_leg = 1

        self._lock = threading.Lock()

        # Scanner state
        self._scanner_thread = None
        self._scanner_stop = threading.Event()
        self.scanner_status = ProcessStatus()

        # Executor state
        self._executor_thread = None
        self._executor_stop = threading.Event()
        self.executor_status = ProcessStatus()

        # Shared: most recent scan result for executor to consume
        self._latest_opportunities = []
        self._opportunities_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Scanner
    # ------------------------------------------------------------------

    def start_scanner(self, interval=None):
        with self._lock:
            if self._scanner_thread and self._scanner_thread.is_alive():
                return False, "Scanner already running"
            if interval:
                self.interval = interval
            if not self.trader or not self.trader.authenticated:
                return False, "Not authenticated - set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"

            self._scanner_stop.clear()
            self.scanner_status = ProcessStatus(
                status="starting",
                started_at=time.time(),
            )
            self._scanner_thread = threading.Thread(
                target=self._scanner_loop,
                daemon=True,
            )
            self._scanner_thread.start()
            return True, f"Scanner started (interval: {self.interval}s)"

    def stop_scanner(self):
        with self._lock:
            if not self._scanner_thread or not self._scanner_thread.is_alive():
                return False, "Scanner not running"
            self.scanner_status.status = "stopping"
            self._scanner_stop.set()
            return True, "Scanner stopping..."

    def _scanner_loop(self):
        self.scanner_status.status = "running"
        self.scanner_status.last_detail = "Starting..."
        print(f"  [scanner] Thread started", flush=True)

        while not self._scanner_stop.is_set():
            t_start = time.time()
            try:
                # Fetch markets
                print(f"  [scanner] Fetching open markets from Kalshi...", flush=True)
                self.scanner_status.last_detail = "Fetching markets..."
                markets = self.trader.get_all_markets()
                print(f"  [scanner] Got {len(markets)} markets in "
                      f"{time.time()-t_start:.1f}s", flush=True)

                if len(markets) == 0:
                    self.scanner_status.last_error = \
                        "Kalshi returned 0 markets. Check your API credentials."
                    self.scanner_status.last_detail = "Got 0 markets (auth issue?)"
                    print(f"  [scanner] WARNING: 0 markets returned", flush=True)
                else:
                    # Run arb detection (also updates latest scan shared state)
                    opps = self.scan_callback(markets)

                    with self._opportunities_lock:
                        self._latest_opportunities = opps

                    self.scanner_status.run_count += 1
                    self.scanner_status.last_run_at = time.time()
                    self.scanner_status.last_error = ""
                    self.scanner_status.last_detail = (
                        f"Scanned {len(markets)} markets, "
                        f"{len(opps)} opportunities ({time.time()-t_start:.1f}s)"
                    )
                    print(f"  [scanner] {self.scanner_status.last_detail}", flush=True)

                # Sync balance from Kalshi (source of truth)
                try:
                    balance_resp = self.trader.get_balance()
                    balance = balance_resp.get("balance", 0)
                    if balance:
                        self.risk_mgr.sync_actual_balance(balance)
                except Exception as e:
                    # Balance sync is non-fatal — don't kill the scanner
                    print(f"  [scanner] Balance sync failed: {e}", flush=True)

            except Exception as e:
                self.scanner_status.last_error = f"{type(e).__name__}: {e}"
                self.scanner_status.last_detail = f"ERROR: {e}"
                print(f"  [scanner] ERROR: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc()

            # Sleep, but wake up on stop signal
            if self._scanner_stop.wait(self.interval):
                break

        self.scanner_status.status = "stopped"
        self.scanner_status.last_detail = "Stopped"
        print(f"  [scanner] Thread stopped", flush=True)

    # ------------------------------------------------------------------
    # Executor
    # ------------------------------------------------------------------

    def start_executor(self, contracts=None, dry_run=None):
        with self._lock:
            if self._executor_thread and self._executor_thread.is_alive():
                return False, "Executor already running"
            if not self.trader or not self.trader.authenticated:
                return False, "Not authenticated - set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH"
            if not (self._scanner_thread and self._scanner_thread.is_alive()):
                return False, "Scanner must be running before starting executor"

            if contracts is not None:
                self.contracts_per_leg = contracts
            if dry_run is not None:
                self.dry_run = dry_run

            self._executor_stop.clear()
            self.executor_status = ProcessStatus(
                status="starting",
                started_at=time.time(),
            )
            self._executor_thread = threading.Thread(
                target=self._executor_loop,
                daemon=True,
            )
            self._executor_thread.start()
            mode = "DRY RUN" if self.dry_run else "LIVE"
            return True, f"Executor started ({mode}, {self.contracts_per_leg} contracts/leg)"

    def stop_executor(self):
        with self._lock:
            if not self._executor_thread or not self._executor_thread.is_alive():
                return False, "Executor not running"
            self.executor_status.status = "stopping"
            self._executor_stop.set()
            return True, "Executor stopping..."

    def _executor_loop(self):
        self.executor_status.status = "running"
        self.executor_status.last_detail = \
            f"Started ({'DRY RUN' if self.dry_run else 'LIVE'})"

        while not self._executor_stop.is_set():
            try:
                # Don't execute if kill switch is active
                if self.risk_mgr.state.kill_switch_active:
                    self.executor_status.last_detail = \
                        f"Kill switch active: {self.risk_mgr.state.kill_switch_reason}"
                    if self._executor_stop.wait(self.interval):
                        break
                    continue

                # Get latest opportunities from scanner
                with self._opportunities_lock:
                    opps = list(self._latest_opportunities)

                if not opps:
                    self.executor_status.last_detail = "No opportunities"
                else:
                    # Try to execute each (best first, scanner already sorted)
                    for opp in opps:
                        if self._executor_stop.is_set():
                            break

                        success, detail = self.execute_callback(
                            opp, self.contracts_per_leg, self.dry_run
                        )

                        if success:
                            self.executor_status.trades_placed += 1
                            self.executor_status.last_detail = \
                                f"Executed {opp.event_ticker}: {detail}"
                        else:
                            # Most blocks are risk-check rejections; log but continue
                            self.executor_status.last_detail = \
                                f"Skipped {opp.event_ticker}: {detail}"

                self.executor_status.run_count += 1
                self.executor_status.last_run_at = time.time()
                self.executor_status.last_error = ""

            except Exception as e:
                self.executor_status.last_error = str(e)
                self.executor_status.last_detail = f"Error: {e}"
                print(f"  Executor error: {e}")
                traceback.print_exc()

            if self._executor_stop.wait(self.interval):
                break

        self.executor_status.status = "stopped"
        self.executor_status.last_detail = "Stopped"

    # ------------------------------------------------------------------
    # Status for dashboard
    # ------------------------------------------------------------------

    def get_status(self):
        return {
            "scanner": self.scanner_status.to_dict(),
            "executor": self.executor_status.to_dict(),
            "authenticated": bool(self.trader and self.trader.authenticated),
            "config": {
                "interval": self.interval,
                "min_profit": self.min_profit,
                "contracts_per_leg": self.contracts_per_leg,
                "dry_run": self.dry_run,
            },
        }

    def update_config(self, interval=None, min_profit=None,
                      contracts=None, dry_run=None):
        """Update runtime config (takes effect on next scan/execute cycle)."""
        with self._lock:
            if interval is not None:
                self.interval = interval
            if min_profit is not None:
                self.min_profit = min_profit
            if contracts is not None:
                self.contracts_per_leg = contracts
            if dry_run is not None:
                self.dry_run = dry_run
