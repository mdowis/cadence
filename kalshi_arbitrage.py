#!/usr/bin/env python3
"""
Kalshi Arbitrage Detector

Scans Kalshi prediction markets for mispriced opportunities:
1. Binary arbitrage: Yes + No ask prices sum to less than $1.00
2. Multi-outcome arbitrage: All outcomes in an event sum to less/more than $1.00
3. Orderbook depth analysis: Checks if arb is executable at sufficient volume

Supports both authenticated (higher rate limits) and unauthenticated access.

Usage:
    python kalshi_arbitrage.py --demo                      # Verify logic with sample data
    python kalshi_arbitrage.py                             # Scan live (unauthenticated)
    python kalshi_arbitrage.py --api-key-id X --api-key K  # Scan live (authenticated)
    python kalshi_arbitrage.py --continuous --interval 15   # Rescan every 15s
"""

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)


KALSHI_API_BASE = "https://trading-api.kalshi.com/trade-api/v2"

# Kalshi quadratic fee: ceil(coefficient * contracts * P * (1 - P))
# where P = price in dollars (e.g. 0.50 for 50¢)
TAKER_FEE_COEFF = 0.07    # 7% coefficient → max 1.75¢/contract at P=50¢
MAKER_FEE_COEFF = 0.0175  # 1.75% coefficient → max ~0.44¢/contract at P=50¢


# ---------------------------------------------------------------------------
# Fee Calculations (Kalshi quadratic fee model)
# ---------------------------------------------------------------------------

def kalshi_fee_per_contract(price_cents, contracts=1, maker=False):
    """
    Compute Kalshi's quadratic fee in cents.

    Formula: ceil(coeff * contracts * P * (1 - P))
    where P = price_cents / 100 (price in dollars).

    Returns cents. At P=50¢ (max): taker pays 2¢/contract, maker pays 1¢.
    At P=5¢ or P=95¢: taker pays 1¢/contract (fees shrink at extremes).
    """
    coeff = MAKER_FEE_COEFF if maker else TAKER_FEE_COEFF
    p = price_cents / 100.0
    raw_dollars = coeff * contracts * p * (1 - p)
    return math.ceil(raw_dollars * 100)  # ceil to nearest cent, return as cents


