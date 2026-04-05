#!/usr/bin/env python3
"""
Dashboard server for Kalshi Arbitrage Detector.

Serves the interactive dashboard and provides a JSON API that
runs the same arbitrage detection logic as the CLI tool.

Usage:
    python dashboard.py                          # Demo data on :8050
    python dashboard.py --port 8080              # Custom port
    python dashboard.py --live                   # Live Kalshi data
    python dashboard.py --live --api-key-id X --api-key K  # Authenticated
"""

import argparse
import json
import math
import os
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# Import the core scanner
from kalshi_arbitrage import (
    KalshiClient,
    DEMO_MARKETS,
    find_binary_arbitrage,
    find_multi_outcome_arbitrage,
    find_near_misses,
    kalshi_fee_per_contract,
    total_arb_fee,
    KALSHI_API_BASE,
)

# Shared state for the latest scan results
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


def run_scan(markets, min_profit=1, mode="demo"):
    """Run scan and store results in shared state."""
    binary_opps = find_binary_arbitrage(markets, min_profit)
    multi_opps = find_multi_outcome_arbitrage(markets, min_profit)
    all_opps = binary_opps + multi_opps
    all_opps.sort(key=lambda o: o.net_profit_cents, reverse=True)

    near = find_near_misses(markets)

    from collections import defaultdict
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


def background_scanner(client, interval, min_profit):
    """Background thread that periodically fetches and scans."""
    while True:
        try:
            markets = client.get_all_markets()
            run_scan(markets, min_profit, mode="live")
            print(f"  [{time.strftime('%H:%M:%S')}] Scanned {len(markets)} markets")
        except Exception as e:
            with _scan_lock:
                _latest_scan["error"] = str(e)
            print(f"  [{time.strftime('%H:%M:%S')}] Scan error: {e}")
        time.sleep(interval)


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves the dashboard and JSON API."""

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = Path(__file__).parent / "dashboard.html"
            self.wfile.write(html_path.read_bytes())
        elif self.path == "/api/scan":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with _scan_lock:
                self.wfile.write(json.dumps(_latest_scan).encode())
        elif self.path == "/api/fee-curve":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            curve = []
            for p in range(1, 100):
                curve.append({
                    "price": p,
                    "taker_fee": kalshi_fee_per_contract(p),
                    "maker_fee": kalshi_fee_per_contract(p, maker=True),
                })
            self.wfile.write(json.dumps(curve).encode())
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass  # Suppress request logs


def main():
    parser = argparse.ArgumentParser(description="Kalshi Arbitrage Dashboard")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--live", action="store_true",
                        help="Fetch live Kalshi data instead of demo")
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between live scans (default: 30)")
    parser.add_argument("--min-profit", type=float, default=1)
    parser.add_argument("--api-key-id", help="Kalshi API key ID")
    parser.add_argument("--api-key", help="Kalshi API key secret")
    parser.add_argument("--email", help="Kalshi email")
    parser.add_argument("--password", help="Kalshi password")
    args = parser.parse_args()

    # Initial scan with demo data
    run_scan(DEMO_MARKETS, args.min_profit, mode="demo")

    if args.live:
        api_key_id = args.api_key_id or os.environ.get("KALSHI_API_KEY_ID")
        api_key = args.api_key or os.environ.get("KALSHI_API_KEY")
        email = args.email or os.environ.get("KALSHI_EMAIL")
        password = args.password or os.environ.get("KALSHI_PASSWORD")

        client = KalshiClient(
            base_url=KALSHI_API_BASE,
            api_key_id=api_key_id, api_key=api_key,
            email=email, password=password,
        )
        t = threading.Thread(
            target=background_scanner,
            args=(client, args.interval, args.min_profit),
            daemon=True,
        )
        t.start()
        print(f"  Live scanning every {args.interval}s in background")

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    print(f"\n  Dashboard running at http://localhost:{args.port}")
    print(f"  Mode: {'LIVE' if args.live else 'DEMO'}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
