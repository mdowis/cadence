#!/usr/bin/env python3
"""
Automated Arbitrage Executor

Connects the scanner, risk manager, and Kalshi order API to automatically
execute arbitrage trades when opportunities are found.

IMPORTANT: This places real trades with real money. Always start with small
limits via RiskConfig and monitor the dashboard.

Usage:
    # Dry run (scan + risk check, no orders)
    python executor.py --api-key-id X --private-key-path key.pem --dry-run

    # Live auto-trading with $50 equity, $5 max per trade
    python executor.py --api-key-id X --private-key-path key.pem \\
        --equity 5000 --max-per-trade 500

    # With state persistence (resumes on restart)
    python executor.py --api-key-id X --private-key-path key.pem \\
        --equity 5000 --state-file risk_state.json

Or put KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env and omit the flags.
"""

import argparse
import json
import os
import sys
import time
import uuid


def _load_dotenv(path=".env"):
    """Load .env into os.environ (env vars take priority). Checks CWD then script dir."""
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
        return
    return


_load_dotenv()


from kalshi_arbitrage import (
    KalshiClient,
    HTTPError,
    find_binary_arbitrage,
    find_multi_outcome_arbitrage,
    KALSHI_API_BASE,
)
from risk_manager import RiskManager, RiskConfig, RiskAction


# Kalshi order constants
TIF_IOC = "immediate_or_cancel"  # fill as much as possible now, cancel rest
TIF_GTC = "good_till_cancelled"  # stay on the book until explicitly cancelled


def _cents_to_dollars_str(cents):
    """
    Convert integer cents to Kalshi's fixed-point dollar string format.

    Kalshi accepts both legacy cents (yes_price=47) and new dollar strings
    (yes_price_dollars="0.47"). The new format is required for fractional
    prices and is the migration target, so we use it everywhere.
    """
    if cents is None:
        return None
    return f"{cents / 100:.4f}"