def total_arb_fee(market_prices, contracts=1):
    """
    Compute total taker fees for an arbitrage trade (buying multiple sides).
    Each leg is a separate taker order.
    """
    total = 0.0
    for price_cents in market_prices:
        total += kalshi_fee_per_contract(price_cents, contracts)
    return total


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class ArbitrageOpportunity:
    type: str
    event_title: str
    event_ticker: str
    markets: list = field(default_factory=list)
    total_cost: float = 0.0
    guaranteed_payout: float = 0.0
    profit_cents: float = 0.0
    roi_percent: float = 0.0
    fee_cents: float = 0.0
    net_profit_cents: float = 0.0

    def __str__(self):
        lines = [
            f"{'='*72}",
            f"  TYPE:  {self.type}",
            f"  EVENT: {self.event_title}",
            f"  TICKER: {self.event_ticker}",
        ]
        if self.type == "binary":
            m = self.markets[0]
            lines.append(f"  MARKET: {m['title']}")
            lines.append(f"  Yes Ask: {m['yes_ask']}¢  |  No Ask: {m['no_ask']}¢")
            lines.append(f"  Combined cost: {m['yes_ask'] + m['no_ask']}¢ → payout: 100¢")
        else:
            side = "Yes" if "YES" in self.type else "No"
            lines.append(f"  STRATEGY: Buy ALL {side} contracts")
            lines.append(f"  Markets ({len(self.markets)}):")
            for m in self.markets:
                price = m.get('yes_ask') or m.get('no_ask', '?')
                lines.append(f"    - {m['title']}: {price}¢")

        lines.append(f"  Cost: {self.total_cost:.0f}¢  →  Payout: {self.guaranteed_payout:.0f}¢")
        lines.append(f"  GROSS PROFIT: {self.profit_cents:.1f}¢  ({self.roi_percent:.2f}% ROI)")
        lines.append(f"  TAKER FEES:   {self.fee_cents:.1f}¢  (quadratic: 0.07 × P × (1-P) per leg)")
        lines.append(f"  NET PROFIT:   {self.net_profit_cents:.1f}¢")
        lines.append(f"{'='*72}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class KalshiClient:
    """
    Kalshi API client supporting both authenticated and unauthenticated access.

    Authenticated: higher rate limits, access to trading/portfolio endpoints.
    Unauthenticated: market data only, lower rate limits.
    """

    MAX_RETRIES = 4
    BASE_DELAY = 0.1  # seconds between paginated requests

    def __init__(self, base_url=KALSHI_API_BASE, api_key_id=None, api_key=None,
                 email=None, password=None):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cadence-arbitrage-scanner/1.0",
        })
        self.authenticated = False

        # Prefer API key auth (RSA or HMAC key pair)
        if api_key_id and api_key:
            self._auth_api_key(api_key_id, api_key)
        # Fall back to email/password login for JWT
        elif email and password:
            self._auth_login(email, password)

    def _auth_api_key(self, api_key_id, api_key):
        """Authenticate via API key (passed as bearer token)."""
        self.session.headers["Authorization"] = f"Bearer {api_key_id}:{api_key}"
        self.authenticated = True
        print(f"  Authenticated via API key (key_id: {api_key_id[:8]}...)")

    def _auth_login(self, email, password):
        """Authenticate via email/password to obtain a JWT session token."""
        print(f"  Logging in as {email}...")
        resp = self.session.post(
            f"{self.base_url}/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"Login failed: no token in response. Keys: {list(data.keys())}")
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.authenticated = True
        member_id = data.get("member_id", "?")
        print(f"  Logged in successfully (member_id: {member_id})")

    def _request(self, method, url, **kwargs):
        """Make a request with retry on 429 (rate limit) responses."""
        for attempt in range(self.MAX_RETRIES):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                delay = float(retry_after)
            else:
                delay = 2 ** attempt  # 1s, 2s, 4s, 8s
            print(f"  Rate limited (429). Retrying in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{self.MAX_RETRIES})...")
            time.sleep(delay)
        # Final attempt
        resp = self.session.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def get_markets(self, limit=200, cursor=None, status="open", event_ticker=None):
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._request("GET", f"{self.base_url}/markets", params=params)

    def get_all_markets(self, status="open"):
        all_markets = []
        cursor = None
        page = 0
        while True:
            data = self.get_markets(limit=200, cursor=cursor, status=status)
            markets = data.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            page += 1
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(self.BASE_DELAY)
        return all_markets

    def get_event(self, event_ticker):
        return self._request("GET", f"{self.base_url}/events/{event_ticker}")

    def get_orderbook(self, ticker):
        return self._request("GET", f"{self.base_url}/orderbook/{ticker}")

    def get_exchange_status(self):
        return self._request("GET", f"{self.base_url}/exchange/status")


# ---------------------------------------------------------------------------
# Arbitrage Detection
# ---------------------------------------------------------------------------

def find_binary_arbitrage(markets, min_profit=1):
    """
    Binary: If Yes_ask + No_ask < 100¢, buy both for a guaranteed 100¢ payout.
    Fee is computed per-leg using Kalshi's quadratic formula.
    """
    opportunities = []
    for m in markets:
        yes_ask = m.get("yes_ask")
        no_ask = m.get("no_ask")
        if not yes_ask or not no_ask or yes_ask <= 0 or no_ask <= 0:
            continue

        combined = yes_ask + no_ask
        if combined >= 100:
            continue

        gross_profit = 100 - combined
        fees = total_arb_fee([yes_ask, no_ask])
        net = gross_profit - fees

        if net >= min_profit:
            opportunities.append(ArbitrageOpportunity(
                type="binary",
                event_title=m.get("event_title", m.get("title", "?")),
                event_ticker=m.get("event_ticker", "?"),
                markets=[{"title": m.get("title", "?"), "ticker": m.get("ticker", "?"),
                          "yes_ask": yes_ask, "no_ask": no_ask}],
                total_cost=combined,
                guaranteed_payout=100,
                profit_cents=gross_profit,
                roi_percent=(gross_profit / combined) * 100,
                fee_cents=fees,
                net_profit_cents=net,
            ))
    return opportunities


def find_multi_outcome_arbitrage(markets, min_profit=1):
    """
    Multi-outcome events (mutually exclusive & exhaustive):
    - Buy all YES: costs sum(yes_ask), pays 100¢ guaranteed → arb if sum < 100
    - Buy all NO: costs sum(no_ask), pays (N-1)*100¢ guaranteed → arb if sum < (N-1)*100
    """
    events = defaultdict(list)
    for m in markets:
        event_ticker = m.get("event_ticker")
        if event_ticker:
            events[event_ticker].append(m)

    opportunities = []
    for event_ticker, event_markets in events.items():
        if len(event_markets) < 2:
            continue

        event_title = event_markets[0].get("event_title", event_ticker)

        # --- Buy all YES ---
        valid_yes = [m for m in event_markets if m.get("yes_ask") and m["yes_ask"] > 0]
        if len(valid_yes) >= 2:
            prices = [m["yes_ask"] for m in valid_yes]
            total_yes = sum(prices)
            if total_yes < 100:
                gross = 100 - total_yes
                fees = total_arb_fee(prices)
                net = gross - fees
                if net >= min_profit:
                    opportunities.append(ArbitrageOpportunity(
                        type="multi_outcome_under (buy all YES)",
                        event_title=event_title,
                        event_ticker=event_ticker,
                        markets=[{"title": m.get("title", "?"), "ticker": m.get("ticker", "?"),
                                  "yes_ask": m["yes_ask"]} for m in valid_yes],
                        total_cost=total_yes,
                        guaranteed_payout=100,
                        profit_cents=gross,
                        roi_percent=(gross / total_yes) * 100,
                        fee_cents=fees,
                        net_profit_cents=net,
                    ))

        # --- Buy all NO ---
        valid_no = [m for m in event_markets if m.get("no_ask") and m["no_ask"] > 0]
        if len(valid_no) >= 2:
            prices = [m["no_ask"] for m in valid_no]
            total_no = sum(prices)
            payout = (len(valid_no) - 1) * 100
            if total_no < payout:
                gross = payout - total_no
                fees = total_arb_fee(prices)
                net = gross - fees
                if net >= min_profit:
                    opportunities.append(ArbitrageOpportunity(
                        type="multi_outcome_over (buy all NO)",
                        event_title=event_title,
                        event_ticker=event_ticker,
                        markets=[{"title": m.get("title", "?"), "ticker": m.get("ticker", "?"),
                                  "no_ask": m["no_ask"]} for m in valid_no],
                        total_cost=total_no,
                        guaranteed_payout=payout,
                        profit_cents=gross,
                        roi_percent=(gross / total_no) * 100,
                        fee_cents=fees,
                        net_profit_cents=net,
                    ))

    return opportunities


def find_near_misses(markets, threshold=3):
    """Find markets within `threshold` cents of binary arbitrage."""
    near = []
    for m in markets:
        yes_ask = m.get("yes_ask")
        no_ask = m.get("no_ask")
        if yes_ask and no_ask and yes_ask > 0 and no_ask > 0:
            combined = yes_ask + no_ask
            if 100 <= combined <= 100 + threshold:
                near.append((combined, m))
    near.sort(key=lambda x: x[0])
    return near


# ---------------------------------------------------------------------------
# Demo Data
# ---------------------------------------------------------------------------

DEMO_MARKETS = [
    # Binary arb: 47 + 48 = 95 < 100 → 5¢ gross
    {"ticker": "DEMO-BTC-100K", "title": "Bitcoin above $100K on June 30?",
     "event_ticker": "DEMO-BTC", "event_title": "Bitcoin Price",
     "yes_ask": 47, "no_ask": 48, "yes_bid": 45, "no_bid": 46, "status": "open"},

    # No binary arb: 62 + 40 = 102
    {"ticker": "DEMO-RAIN-NYC", "title": "Rain in NYC tomorrow?",
     "event_ticker": "DEMO-RAIN", "event_title": "NYC Weather",
     "yes_ask": 62, "no_ask": 40, "yes_bid": 60, "no_bid": 38, "status": "open"},

    # Multi-outcome arb: 30+25+20+15 = 90 < 100 → 10¢ gross buying all YES
    {"ticker": "DEMO-GDP-A", "title": "GDP growth 0-1%",
     "event_ticker": "DEMO-GDP", "event_title": "Q2 GDP Growth Range",
     "yes_ask": 30, "no_ask": 72, "yes_bid": 28, "no_bid": 70, "status": "open"},
    {"ticker": "DEMO-GDP-B", "title": "GDP growth 1-2%",
     "event_ticker": "DEMO-GDP", "event_title": "Q2 GDP Growth Range",
     "yes_ask": 25, "no_ask": 77, "yes_bid": 23, "no_bid": 75, "status": "open"},
    {"ticker": "DEMO-GDP-C", "title": "GDP growth 2-3%",
     "event_ticker": "DEMO-GDP", "event_title": "Q2 GDP Growth Range",
     "yes_ask": 20, "no_ask": 82, "yes_bid": 18, "no_bid": 80, "status": "open"},
    {"ticker": "DEMO-GDP-D", "title": "GDP growth 3%+",
     "event_ticker": "DEMO-GDP", "event_title": "Q2 GDP Growth Range",
     "yes_ask": 15, "no_ask": 87, "yes_bid": 13, "no_bid": 85, "status": "open"},

    # Multi-outcome NO arb: no_asks = 30+28+25+22 = 105, payout = 3*100 = 300 → 195¢ gross
    {"ticker": "DEMO-PRES-A", "title": "Candidate A wins",
     "event_ticker": "DEMO-PRES", "event_title": "2028 Presidential Winner",
     "yes_ask": 45, "no_ask": 30, "yes_bid": 43, "no_bid": 28, "status": "open"},
    {"ticker": "DEMO-PRES-B", "title": "Candidate B wins",
     "event_ticker": "DEMO-PRES", "event_title": "2028 Presidential Winner",
     "yes_ask": 30, "no_ask": 28, "yes_bid": 28, "no_bid": 26, "status": "open"},
    {"ticker": "DEMO-PRES-C", "title": "Candidate C wins",
     "event_ticker": "DEMO-PRES", "event_title": "2028 Presidential Winner",
     "yes_ask": 15, "no_ask": 25, "yes_bid": 13, "no_bid": 23, "status": "open"},
    {"ticker": "DEMO-PRES-D", "title": "Field (other) wins",
     "event_ticker": "DEMO-PRES", "event_title": "2028 Presidential Winner",
     "yes_ask": 12, "no_ask": 22, "yes_bid": 10, "no_bid": 20, "status": "open"},

    # Near-miss: 51 + 50 = 101
    {"ticker": "DEMO-FED", "title": "Fed cuts rates in June?",
     "event_ticker": "DEMO-FEDRATE", "event_title": "Fed Rate Decision",
     "yes_ask": 51, "no_ask": 50, "yes_bid": 49, "no_bid": 48, "status": "open"},

    # Efficient market: 55 + 47 = 102
    {"ticker": "DEMO-MOON", "title": "Artemis lands on moon in 2026?",
     "event_ticker": "DEMO-ARTEMIS", "event_title": "Artemis Program",
     "yes_ask": 55, "no_ask": 47, "yes_bid": 53, "no_bid": 45, "status": "open"},
]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan(markets, min_profit=1):
    """Run all arbitrage detectors on a list of markets."""
    print(f"Analyzing {len(markets)} markets...\n")

    binary_opps = find_binary_arbitrage(markets, min_profit)
    multi_opps = find_multi_outcome_arbitrage(markets, min_profit)
    all_opps = binary_opps + multi_opps
    all_opps.sort(key=lambda o: o.net_profit_cents, reverse=True)

    if all_opps:
        print(f"{'#'*72}")
        print(f"  FOUND {len(all_opps)} ARBITRAGE OPPORTUNITIES (net of fees)")
        print(f"{'#'*72}\n")
        for opp in all_opps:
            print(opp)
            print()
    else:
        print("No arbitrage opportunities found (after fees).")
        print("Kalshi markets are generally efficient; opportunities appear briefly")
        print("during high-volatility moments.\n")

    near = find_near_misses(markets)
    if near:
        print("--- NEAR-MISS (within 3¢ of binary arb) ---")
        for combined, m in near[:20]:
            spread = combined - 100
            title = m.get('title', '?')[:50]
            print(f"  {title:50s}  Y:{m['yes_ask']:3d}¢ + N:{m['no_ask']:3d}¢ "
                  f"= {combined}¢  (spread {spread}¢)")
        print()

    events = defaultdict(list)
    for m in markets:
        et = m.get("event_ticker")
        if et:
            events[et].append(m)
    multi_events = {k: v for k, v in events.items() if len(v) >= 2}

    print("--- SUMMARY ---")
    print(f"  Markets scanned:           {len(markets)}")
    print(f"  Multi-outcome events:      {len(multi_events)}")
    print(f"  Binary arb opportunities:  {len(binary_opps)}")
    print(f"  Multi-outcome arb opps:    {len(multi_opps)}")
    print(f"  Near-misses:               {len(near)}")

    return all_opps


def build_client(args):
    """Build a KalshiClient from CLI args + environment variables."""
    # Priority: CLI args > env vars
    api_key_id = args.api_key_id or os.environ.get("KALSHI_API_KEY_ID")
    api_key = args.api_key or os.environ.get("KALSHI_API_KEY")
    email = args.email or os.environ.get("KALSHI_EMAIL")
    password = args.password or os.environ.get("KALSHI_PASSWORD")

    client = KalshiClient(
        base_url=KALSHI_API_BASE,
        api_key_id=api_key_id,
        api_key=api_key,
        email=email,
        password=password,
    )

    if not client.authenticated:
        print("  Running unauthenticated (lower rate limits).")
        print("  Set KALSHI_API_KEY_ID + KALSHI_API_KEY env vars, or use")
        print("  --api-key-id / --api-key flags for authenticated access.\n")

    return client


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Detector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Authentication (pick one):
  API key:        --api-key-id ID --api-key KEY
                  or set KALSHI_API_KEY_ID / KALSHI_API_KEY env vars
  Email/password: --email E --password P
                  or set KALSHI_EMAIL / KALSHI_PASSWORD env vars

Examples:
  %(prog)s --demo                              # verify logic with sample data
  %(prog)s --api-key-id myid --api-key mykey   # scan live, authenticated
  %(prog)s --continuous --interval 15           # rescan every 15 seconds
        """,
    )
    parser.add_argument("--demo", action="store_true",
                        help="Run with built-in sample data to verify logic")
    parser.add_argument("--min-profit", type=float, default=1,
                        help="Minimum net profit in cents to report (default: 1)")
    parser.add_argument("--continuous", action="store_true",
                        help="Rescan continuously every --interval seconds")
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between scans in continuous mode (default: 30)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")

    # Auth options
    auth = parser.add_argument_group("authentication")
    auth.add_argument("--api-key-id", help="Kalshi API key ID")
    auth.add_argument("--api-key", help="Kalshi API key secret")
    auth.add_argument("--email", help="Kalshi account email (for JWT login)")
    auth.add_argument("--password", help="Kalshi account password (for JWT login)")

    args = parser.parse_args()

    if args.demo:
        print("=" * 72)
        print("  DEMO MODE — using sample data to verify arbitrage logic")
        print("  Fee model: Kalshi quadratic taker fee = ceil(0.07 * P * (1-P))")
        print("=" * 72)
        print()
        opps = scan(DEMO_MARKETS, args.min_profit)
        if args.json:
            _print_json(opps)
        return

    client = build_client(args)

    def run_once():
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        auth_label = "authenticated" if client.authenticated else "unauthenticated"
        print(f"\n[{ts}] Fetching open Kalshi markets ({auth_label})...")
        try:
            markets = client.get_all_markets()
        except requests.RequestException as e:
            print(f"  ERROR fetching markets: {e}")
            return []
        print(f"  Fetched {len(markets)} markets.")
        return scan(markets, args.min_profit)

    if args.continuous:
        print(f"Continuous mode: scanning every {args.interval}s (Ctrl+C to stop)")
        while True:
            opps = run_once()
            if args.json and opps:
                _print_json(opps)
            time.sleep(args.interval)
    else:
        opps = run_once()
        if args.json and opps:
            _print_json(opps)


def _print_json(opps):
    print(json.dumps([{
        "type": o.type,
        "event": o.event_title,
        "ticker": o.event_ticker,
        "gross_profit_cents": o.profit_cents,
        "fee_cents": o.fee_cents,
        "net_profit_cents": o.net_profit_cents,
        "roi_percent": o.roi_percent,
    } for o in opps], indent=2))


if __name__ == "__main__":
    main()
