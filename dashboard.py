#!/usr/bin/env python3
"""
Cadence dashboard - single entry point for the entire system.

Loads configuration from .env, starts the web server, and exposes
process control endpoints so you can start/stop the scanner and
auto-executor from the browser.

Usage:
    # 1. Copy .env.example to .env and add your API key
    cp .env.example .env
    # 2. Edit .env: add KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
    # 3. Run
    python dashboard.py
    # 4. Open http://localhost:8050 and click "Start Scanner"
"""

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


# ---------------------------------------------------------------------------
# .env loader (no external dependency)
# ---------------------------------------------------------------------------

def load_dotenv(path=".env"):
    """
    Load a simple KEY=VALUE .env file into os.environ.

    Checks:
      1. Current working directory
      2. The directory of this script
    Existing environment variables take priority (env beats file).
    """
    candidates = [path]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, ".env"))

    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        with open(candidate) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val
        return os.path.abspath(candidate)
    return None


# Load .env BEFORE importing modules that may read env vars
_DOTENV_PATH = load_dotenv()


from kalshi_arbitrage import (
    DEMO_MARKETS,
    find_binary_arbitrage,
    find_multi_outcome_arbitrage,
    find_near_misses,
    kalshi_fee_per_contract,
    KALSHI_API_BASE,
)
from risk_manager import RiskManager, RiskConfig
from executor import KalshiTrader, execute_opportunity
from process_controller import ProcessController


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_latest_scan = {
    "timestamp": None,
    "markets": [],
    "opportunities": [],
    "near_misses": [],
    "summary": {},
    "mode": "demo",
    "error": None,
}
_scan_lock = threading.Lock()

# Global singletons, initialized in main()
_risk_mgr = None
_trader = None
_process_ctrl = None


def run_scan(markets, min_profit=1, mode="demo"):
    """Run scan and store results in shared state. Returns the opportunities."""
    binary_opps = find_binary_arbitrage(markets, min_profit)
    multi_opps = find_multi_outcome_arbitrage(markets, min_profit)
    all_opps = binary_opps + multi_opps
    all_opps.sort(key=lambda o: o.net_profit_cents, reverse=True)

    near = find_near_misses(markets)

    events = defaultdict(list)
    for m in markets:
        et = m.get("event_ticker")
        if et:
            events[et].append(m)
    multi_events = {k: v for k, v in events.items() if len(v) >= 2}

    opp_dicts = []
    for o in all_opps:
        opp_dicts.append({
            "type": o.type,
            "event_title": o.event_title,
            "event_ticker": o.event_ticker,
            "markets": o.markets,
            "total_cost": o.total_cost,
            "guaranteed_payout": o.guaranteed_payout,
            "profit_cents": o.profit_cents,
            "roi_percent": round(o.roi_percent, 2),
            "fee_cents": o.fee_cents,
            "net_profit_cents": o.net_profit_cents,
        })

    near_dicts = []
    for combined, m in near[:30]:
        near_dicts.append({
            "title": m.get("title", "?"),
            "ticker": m.get("ticker", "?"),
            "yes_ask": m.get("yes_ask"),
            "no_ask": m.get("no_ask"),
            "combined": combined,
            "spread": combined - 100,
        })

    with _scan_lock:
        _latest_scan.update({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "markets": [
                {
                    "ticker": m.get("ticker", "?"),
                    "title": m.get("title", "?"),
                    "event_ticker": m.get("event_ticker", "?"),
                    "event_title": m.get("event_title", "?"),
                    "yes_ask": m.get("yes_ask"),
                    "no_ask": m.get("no_ask"),
                    "yes_bid": m.get("yes_bid"),
                    "no_bid": m.get("no_bid"),
                }
                for m in markets
            ],
            "opportunities": opp_dicts,
            "near_misses": near_dicts,
            "summary": {
                "total_markets": len(markets),
                "multi_outcome_events": len(multi_events),
                "binary_arb_count": len(binary_opps),
                "multi_outcome_arb_count": len(multi_opps),
                "near_miss_count": len(near),
                "total_arb_count": len(all_opps),
            },
            "mode": mode,
            "error": None,
        })

    return all_opps


# ---------------------------------------------------------------------------
# Callbacks for ProcessController
# ---------------------------------------------------------------------------

def scan_callback(markets):
    """Called by the scanner thread after fetching markets."""
    min_profit = _process_ctrl.min_profit if _process_ctrl else 1
    return run_scan(markets, min_profit, mode="live")


