#!/usr/bin/env python3
"""
Automated Arbitrage Executor

Connects the scanner, risk manager, and Kalshi order API to automatically
execute arbitrage trades when opportunities are found.

IMPORTANT: This places real trades with real money. Always start with small
limits via RiskConfig and monitor the dashboard.

Usage:
    # Dry run (scan + risk check, no orders)
    python executor.py --api-key-id X --api-key K --dry-run

    # Live auto-trading with $50 equity, $5 max per trade
    python executor.py --api-key-id X --api-key K \\
        --equity 5000 --max-per-trade 500

    # With state persistence (resumes on restart)
    python executor.py --api-key-id X --api-key K \\
        --equity 5000 --state-file risk_state.json
"""

import argparse
import json
import os
import sys
import time
import uuid

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)


def _load_dotenv(path=".env"):
    """Load .env into os.environ (env vars take priority)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val


_load_dotenv()


from kalshi_arbitrage import (
    KalshiClient,
    find_binary_arbitrage,
    find_multi_outcome_arbitrage,
    KALSHI_API_BASE,
)
from risk_manager import RiskManager, RiskConfig, RiskAction


class KalshiTrader(KalshiClient):
    """
    Extends KalshiClient with order placement capabilities.

    Order API: POST /portfolio/orders
    Batch API: POST /portfolio/orders/batched (up to 20 orders)
    """

    def create_order(self, ticker, side, action="buy", count=1,
                     yes_price=None, no_price=None, time_in_force="ioc"):
        """
        Place a single order.

        Args:
            ticker: Market ticker (e.g. "KXBTC-100K-26JUN30")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            yes_price: Limit price in cents for yes side (1-99)
            no_price: Limit price in cents for no side (1-99)
            time_in_force: "ioc" (immediate or cancel) or "gtc" (good til cancelled)

        Returns:
            Order response dict with order_id, status, etc.
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "time_in_force": time_in_force,
            "client_order_id": str(uuid.uuid4()),
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price

        return self._request("POST", f"{self.base_url}/portfolio/orders", json=body)

    def batch_create_orders(self, orders):
        """
        Place up to 20 orders atomically.

        Args:
            orders: List of order dicts (same format as create_order body)

        Returns:
            Batch response with per-order results
        """
        for order in orders:
            if "client_order_id" not in order:
                order["client_order_id"] = str(uuid.uuid4())

        return self._request(
            "POST",
            f"{self.base_url}/portfolio/orders/batched",
            json={"orders": orders},
        )

    def get_positions(self):
        """Get current portfolio positions."""
        return self._request("GET", f"{self.base_url}/portfolio/positions")

    def get_balance(self):
        """Get account balance."""
        return self._request("GET", f"{self.base_url}/portfolio/balance")


def build_orders_for_opportunity(opp, contracts=1):
    """
    Convert an ArbitrageOpportunity into a list of Kalshi order dicts.

    For binary arb: buy YES + buy NO on the same market.
    For multi-outcome YES: buy YES on every market in the event.
    For multi-outcome NO: buy NO on every market in the event.
    """
    orders = []

    if opp.type == "binary":
        m = opp.markets[0]
        # Buy YES at the ask price
        orders.append({
            "ticker": m["ticker"],
            "side": "yes",
            "action": "buy",
            "count": contracts,
            "type": "limit",
            "yes_price": m["yes_ask"],
            "time_in_force": "ioc",  # immediate-or-cancel: don't leave resting orders
        })
        # Buy NO at the ask price
        orders.append({
            "ticker": m["ticker"],
            "side": "no",
            "action": "buy",
            "count": contracts,
            "type": "limit",
            "no_price": m["no_ask"],
            "time_in_force": "ioc",
        })

    elif "YES" in opp.type:
        for m in opp.markets:
            orders.append({
                "ticker": m["ticker"],
                "side": "yes",
                "action": "buy",
                "count": contracts,
                "type": "limit",
                "yes_price": m["yes_ask"],
                "time_in_force": "ioc",
            })

    elif "NO" in opp.type:
        for m in opp.markets:
            orders.append({
                "ticker": m["ticker"],
                "side": "no",
                "action": "buy",
                "count": contracts,
                "type": "limit",
                "no_price": m["no_ask"],
                "time_in_force": "ioc",
            })

    return orders


