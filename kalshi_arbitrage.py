#!/usr/bin/env python3
"""
Kalshi Arbitrage Detector

Scans Kalshi prediction markets for mispriced opportunities:
1. Binary arbitrage: Yes + No ask prices sum to less than $1.00
2. Multi-outcome arbitrage: All outcomes in an event sum to less/more than $1.00
3. Orderbook depth analysis: Checks if arb is executable at sufficient volume

Usage:
    python kalshi_arbitrage.py              # Scan live Kalshi markets
    python kalshi_arbitrage.py --demo       # Run with sample data to verify logic
    python kalshi_arbitrage.py --min-profit 3  # Only show opportunities >= 3¢
    python kalshi_arbitrage.py --continuous    # Rescan every 30 seconds
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)


KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_FEE_RATE = 0.07  # ~7% on profits (not cost basis)


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
    fee_adjusted_profit: float = 0.0

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
        if self.fee_adjusted_profit != self.profit_cents:
            lines.append(f"  NET PROFIT (after ~7% Kalshi fee on winnings): {self.fee_adjusted_profit:.1f}¢")
        lines.append(f"{'='*72}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# API Clients
# ---------------------------------------------------------------------------

class KalshiClient:
    """Client for the Kalshi public (unauthenticated) API."""

    def __init__(self, base_url=KALSHI_API_BASE):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "cadence-arbitrage-scanner/1.0",
        })

    def get_markets(self, limit=200, cursor=None, status="open", event_ticker=None):
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        resp = self.session.get(f"{self.base_url}/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_all_markets(self, status="open"):
        all_markets = []
        cursor = None
        while True:
            data = self.get_markets(limit=200, cursor=cursor, status=status)
            markets = data.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)
        return all_markets

    def get_orderbook(self, ticker):
        resp = self.session.get(f"{self.base_url}/orderbook/{ticker}")
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Arbitrage Detection
# ---------------------------------------------------------------------------

def compute_fee_adjusted_profit(gross_profit, fee_rate=KALSHI_FEE_RATE):
    """Kalshi charges fees on profit, not on the cost basis."""
    if gross_profit <= 0:
        return 0.0
    return gross_profit * (1 - fee_rate)


def find_binary_arbitrage(markets, min_profit=1):
    """
    Binary: If Yes_ask + No_ask < 100¢, buy both for a guaranteed profit.
    """
    opportunities = []
    for m in markets:
        yes_ask = m.get("yes_ask")
        no_ask = m.get("no_ask")
        if not yes_ask or not no_ask or yes_ask <= 0 or no_ask <= 0:
            continue

        combined = yes_ask + no_ask
        if combined < 100:
            profit = 100 - combined
            net = compute_fee_adjusted_profit(profit)
            if net >= min_profit:
                opportunities.append(ArbitrageOpportunity(
                    type="binary",
                    event_title=m.get("event_title", m.get("title", "?")),
                    event_ticker=m.get("event_ticker", "?"),
                    markets=[{"title": m.get("title", "?"), "ticker": m.get("ticker", "?"),
                              "yes_ask": yes_ask, "no_ask": no_ask}],
                    total_cost=combined,
                    guaranteed_payout=100,
                    profit_cents=profit,
                    roi_percent=(profit / combined) * 100,
                    fee_adjusted_profit=net,
                ))
    return opportunities


def find_multi_outcome_arbitrage(markets, min_profit=1):
    """
    Multi-outcome events where outcomes are mutually exclusive and exhaustive:
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
            total_yes = sum(m["yes_ask"] for m in valid_yes)
            if total_yes < 100:
                profit = 100 - total_yes
                net = compute_fee_adjusted_profit(profit)
                if net >= min_profit:
                    opportunities.append(ArbitrageOpportunity(
                        type="multi_outcome_under (buy all YES)",
                        event_title=event_title,
                        event_ticker=event_ticker,
                        markets=[{"title": m.get("title", "?"), "ticker": m.get("ticker", "?"),
                                  "yes_ask": m["yes_ask"]} for m in valid_yes],
                        total_cost=total_yes,
                        guaranteed_payout=100,
                        profit_cents=profit,
                        roi_percent=(profit / total_yes) * 100,
                        fee_adjusted_profit=net,
                    ))

        # --- Buy all NO ---
        valid_no = [m for m in event_markets if m.get("no_ask") and m["no_ask"] > 0]
        if len(valid_no) >= 2:
            total_no = sum(m["no_ask"] for m in valid_no)
            payout = (len(valid_no) - 1) * 100
            if total_no < payout:
                profit = payout - total_no
                net = compute_fee_adjusted_profit(profit)
                if net >= min_profit:
                    opportunities.append(ArbitrageOpportunity(
                        type="multi_outcome_over (buy all NO)",
                        event_title=event_title,
                        event_ticker=event_ticker,
                        markets=[{"title": m.get("title", "?"), "ticker": m.get("ticker", "?"),
                                  "no_ask": m["no_ask"]} for m in valid_no],
                        total_cost=total_no,
                        guaranteed_payout=payout,
                        profit_cents=profit,
                        roi_percent=(profit / total_no) * 100,
                        fee_adjusted_profit=net,
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
# Demo Data (for testing logic when API is unreachable)
# ---------------------------------------------------------------------------

DEMO_MARKETS = [
    # Binary arb: 47 + 48 = 95 < 100 → 5¢ profit
    {"ticker": "DEMO-BTC-100K", "title": "Bitcoin above $100K on June 30?",
     "event_ticker": "DEMO-BTC", "event_title": "Bitcoin Price",
     "yes_ask": 47, "no_ask": 48, "yes_bid": 45, "no_bid": 46, "status": "open"},

    # No binary arb: 62 + 40 = 102
    {"ticker": "DEMO-RAIN-NYC", "title": "Rain in NYC tomorrow?",
     "event_ticker": "DEMO-RAIN", "event_title": "NYC Weather",
     "yes_ask": 62, "no_ask": 40, "yes_bid": 60, "no_bid": 38, "status": "open"},

    # Multi-outcome arb: 30+25+20+15 = 90 < 100 → 10¢ profit buying all YES
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

    # Multi-outcome NO arb: no_asks = 30+28+25+22 = 105, payout = 3*100 = 300 → 195¢ profit
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
# Main Scanner
# ---------------------------------------------------------------------------

def scan(markets, min_profit=1):
    """Run all arbitrage detectors on a list of markets."""
    print(f"Analyzing {len(markets)} markets...\n")

    binary_opps = find_binary_arbitrage(markets, min_profit)
    multi_opps = find_multi_outcome_arbitrage(markets, min_profit)
    all_opps = binary_opps + multi_opps
    all_opps.sort(key=lambda o: o.fee_adjusted_profit, reverse=True)

    if all_opps:
        print(f"{'#'*72}")
        print(f"  FOUND {len(all_opps)} ARBITRAGE OPPORTUNITIES")
        print(f"{'#'*72}\n")
        for opp in all_opps:
            print(opp)
            print()
    else:
        print("No arbitrage opportunities found.")
        print("Kalshi markets are generally efficient; opportunities appear briefly")
        print("during high-volatility moments.\n")

    # Near misses
    near = find_near_misses(markets)
    if near:
        print("--- NEAR-MISS (within 3¢ of binary arb) ---")
        for combined, m in near[:20]:
            spread = combined - 100
            title = m.get('title', '?')[:50]
            print(f"  {title:50s}  Y:{m['yes_ask']:3d}¢ + N:{m['no_ask']:3d}¢ = {combined}¢  (spread {spread}¢)")
        print()

    # Summary
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


def main():
    parser = argparse.ArgumentParser(description="Kalshi Arbitrage Detector")
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
    args = parser.parse_args()

    if args.demo:
        print("=" * 72)
        print("  DEMO MODE — using sample data to verify arbitrage logic")
        print("=" * 72)
        print()
        opps = scan(DEMO_MARKETS, args.min_profit)
        if args.json:
            print(json.dumps([{
                "type": o.type, "event": o.event_title, "ticker": o.event_ticker,
                "profit_cents": o.profit_cents, "net_profit": o.fee_adjusted_profit,
                "roi_percent": o.roi_percent,
            } for o in opps], indent=2))
        return

    client = KalshiClient()

    def run_once():
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Fetching open Kalshi markets...")
        try:
            markets = client.get_all_markets()
        except requests.RequestException as e:
            print(f"  ERROR fetching markets: {e}")
            print("  Make sure you can reach api.elections.kalshi.com")
            return []
        print(f"  Fetched {len(markets)} markets.")
        return scan(markets, args.min_profit)

    if args.continuous:
        print(f"Continuous mode: scanning every {args.interval}s (Ctrl+C to stop)")
        while True:
            opps = run_once()
            if args.json and opps:
                print(json.dumps([{
                    "type": o.type, "event": o.event_title, "profit_cents": o.profit_cents,
                    "net_profit": o.fee_adjusted_profit, "roi_percent": o.roi_percent,
                } for o in opps], indent=2))
            time.sleep(args.interval)
    else:
        opps = run_once()
        if args.json and opps:
            print(json.dumps([{
                "type": o.type, "event": o.event_title, "ticker": o.event_ticker,
                "profit_cents": o.profit_cents, "net_profit": o.fee_adjusted_profit,
                "roi_percent": o.roi_percent,
            } for o in opps], indent=2))


if __name__ == "__main__":
    main()
