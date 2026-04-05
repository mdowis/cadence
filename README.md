# Cadence

Automated arbitrage detection and execution for [Kalshi](https://kalshi.com) prediction markets. Scans for mispriced contracts, calculates profit net of Kalshi's quadratic fee model, enforces risk limits, and optionally auto-executes trades via the Kalshi API.

## How It Works

Kalshi contracts pay out $1.00 (100 cents) if an event occurs, $0.00 otherwise. Arbitrage exists when you can buy a combination of contracts whose total cost is less than the guaranteed payout:

| Strategy | Condition | Payout |
|---|---|---|
| **Binary** | Yes ask + No ask < 100c on one market | 100c (one side always wins) |
| **Multi-outcome YES** | Sum of all Yes asks < 100c across an event | 100c (exactly one outcome wins) |
| **Multi-outcome NO** | Sum of all No asks < (N-1) x 100c | (N-1) x 100c (N-1 No's always win) |

All profits are calculated net of Kalshi's quadratic taker fee: `ceil(0.07 x P x (1 - P))` per contract, where P is the price in dollars. Fees peak at 2c/contract at 50c and shrink toward the extremes.

## Quick Start

```bash
# Install
pip install requests

# Verify logic with built-in sample data
python kalshi_arbitrage.py --demo

# Scan live markets (unauthenticated, lower rate limits)
python kalshi_arbitrage.py

# Scan live with your API key (higher rate limits)
python kalshi_arbitrage.py --api-key-id YOUR_ID --api-key YOUR_KEY

# Launch the visual dashboard
python dashboard.py
# Then open http://localhost:8050
```

## Architecture

```
kalshi_arbitrage.py    Scanner: finds opportunities, computes fees
risk_manager.py        Risk engine: gates every trade through limits
executor.py            Auto-trader: scanner + risk + Kalshi order API
dashboard.py           Web server: serves dashboard + JSON API
dashboard.html         Interactive frontend (single-file, no build step)
```

## Components

### Scanner (`kalshi_arbitrage.py`)

Fetches all open markets from the Kalshi API, groups them by event, and runs three detection algorithms. Supports both authenticated and unauthenticated access with automatic rate limit retry (exponential backoff on 429s).

```bash
# One-shot scan
python kalshi_arbitrage.py --api-key-id X --api-key K

# Continuous scanning every 15 seconds
python kalshi_arbitrage.py --continuous --interval 15

# Only show opportunities with >= 3c net profit
python kalshi_arbitrage.py --min-profit 3

# Machine-readable output
python kalshi_arbitrage.py --json
```

### Risk Manager (`risk_manager.py`)

Every trade must pass all of the following checks before execution:

| Check | Default | Description |
|---|---|---|
| Drawdown kill switch | 10% from peak | Auto-halts all trading, requires manual resume |
| Daily loss limit | $20 | No new trades after hitting daily P&L floor |
| Per-trade size cap | $5 | Rejects any single arb costing more |
| Total exposure cap | $100 | No new trades when fully deployed |
| Per-event exposure | $20 | Prevents concentration in one outcome |
| Consecutive loss breaker | 5 losses, 5min cooldown | Automatic pause after streak |
| Min net profit | 1c | Ignores sub-penny arbs |
| Min ROI | 0.5% | Ignores trades not worth the execution risk |

The kill switch can be triggered three ways:
1. **Automatically** by drawdown exceeding the configured threshold
2. **Automatically** by consecutive loss circuit breaker
3. **Manually** via the dashboard button or API call

State persists to a JSON file so position tracking and equity survive restarts.

### Auto-Executor (`executor.py`)

Wires the scanner and risk manager to the Kalshi order API. Scans for opportunities, risk-checks each one, and submits orders via the batch API (up to 20 legs atomically). All orders use IOC (immediate-or-cancel) to avoid leaving stale resting orders.

```bash
# ALWAYS start with a dry run
python executor.py --dry-run --api-key-id X --api-key K

# Live trading: $50 equity, $5 max per trade, scan every 15s
python executor.py \
    --api-key-id X --api-key K \
    --equity 5000 \
    --max-per-trade 500 \
    --interval 15

# Conservative: tight limits, state persistence
python executor.py \
    --api-key-id X --api-key K \
    --equity 2000 \
    --max-per-trade 200 \
    --daily-loss-limit 500 \
    --max-drawdown-pct 5 \
    --max-consecutive-losses 3 \
    --state-file risk_state.json
```

**Partial fill warning:** Arb trades are only risk-free when all legs execute. If the batch API reports fewer fills than legs, the executor logs a warning. Monitor these closely.

### Dashboard (`dashboard.py` + `dashboard.html`)

Interactive web dashboard served on `http://localhost:8050`. No build step, no npm, no external CDN. Just Python's stdlib HTTP server and vanilla JS with canvas rendering.

```bash
# Demo mode (sample data)
python dashboard.py

# Live mode with API key
python dashboard.py --live --api-key-id X --api-key K

# Custom port, faster scans
python dashboard.py --live --port 8080 --interval 10
```

**Dashboard panels:**

- **Stats row** -- Markets scanned, arb count, near misses, color-coded cards
- **Opportunity cards** -- Expandable, with stacked cost/fee/profit bar, per-leg detail
- **Risk panel** -- Equity, daily P&L, exposure, drawdown progress bar, kill switch button
- **Spread heatmap** -- Every market as a colored cell (green = arb, yellow = near miss)
- **Fee curve chart** -- Canvas-rendered Kalshi quadratic fee parabola (taker vs maker)
- **Profit breakdown** -- Stacked bar chart comparing opportunities side by side
- **Near misses** -- Markets within 3c of arbitrage, ranked by closeness

Auto-refreshes every 15 seconds.

## Authentication

Two methods, in priority order. Both the scanner and executor accept them:

**API key (recommended):**
```bash
# Via CLI flags
--api-key-id YOUR_KEY_ID --api-key YOUR_KEY_SECRET

# Via environment variables
export KALSHI_API_KEY_ID=your_key_id
export KALSHI_API_KEY=your_key_secret
```

**Email/password (gets a 24h JWT):**
```bash
--email you@example.com --password yourpass

# Or via env vars
export KALSHI_EMAIL=you@example.com
export KALSHI_PASSWORD=yourpass
```

Unauthenticated access works for market data scanning but has lower rate limits and cannot place orders.

## Fee Model

Kalshi uses a quadratic taker fee that depends on the contract price:

```
fee = ceil(0.07 * P * (1 - P))  per contract
```

| Price | Taker Fee | Effective Rate |
|---|---|---|
| 5c or 95c | 1c | 1.1% |
| 25c or 75c | 2c | 1.1% |
| 50c | 2c | 1.8% |

Fees are symmetric around 50c, peak there, and shrink toward the extremes. This means thin-margin arbs near mid-range prices can be wiped out by fees. All profit calculations in Cadence are net of these fees.

Maker fees use a 0.0175 coefficient instead of 0.07 (roughly 4x cheaper).

## API Endpoints (Dashboard)

When running `dashboard.py`, these JSON endpoints are available:

| Method | Path | Description |
|---|---|---|
| GET | `/api/scan` | Latest scan results, opportunities, near misses |
| GET | `/api/risk` | Current risk state, equity, drawdown, config |
| GET | `/api/fee-curve` | Fee per contract at each price point (1-99c) |
| POST | `/api/risk/kill-switch/activate` | Halt all trading |
| POST | `/api/risk/kill-switch/deactivate` | Resume trading |
| POST | `/api/risk/reset-daily` | Reset daily P&L and trade counters |

## Testing

```bash
python test_arbitrage.py       # 14 tests: fee model, detection, edge cases
python test_risk_manager.py    # 18 tests: kill switch, drawdown, limits, lifecycle
```

## Project Structure

```
cadence/
  kalshi_arbitrage.py     Core scanner + Kalshi API client + fee model
  risk_manager.py         Risk config, state tracking, trade gating
  executor.py             Auto-execution loop + Kalshi order API
  dashboard.py            HTTP server for dashboard + JSON API
  dashboard.html          Single-file interactive frontend
  test_arbitrage.py       Scanner and fee model tests
  test_risk_manager.py    Risk management tests
  requirements.txt        Python dependencies (requests)
  .gitignore              Excludes .env, __pycache__, venvs
```

## Disclaimer

This software is for educational and research purposes. Trading on Kalshi involves real financial risk. Arbitrage opportunities on efficient markets are rare, fleeting, and may not be executable at the displayed prices due to liquidity constraints, latency, or partial fills. Always start with `--dry-run`, use small position sizes, and monitor actively.