def execute_opportunity(trader, risk_mgr, opp, contracts=1, dry_run=False):
    """
    Full execution pipeline for one opportunity:
    1. Risk check
    2. Build orders
    3. Submit via batch API (or log in dry-run mode)
    4. Record trade in risk manager

    Returns (success: bool, detail: str)
    """
    # 1. Risk check
    decision = risk_mgr.check_trade(opp, contracts)
    if not decision.allowed:
        return False, f"RISK BLOCKED: {decision}"

    # 2. Build orders
    orders = build_orders_for_opportunity(opp, contracts)
    if not orders:
        return False, "No orders generated"

    # 3. Execute
    if dry_run:
        print(f"    [DRY RUN] Would place {len(orders)} orders:")
        for o in orders:
            price = o.get("yes_price") or o.get("no_price")
            print(f"      {o['side'].upper():3s} {o['ticker']} "
                  f"x{o['count']} @ {price}¢")
        risk_mgr.record_trade_opened(opp, contracts)
        return True, f"DRY RUN: {len(orders)} orders logged"

    try:
        if len(orders) <= 20:
            result = trader.batch_create_orders(orders)
        else:
            # Shouldn't happen for arb trades, but handle gracefully
            result = {"orders": []}
            for order in orders:
                r = trader.create_order(**{k: v for k, v in order.items()
                                           if k != "type"})
                result["orders"].append(r)

        # Check for partial fills — critical for arb safety
        filled_count = 0
        total_count = len(orders)
        for order_result in result.get("orders", []):
            order_data = order_result.get("order", order_result)
            status = order_data.get("status", "unknown")
            if status in ("resting", "executed"):
                filled_count += 1

        if filled_count < total_count:
            # PARTIAL FILL WARNING: Arb is only risk-free if ALL legs fill
            print(f"    WARNING: Only {filled_count}/{total_count} legs filled!")
            print(f"    This arb may not be fully hedged.")

        risk_mgr.record_trade_opened(opp, contracts)
        return True, f"Placed {filled_count}/{total_count} orders"

    except requests.RequestException as e:
        return False, f"ORDER ERROR: {e}"