def execute_callback(opp, contracts, dry_run):
    """Called by the executor thread for each opportunity."""
    return execute_opportunity(_trader, _risk_mgr, opp, contracts, dry_run)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class DashboardHandler(SimpleHTTPRequestHandler):
    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            return {}
        return json.loads(self.rfile.read(content_len))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = Path(__file__).parent / "dashboard.html"
            self.wfile.write(html_path.read_bytes())
        elif self.path == "/api/scan":
            with _scan_lock:
                payload = dict(_latest_scan)
            # Merge scanner error state so the frontend can show it prominently
            if _process_ctrl:
                scanner = _process_ctrl.scanner_status
                payload["scanner_status"] = scanner.status
                payload["scanner_error"] = scanner.last_error or ""
                payload["scanner_detail"] = scanner.last_detail or ""
            self._json(payload)
        elif self.path == "/api/risk":
            if _risk_mgr:
                self._json(_risk_mgr.get_status())
            else:
                self._json({"error": "Risk manager not initialized"})
        elif self.path == "/api/processes":
            if _process_ctrl:
                self._json(_process_ctrl.get_status())
            else:
                self._json({"error": "Process controller not initialized"})
        elif self.path == "/api/diagnostics":
            self._json(build_diagnostics())
        elif self.path == "/api/fee-curve":
            curve = []
            for p in range(1, 100):
                curve.append({
                    "price": p,
                    "taker_fee": kalshi_fee_per_contract(p),
                    "maker_fee": kalshi_fee_per_contract(p, maker=True),
                })
            self._json(curve)
        else:
            super().do_GET()

    def do_POST(self):
        # Risk management
        if self.path == "/api/risk/kill-switch/activate":
            if _risk_mgr:
                body = self._read_body()
                reason = body.get("reason", "Dashboard manual activation")
                _risk_mgr.activate_kill_switch(reason)
                self._json({"status": "activated", "reason": reason})
            else:
                self._json({"error": "not initialized"}, 400)

        elif self.path == "/api/risk/kill-switch/deactivate":
            if _risk_mgr:
                _risk_mgr.deactivate_kill_switch()
                self._json({"status": "deactivated"})
            else:
                self._json({"error": "not initialized"}, 400)

        elif self.path == "/api/risk/reset-daily":
            if _risk_mgr:
                _risk_mgr.reset_daily()
                self._json({"status": "daily counters reset"})
            else:
                self._json({"error": "not initialized"}, 400)

        # Process control - Scanner
        elif self.path == "/api/scanner/start":
            if not _process_ctrl:
                self._json({"error": "not initialized"}, 400)
                return
            body = self._read_body()
            interval = body.get("interval")
            success, msg = _process_ctrl.start_scanner(interval=interval)
            self._json({"success": success, "message": msg}, 200 if success else 400)

        elif self.path == "/api/scanner/stop":
            if not _process_ctrl:
                self._json({"error": "not initialized"}, 400)
                return
            success, msg = _process_ctrl.stop_scanner()
            self._json({"success": success, "message": msg}, 200 if success else 400)

        # Process control - Executor
        elif self.path == "/api/executor/start":
            if not _process_ctrl:
                self._json({"error": "not initialized"}, 400)
                return
            body = self._read_body()
            contracts = body.get("contracts")
            dry_run = body.get("dry_run")
            success, msg = _process_ctrl.start_executor(
                contracts=contracts, dry_run=dry_run
            )
            self._json({"success": success, "message": msg}, 200 if success else 400)

        elif self.path == "/api/executor/stop":
            if not _process_ctrl:
                self._json({"error": "not initialized"}, 400)
                return
            success, msg = _process_ctrl.stop_executor()
            self._json({"success": success, "message": msg}, 200 if success else 400)

        # Config updates
        elif self.path == "/api/config":
            if not _process_ctrl:
                self._json({"error": "not initialized"}, 400)
                return
            body = self._read_body()
            _process_ctrl.update_config(**body)
            self._json({"success": True, "config": _process_ctrl.get_status()["config"]})

        # Test fetch: synchronously tries one Kalshi API call and returns the result
        elif self.path == "/api/test-fetch":
            if not _trader:
                self._json({"success": False, "error": "Trader not initialized"}, 400)
                return
            result = {"authenticated": _trader.authenticated}
            try:
                import time as _t
                t0 = _t.time()
                data = _trader.get_markets(limit=5, status="open")
                elapsed = _t.time() - t0
                markets = data.get("markets", [])
                result.update({
                    "success": True,
                    "elapsed_seconds": round(elapsed, 2),
                    "markets_returned": len(markets),
                    "first_market": markets[0] if markets else None,
                    "cursor": data.get("cursor", ""),
                })
            except Exception as e:
                result.update({
                    "success": False,
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
            self._json(result)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logs


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def env_int(key, default):
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def env_float(key, default):
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def env_bool(key, default):
    val = os.environ.get(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def env_optional_float(key):
    """Return float if env var is set and non-empty, else None."""
    val = os.environ.get(key, "").strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def build_risk_config():
    """Build RiskConfig from environment variables with sane defaults."""
    return RiskConfig(
        max_drawdown_pct=env_float("CADENCE_MAX_DRAWDOWN_PCT", 10.0),
        daily_loss_limit_cents=env_int("CADENCE_DAILY_LOSS_LIMIT", 2000),
        daily_loss_limit_pct=env_optional_float("CADENCE_DAILY_LOSS_LIMIT_PCT"),
        max_per_trade_cents=env_int("CADENCE_MAX_PER_TRADE", 500),
        max_per_trade_pct=env_optional_float("CADENCE_MAX_PER_TRADE_PCT"),
        max_total_exposure_cents=env_int("CADENCE_MAX_EXPOSURE", 10000),
        max_consecutive_losses=env_int("CADENCE_MAX_CONSECUTIVE_LOSSES", 5),
        min_net_profit_cents=env_float("CADENCE_MIN_PROFIT", 2),
    )


def build_diagnostics():
    """Return a full snapshot of system state for the dashboard."""
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    email = os.environ.get("KALSHI_EMAIL", "")

    diag = {
        "dotenv_path": _DOTENV_PATH,
        "cwd": os.getcwd(),
        "script_dir": os.path.dirname(os.path.abspath(__file__)),
        "credentials": {
            "KALSHI_API_KEY_ID": (
                f"{api_key_id[:6]}..." if api_key_id else "(not set)"
            ),
            "KALSHI_PRIVATE_KEY_PATH": private_key_path or "(not set)",
            "KALSHI_PRIVATE_KEY_PATH_EXISTS": (
                os.path.exists(private_key_path) if private_key_path else False
            ),
            "KALSHI_EMAIL": email or "(not set)",
            "KALSHI_PASSWORD_SET": bool(os.environ.get("KALSHI_PASSWORD")),
        },
        "trader": {
            "initialized": _trader is not None,
            "authenticated": bool(_trader and _trader.authenticated),
            "api_key_id": getattr(_trader, "api_key_id", None),
            "signer_backend": (
                _trader.signer.backend
                if _trader and getattr(_trader, "signer", None) else None
            ),
            "base_url": getattr(_trader, "base_url", None),
        },
        "scanner": (
            _process_ctrl.scanner_status.to_dict() if _process_ctrl else None
        ),
        "executor": (
            _process_ctrl.executor_status.to_dict() if _process_ctrl else None
        ),
        "latest_scan_mode": _latest_scan.get("mode"),
        "latest_scan_markets": len(_latest_scan.get("markets", [])),
        "latest_scan_timestamp": _latest_scan.get("timestamp"),
    }
    return diag


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cadence - Kalshi arbitrage dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
All configuration via .env file or environment variables:
  KALSHI_API_KEY_ID        Your Kalshi API key ID
  KALSHI_PRIVATE_KEY_PATH  Path to your Kalshi RSA private key PEM file
  CADENCE_PORT             Dashboard port (default 8050)
  CADENCE_EQUITY           Starting equity in cents if balance sync disabled
  CADENCE_SYNC_BALANCE     Use live Kalshi balance (default true)
  CADENCE_MAX_DRAWDOWN_PCT  Kill switch drawdown % (default 10)
  CADENCE_MAX_PER_TRADE     Max cents per trade (default 500)
  CADENCE_MAX_PER_TRADE_PCT Max trade size as % of equity (dynamic, opt-in)
  CADENCE_DAILY_LOSS_LIMIT  Max daily loss in cents (default 2000)
  CADENCE_DAILY_LOSS_LIMIT_PCT  Max daily loss as % of daily start (dynamic, opt-in)
  CADENCE_INTERVAL         Seconds between scans (default 15)
  CADENCE_MAX_MARKETS      Cap markets per scan (default: all; Kalshi has ~50K+)
  CADENCE_MIN_PROFIT       Min net profit in cents (default 2)
  CADENCE_STATE_FILE       Risk state persistence file

Just run: python dashboard.py
Then open http://localhost:8050
        """,
    )
    parser.add_argument("--port", type=int,
                        default=env_int("CADENCE_PORT", 8050),
                        help="Dashboard port")
    args = parser.parse_args()

    global _risk_mgr, _trader, _process_ctrl

    print("=" * 60)
    print("  CADENCE - Kalshi Arbitrage Dashboard")
    print("=" * 60)

    # 0. Diagnostic: where did we load config from?
    if _DOTENV_PATH:
        print(f"  .env loaded from: {_DOTENV_PATH}")
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        print(f"  .env NOT FOUND. Checked:")
        print(f"     - {os.path.abspath('.env')} (current directory)")
        print(f"     - {os.path.join(script_dir, '.env')} (script directory)")
        print(f"  Create one from .env.example and add your Kalshi API key.")

    # 1. Load risk config from env
    risk_config = build_risk_config()
    starting_equity = env_int("CADENCE_EQUITY", 5000)
    state_file = os.environ.get("CADENCE_STATE_FILE") or None

    # 2. Initialize risk manager
    _risk_mgr = RiskManager(
        config=risk_config,
        starting_equity_cents=starting_equity,
        state_file=state_file,
    )

    # 3. Initialize Kalshi trader (authenticated if creds present)
    api_key_id = os.environ.get("KALSHI_API_KEY_ID")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    email = os.environ.get("KALSHI_EMAIL")
    password = os.environ.get("KALSHI_PASSWORD")

    # Print credential diagnostics (masked)
    print()
    print(f"  KALSHI_API_KEY_ID:       "
          f"{'set (' + api_key_id[:6] + '...)' if api_key_id else 'NOT SET'}")
    print(f"  KALSHI_PRIVATE_KEY_PATH: "
          f"{private_key_path if private_key_path else 'NOT SET'}")
    if private_key_path:
        exists = os.path.exists(private_key_path)
        print(f"     File exists:          {'yes' if exists else 'NO - check the path'}")
    if email:
        print(f"  KALSHI_EMAIL:            {email}")

    try:
        _trader = KalshiTrader(
            base_url=KALSHI_API_BASE,
            api_key_id=api_key_id, private_key_path=private_key_path,
            email=email, password=password,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"  WARNING: Auth setup failed: {e}")
        print(f"  Starting in demo-only mode.\n")
        _trader = KalshiTrader(base_url=KALSHI_API_BASE)

    authenticated = _trader.authenticated

    # 4. If authenticated and sync enabled, pull actual balance now
    if authenticated and env_bool("CADENCE_SYNC_BALANCE", True):
        try:
            balance_resp = _trader.get_balance()
            balance = balance_resp.get("balance", 0)
            if balance:
                _risk_mgr.sync_actual_balance(balance)
                print(f"  Synced Kalshi balance: {balance}c (${balance/100:.2f})")
        except Exception as e:
            print(f"  WARNING: Could not sync balance: {e}")

    # 5. Initialize process controller
    max_markets = os.environ.get("CADENCE_MAX_MARKETS", "").strip()
    max_markets_int = int(max_markets) if max_markets.isdigit() else None
    _process_ctrl = ProcessController(
        trader=_trader,
        risk_mgr=_risk_mgr,
        scan_callback=scan_callback,
        execute_callback=execute_callback,
        min_profit=env_float("CADENCE_MIN_PROFIT", 2),
        interval=env_int("CADENCE_INTERVAL", 15),
        dry_run=True,  # default to dry run; user enables live via dashboard
        max_markets=max_markets_int,
    )
    _process_ctrl.contracts_per_leg = env_int("CADENCE_CONTRACTS", 1)

    # 6. Load demo data so the dashboard has something to show immediately
    run_scan(DEMO_MARKETS, _process_ctrl.min_profit, mode="demo")

    # 7. Print status
    print()
    print(f"  Authentication: {'OK (live trading enabled)' if authenticated else 'none (demo only)'}")
    if authenticated:
        status = _risk_mgr.get_status()
        print(f"  Equity:         ${status['equity_cents']/100:.2f}")
        print(f"  Max drawdown:   {risk_config.max_drawdown_pct}% from peak (trailing)")
        pt_limit = status.get("effective_per_trade_limit_cents")
        pt_basis = status.get("per_trade_limit_basis", "none")
        if pt_limit is not None:
            print(f"  Max per trade:  ${pt_limit/100:.2f} [{pt_basis}]")
        daily_limit = status.get("effective_daily_loss_limit_cents")
        daily_basis = status.get("daily_loss_limit_basis", "none")
        if daily_limit is not None:
            print(f"  Daily limit:    ${daily_limit/100:.2f} [{daily_basis}]")
    else:
        print(f"  Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env for live mode")
    print()
    print(f"  Dashboard:  http://localhost:{args.port}")
    print(f"  Press Ctrl+C to stop")
    print()

    # 8. Auto-start scanner if authenticated (user wants live data)
    # Executor still requires explicit start via the dashboard.
    if authenticated and env_bool("CADENCE_AUTOSTART_SCANNER", True):
        success, msg = _process_ctrl.start_scanner()
        if success:
            print(f"  Scanner: auto-started ({msg})")
        else:
            print(f"  Scanner: auto-start failed - {msg}")
        print()

    # 9. Start web server
    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        if _process_ctrl:
            _process_ctrl.stop_scanner()
            _process_ctrl.stop_executor()
        server.server_close()


if __name__ == "__main__":
    main()