class KalshiTrader(KalshiClient):
    """
    Extends KalshiClient with order placement capabilities.

    Order API: POST /portfolio/orders
    Batch API: POST /portfolio/orders/batched (up to 20 orders)
    """

    def create_order(self, ticker, side, action="buy", count=1,
                     yes_price=None, no_price=None, time_in_force=TIF_IOC):
        """
        Place a single limit order on Kalshi.

        Args:
            ticker: Market ticker (e.g. "KXBTC-100K-26JUN30")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            yes_price: Limit price in cents for yes side (1-99)
            no_price: Limit price in cents for no side (1-99)
            time_in_force: "immediate_or_cancel" or "good_till_cancelled"

        Returns:
            Order response dict with order_id, status, etc.
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": int(count),
            "time_in_force": time_in_force,
            "client_order_id": str(uuid.uuid4()),
        }
        # Use the new fixed-point dollar fields (Kalshi's recommended format
        # post-March 2026 migration). Presence of a price field makes it a
        # limit order; there's no explicit "type" field.
        if yes_price is not None:
            body["yes_price_dollars"] = _cents_to_dollars_str(yes_price)
        if no_price is not None:
            body["no_price_dollars"] = _cents_to_dollars_str(no_price)

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


def _yes_order(ticker, price_cents, count, client_order_id=None):
    """Build a Kalshi limit order to buy YES at the given price."""
    return {
        "ticker": ticker,
        "side": "yes",
        "action": "buy",
        "count": int(count),
        "yes_price_dollars": _cents_to_dollars_str(price_cents),
        "time_in_force": TIF_IOC,
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }


def _no_order(ticker, price_cents, count, client_order_id=None):
    """Build a Kalshi limit order to buy NO at the given price."""
    return {
        "ticker": ticker,
        "side": "no",
        "action": "buy",
        "count": int(count),
        "no_price_dollars": _cents_to_dollars_str(price_cents),
        "time_in_force": TIF_IOC,
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }


def build_orders_for_opportunity(opp, contracts=1):
    """
    Convert an ArbitrageOpportunity into a list of Kalshi order dicts.

    For binary arb: buy YES + buy NO on the same market.
    For multi-outcome YES: buy YES on every market in the event.
    For multi-outcome NO: buy NO on every market in the event.

    Orders use Kalshi's post-migration format:
      - yes_price_dollars / no_price_dollars as "0.4700" strings
      - time_in_force: "immediate_or_cancel" (full name, not "ioc")
      - No "type" field (Kalshi infers limit from price presence)
    """
    orders = []

    if opp.type == "binary":
        m = opp.markets[0]
        orders.append(_yes_order(m["ticker"], m["yes_ask"], contracts))
        orders.append(_no_order(m["ticker"], m["no_ask"], contracts))

    elif "YES" in opp.type:
        for m in opp.markets:
            orders.append(_yes_order(m["ticker"], m["yes_ask"], contracts))

    elif "NO" in opp.type:
        for m in opp.markets:
            orders.append(_no_order(m["ticker"], m["no_ask"], contracts))

    return orders


# Fill statuses that mean "order actually traded contracts"
# "resting" means the order is sitting on the book - NOT filled. For IOC
# orders resting shouldn't happen; only executed counts.
FILLED_STATUSES = {"executed"}


def _parse_order_status(order_result):
    """
    Extract the status string from a Kalshi order response.

    Kalshi wraps the order under an 'order' key in some responses,
    returns it flat in others.
    """
    order = order_result.get("order", order_result)
    return order.get("status", "unknown"), order


def _best_bid_cents(trader, ticker, side):
    """
    Fetch the best bid for selling `side` on this ticker.

    Kalshi's orderbook only lists BIDS for both yes and no sides. To sell
    YES we need the highest YES bid. To sell NO we need the highest NO bid.

    Returns (price_cents, source) where source is a string for logging,
    or (None, reason) if no bid is available.
    """
    try:
        resp = trader.get_orderbook(ticker)
    except Exception as e:
        return None, f"orderbook fetch failed: {e}"

    # Post-migration: orderbook_fp with yes_dollars/no_dollars
    # Pre-migration: orderbook with yes/no as integer cents
    book = resp.get("orderbook_fp") or resp.get("orderbook") or {}

    if "orderbook_fp" in resp or f"{side}_dollars" in book:
        levels = book.get(f"{side}_dollars") or book.get(side) or []
    else:
        levels = book.get(side) or []

    if not levels:
        return None, f"no {side} bids in orderbook"

    # Each level is [price, quantity]. Price may be string dollars or int cents.
    # Take the max across all levels regardless of sort order.
    best = None
    for lvl in levels:
        if not isinstance(lvl, (list, tuple)) or not lvl:
            continue
        price = lvl[0]
        if isinstance(price, str):
            try:
                cents = round(float(price) * 100)
            except (ValueError, TypeError):
                continue
        elif isinstance(price, (int, float)):
            # Legacy format: integer cents
            cents = int(price)
        else:
            continue
        if best is None or cents > best:
            best = cents

    if best is None:
        return None, f"no parseable {side} bid prices"
    return best, f"best {side} bid"


def _unwind_filled_legs(trader, orders, result, risk_mgr):
    """
    Partial fill safety: if some legs filled but not all, immediately
    place OPPOSING orders to close the filled legs. This prevents
    naked directional exposure after a failed multi-leg arb.

    Unwind strategy:
      1. Fetch the live orderbook for the market
      2. Sell at the best bid price (guaranteed fill at that price)
      3. If no bid exists, fail loudly — the kill switch will trip

    Returns (unwound_count, errors).
    """
    unwound = 0
    errors = []
    order_results = result.get("orders", [])

    for i, order_result in enumerate(order_results):
        if i >= len(orders):
            continue
        original = orders[i]
        status, order_data = _parse_order_status(order_result)

        # Only unwind legs that actually filled
        if status not in FILLED_STATUSES:
            continue

        filled_qty = int(order_data.get("filled_quantity") or
                         order_data.get("taker_fill_count") or
                         original.get("count", 0))
        if filled_qty <= 0:
            continue

        ticker = original["ticker"]
        side = original["side"]

        # Find the real price we can sell at right now
        bid_cents, source = _best_bid_cents(trader, ticker, side)
        if bid_cents is None:
            err = f"Cannot unwind {ticker}: {source}"
            print(f"    [UNWIND] ERROR: {err}", flush=True)
            errors.append(err)
            continue

        # Compare to what we paid on the filling leg so we can log the loss
        paid_dollars = original.get(f"{side}_price_dollars", "?")
        try:
            paid_cents = round(float(paid_dollars) * 100)
            loss_per_contract = paid_cents - bid_cents
        except (ValueError, TypeError):
            paid_cents = None
            loss_per_contract = None

        unwind = {
            "ticker": ticker,
            "side": side,
            "action": "sell",  # close the long position
            "count": filled_qty,
            f"{side}_price_dollars": _cents_to_dollars_str(bid_cents),
            "time_in_force": TIF_IOC,
            "client_order_id": f"unwind-{uuid.uuid4()}",
        }

        try:
            loss_str = f", est loss {loss_per_contract}c/contract" \
                if loss_per_contract is not None else ""
            print(f"    [UNWIND] Selling {filled_qty} {side} on {ticker} "
                  f"at {bid_cents}c (paid {paid_cents}c{loss_str})", flush=True)
            trader._request("POST", f"{trader.base_url}/portfolio/orders",
                            json=unwind)
            unwound += 1
            if risk_mgr:
                total_loss = (loss_per_contract * filled_qty
                              if loss_per_contract is not None else None)
                risk_mgr._log_risk_event(
                    "partial_fill_unwind",
                    f"Unwound {filled_qty} {side} on {ticker} at {bid_cents}c "
                    f"(loss: {total_loss}c)" if total_loss is not None
                    else f"Unwound {filled_qty} {side} on {ticker} at {bid_cents}c"
                )
        except Exception as e:
            err = f"Failed to unwind {ticker}: {e}"
            print(f"    [UNWIND] ERROR: {err}", flush=True)
            errors.append(err)

    return unwound, errors


def _sync_balance_before_trade(trader, risk_mgr):
    """
    Pull fresh balance from Kalshi and sync it into the risk manager so
    the risk check uses the real-time total equity, not a stale snapshot.
    Silent on failure — the periodic scanner sync is the safety net.
    """
    try:
        resp = trader.get_balance()
        balance = resp.get("balance") or 0
        portfolio_value = resp.get("portfolio_value") or 0
        if balance or portfolio_value:
            risk_mgr.sync_actual_balance(
                balance_cents=balance,
                portfolio_value_cents=portfolio_value,
            )
    except Exception as e:
        print(f"    [pre-trade balance sync] failed: {e}", flush=True)


def execute_opportunity(trader, risk_mgr, opp, contracts=1, dry_run=False,
                        allow_multi_leg=False):
    """
    Full execution pipeline for one opportunity, with safety rails:

    1. Safety filter: reject multi-leg arbs unless explicitly allowed
    2. Real exposure sync from Kalshi (source of truth)
    3. Risk check (limits, kill switch, drawdown)
    4. Build orders (correct fixed-point format)
    5. Submit via batch API (or dry-run log)
    6. Verify fills; unwind any partial fills immediately
    7. Only record exposure for legs that actually filled

    Returns (success: bool, detail: str)
    """
    # 1. Multi-leg arbs are opt-in — more legs = more partial fill risk
    is_multi_leg = opp.type != "binary"
    if is_multi_leg and not allow_multi_leg:
        return False, "multi-leg arbs disabled (set CADENCE_ALLOW_MULTI_LEG=true to enable)"

    # 2. Sync real equity and exposure from Kalshi before any risk checks.
    # sync_actual_balance uses portfolio_value (Kalshi's authoritative
    # total equity) as the source of truth and derives exposure as
    # portfolio_value - balance.
    if not dry_run:
        _sync_balance_before_trade(trader, risk_mgr)

    # 3. Risk check
    decision = risk_mgr.check_trade(opp, contracts)
    if not decision.allowed:
        return False, f"RISK BLOCKED: {decision}"

    # 4. Build orders
    orders = build_orders_for_opportunity(opp, contracts)
    if not orders:
        return False, "No orders generated"

    # 5. Dry run path
    if dry_run:
        print(f"    [DRY RUN] Would place {len(orders)} orders:")
        for o in orders:
            price = o.get("yes_price_dollars") or o.get("no_price_dollars")
            print(f"      {o['side'].upper():3s} {o['ticker']} "
                  f"x{o['count']} @ ${price}")
        risk_mgr.record_trade_opened(opp, contracts)
        return True, f"DRY RUN: {len(orders)} orders logged"

    # 6. Live execution
    try:
        if len(orders) <= 20:
            result = trader.batch_create_orders(orders)
        else:
            result = {"orders": []}
            for order in orders:
                r = trader.create_order(
                    **{k: v for k, v in order.items()
                       if k not in ("type",)}
                )
                result["orders"].append(r)
    except HTTPError as e:
        return False, f"ORDER ERROR: {e}"

    # 7. Verify fills — only 'executed' status counts as filled
    filled_count = 0
    total_count = len(orders)
    for order_result in result.get("orders", []):
        status, _ = _parse_order_status(order_result)
        if status in FILLED_STATUSES:
            filled_count += 1

    # 8. Partial fill: immediately unwind any filled legs
    if filled_count > 0 and filled_count < total_count:
        print(f"    ⚠ PARTIAL FILL: {filled_count}/{total_count} legs "
              f"filled, unwinding to avoid naked exposure", flush=True)
        unwound, errs = _unwind_filled_legs(trader, orders, result, risk_mgr)
        if errs:
            # Unwind failed — trip kill switch, this is a dangerous state
            risk_mgr.activate_kill_switch(
                f"Failed to unwind partial fill on {opp.event_ticker}: {errs}"
            )
            return False, (
                f"PARTIAL FILL + UNWIND FAILED: {filled_count}/{total_count} "
                f"filled, {unwound} unwound, {len(errs)} errors. "
                f"KILL SWITCH ACTIVATED."
            )
        return False, (
            f"partial fill ({filled_count}/{total_count}), unwound {unwound} legs"
        )

    # 9. Zero fills — nothing to track, nothing to unwind
    if filled_count == 0:
        return False, f"no legs filled (all orders rejected or cancelled)"

    # 10. All legs filled — track exposure and return success
    risk_mgr.record_trade_opened(opp, contracts)
    return True, f"Placed {filled_count}/{total_count} orders"


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
            except HTTPError as e:
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
    auth.add_argument("--private-key-path",
                      help="Path to RSA private key PEM file "
                           "(or KALSHI_PRIVATE_KEY_PATH env var)")
    auth.add_argument("--email", help="Kalshi email (or KALSHI_EMAIL env var)")
    auth.add_argument("--password",
                      help="Kalshi password (or KALSHI_PASSWORD env var)")

    args = parser.parse_args()

    # Build trader client
    api_key_id = args.api_key_id or os.environ.get("KALSHI_API_KEY_ID")
    private_key_path = args.private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    email = args.email or os.environ.get("KALSHI_EMAIL")
    password = args.password or os.environ.get("KALSHI_PASSWORD")

    if not args.dry_run and not ((api_key_id and private_key_path) or email):
        print("ERROR: Live trading requires authentication.")
        print("  Use --api-key-id + --private-key-path, or --email + --password")
        print("  Or use --dry-run to test without trading.")
        sys.exit(1)

    trader = KalshiTrader(
        base_url=KALSHI_API_BASE,
        api_key_id=api_key_id, private_key_path=private_key_path,
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