def run_executor(trader, risk_mgr, min_profit=1, contracts=1,
                 interval=15, dry_run=False):
    """
    Main execution loop:
    1. Fetch markets
    2. Scan for arbs
    3. Risk-check and execute each
    4. Sleep and repeat
    """
    print(f"\n{'='*60}")
    print(f"  CADENCE AUTO-EXECUTOR")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE TRADING'}")
    print(f"  Contracts per leg: {contracts}")
    print(f"  Min net profit: {min_profit}¢")
    print(f"  Scan interval: {interval}s")
    print(f"{'='*60}\n")

    status = risk_mgr.get_status()
    print(f"  Equity: {status['equity_cents']}¢ (${status['equity_cents']/100:.2f})")
    print(f"  Max drawdown: {status['config']['max_drawdown_pct']}%")
    print(f"  Max per trade: {status['config']['max_per_trade_cents']}¢")
    print(f"  Daily loss limit: {status['config']['daily_loss_limit_cents']}¢")
    print(f"  Kill switch: {'ACTIVE' if status['kill_switch_active'] else 'off'}")
    print()

    trades_executed = 0
    scan_count = 0

    try:
        while True:
            scan_count += 1
            ts = time.strftime("%H:%M:%S")

            # Check kill switch before even fetching
            if risk_mgr.state.kill_switch_active:
                print(f"  [{ts}] Kill switch active: {risk_mgr.state.kill_switch_reason}")
                print(f"         Waiting... (deactivate via dashboard or API)")
                time.sleep(interval)
                continue

            # Fetch markets
            try:
                markets = trader.get_all_markets()
            except requests.RequestException as e:
                print(f"  [{ts}] Fetch error: {e}")
                time.sleep(interval)
                continue

            # Scan
            binary_opps = find_binary_arbitrage(markets, min_profit)
            multi_opps = find_multi_outcome_arbitrage(markets, min_profit)
            all_opps = binary_opps + multi_opps
            all_opps.sort(key=lambda o: o.net_profit_cents, reverse=True)

            if not all_opps:
                print(f"  [{ts}] Scan #{scan_count}: {len(markets)} markets, "
                      f"0 opportunities")
                time.sleep(interval)
                continue

            print(f"  [{ts}] Scan #{scan_count}: {len(markets)} markets, "
                  f"{len(all_opps)} opportunities found!")

            # Execute each opportunity (best first)
            for opp in all_opps:
                success, detail = execute_opportunity(
                    trader, risk_mgr, opp, contracts, dry_run
                )
                symbol = "+" if success else "x"
                print(f"    [{symbol}] {opp.event_ticker}: "
                      f"net {opp.net_profit_cents}¢ — {detail}")
                if success:
                    trades_executed += 1

            # Status update every 10 scans
            if scan_count % 10 == 0:
                s = risk_mgr.get_status()
                print(f"\n  --- Status (scan #{scan_count}) ---")
                print(f"  Equity: {s['equity_cents']}¢  |  "
                      f"Drawdown: {s['drawdown_pct']}%  |  "
                      f"Daily P&L: {s['daily_pnl_cents']}¢  |  "
                      f"Trades today: {s['daily_trade_count']}  |  "
                      f"Open: {s['open_position_count']}\n")

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped. Executed {trades_executed} trades over {scan_count} scans.")
        s = risk_mgr.get_status()
        print(f"Final equity: {s['equity_cents']}¢  |  "
              f"Daily P&L: {s['daily_pnl_cents']}¢  |  "
              f"Drawdown: {s['drawdown_pct']}%")


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Auto-Executor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and risk-check but don't place orders")

    # Trading params
    parser.add_argument("--contracts", type=int, default=1,
                        help="Contracts per leg (default: 1)")
    parser.add_argument("--min-profit", type=float, default=2,
                        help="Min net profit in cents to trade (default: 2)")
    parser.add_argument("--interval", type=int, default=15,
                        help="Seconds between scans (default: 15)")

    # Risk limits
    risk = parser.add_argument_group("risk management")
    risk.add_argument("--equity", type=int, default=5000,
                      help="Starting equity in cents (default: 5000 = $50)")
    risk.add_argument("--max-drawdown-pct", type=float, default=10.0,
                      help="Kill switch at this %% drawdown (default: 10)")
    risk.add_argument("--max-per-trade", type=int, default=500,
                      help="Max cents per trade (default: 500 = $5)")
    risk.add_argument("--daily-loss-limit", type=int, default=2000,
                      help="Max daily loss in cents (default: 2000 = $20)")
    risk.add_argument("--max-exposure", type=int, default=10000,
                      help="Max total exposure in cents (default: 10000 = $100)")
    risk.add_argument("--max-consecutive-losses", type=int, default=5,
                      help="Pause after N consecutive losses (default: 5)")
    risk.add_argument("--state-file", default=None,
                      help="File to persist risk state across restarts")

    # Auth
    auth = parser.add_argument_group("authentication")
    auth.add_argument("--api-key-id",
                      help="Kalshi API key ID (or KALSHI_API_KEY_ID env var)")
    auth.add_argument("--api-key",
                      help="Kalshi API key (or KALSHI_API_KEY env var)")
    auth.add_argument("--email", help="Kalshi email (or KALSHI_EMAIL env var)")
    auth.add_argument("--password",
                      help="Kalshi password (or KALSHI_PASSWORD env var)")

    args = parser.parse_args()

    # Build trader client
    api_key_id = args.api_key_id or os.environ.get("KALSHI_API_KEY_ID")
    api_key = args.api_key or os.environ.get("KALSHI_API_KEY")
    email = args.email or os.environ.get("KALSHI_EMAIL")
    password = args.password or os.environ.get("KALSHI_PASSWORD")

    if not args.dry_run and not (api_key_id or email):
        print("ERROR: Live trading requires authentication.")
        print("  Use --api-key-id + --api-key, or --email + --password")
        print("  Or use --dry-run to test without trading.")
        sys.exit(1)

    trader = KalshiTrader(
        base_url=KALSHI_API_BASE,
        api_key_id=api_key_id, api_key=api_key,
        email=email, password=password,
    )

    # Build risk manager
    risk_config = RiskConfig(
        max_drawdown_pct=args.max_drawdown_pct,
        daily_loss_limit_cents=args.daily_loss_limit,
        max_per_trade_cents=args.max_per_trade,
        max_total_exposure_cents=args.max_exposure,
        max_consecutive_losses=args.max_consecutive_losses,
        min_net_profit_cents=args.min_profit,
    )

    risk_mgr = RiskManager(
        config=risk_config,
        starting_equity_cents=args.equity,
        state_file=args.state_file,
    )

    # Run
    run_executor(
        trader=trader,
        risk_mgr=risk_mgr,
        min_profit=args.min_profit,
        contracts=args.contracts,
        interval=args.interval,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
