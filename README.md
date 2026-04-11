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

## Quick Start (2 steps)

No `pip install` needed. Cadence uses only Python's standard library. Requires Python 3.8+.

```bash
# 1. Configure: copy the template and add your Kalshi API key
cp .env.example .env
# edit .env and fill in KALSHI_API_KEY_ID and KALSHI_API_KEY

# 2. Run the dashboard
python dashboard.py
```

Then open **http://localhost:8050** in your browser. The dashboard is the single entry point — click the **Start** button next to "Market Scanner" to begin scanning, then **Start** next to "Auto Executor" when you're ready to trade. The dry-run toggle lets you simulate trades without placing real orders.

### CLI alternatives

If you prefer running pieces from the command line:

```bash
# Verify detection logic with built-in sample data (no API key needed)
python kalshi_arbitrage.py --demo

# One-shot CLI scan
python kalshi_arbitrage.py

# CLI executor in dry-run mode
python executor.py --dry-run
```

All scripts pick up configuration from `.env` automatically.

## Architecture

```
dashboard.py           Single entry point: loads .env, starts web server
dashboard.html         Interactive frontend with start/stop controls
process_controller.py  Manages scanner and executor background threads
kalshi_arbitrage.py    Scanner: finds opportunities, computes fees
risk_manager.py        Risk engine: gates every trade through limits
executor.py            Order placement via Kalshi trading API
notifier.py            Telegram notifier (optional, background thread)
.env.example           Configuration template (copy to .env)
```

`dashboard.py` orchestrates everything. It loads `.env`, authenticates with Kalshi, initializes the risk manager, and starts a local web server. The dashboard controls the scanner and executor threads via HTTP endpoints - no separate processes to manage.

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
| Drawdown kill switch (trailing) | 10% from peak | Auto-halts all trading. Peak updates as equity grows, so the threshold trails your high-water mark. |
| Daily loss limit | $20 flat, or % of daily start | No new trades after hitting today's loss floor. Both flat cents and % of today's starting balance are supported; the smaller wins. |
| Per-trade size cap | $5 flat, or % of equity | Rejects oversized arbs. Both flat cents and % of current equity are supported; the smaller wins. Dynamic % trails your equity up or down. |
| Total exposure cap | $100 | No new trades when fully deployed |
| Per-event exposure | $20 | Prevents concentration in one outcome |
| Consecutive loss breaker | 5 losses, 5min cooldown | Automatic pause after streak |
| Min net profit | 1c | Ignores sub-penny arbs |
| Min ROI | 0.5% | Ignores trades not worth the execution risk |

**Trailing limits.** Three limits trail your equity automatically:
- **Drawdown kill switch** tracks peak equity — as you profit, your safety margin moves up with you
- **`CADENCE_MAX_PER_TRADE_PCT`** (opt-in) scales trade size with current equity
- **`CADENCE_DAILY_LOSS_LIMIT_PCT`** (opt-in) scales daily loss cap with today's starting balance

When both flat and % limits are set for per-trade or daily-loss, the more restrictive one wins at any given moment. Everything else is flat.

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
python dashboard.py
```

That's it. All configuration comes from `.env`.

**Process control buttons** (top of dashboard):

- **Market Scanner** Start/Stop -- Fetches Kalshi markets on an interval, runs arb detection, syncs your balance. Requires authentication.
- **Auto Executor** Start/Stop -- Places orders on detected opportunities. Requires scanner to be running first. Toggle between DRY RUN (simulate) and LIVE modes with a confirmation prompt.

**Dashboard panels:**

- **Process control** -- Start/Stop buttons, status dots, uptime, trade counts
- **Stats row** -- Markets scanned, arb count, near misses, color-coded cards
- **Risk panel** -- Live equity, cash balance (LIVE/LOCAL indicator), daily P&L, exposure, drawdown progress bar, emergency kill switch button
- **Opportunity cards** -- Expandable, with stacked cost/fee/profit bar, per-leg detail
- **Spread heatmap** -- Every market as a colored cell (green = arb, yellow = near miss)
- **Fee curve chart** -- Canvas-rendered Kalshi quadratic fee parabola (taker vs maker)
- **Profit breakdown** -- Stacked bar chart comparing opportunities side by side
- **Near misses** -- Markets within 3c of arbitrage, ranked by closeness

Auto-refreshes every 15 seconds.

## Configuration

All configuration lives in `.env`. Copy `.env.example` to get started:

```bash
cp .env.example .env
```

### Authentication (required for trading)

Kalshi uses RSA-PSS signed requests. When you create an API key pair at https://kalshi.com/account/api-keys you get two things:

1. **API key ID** (a short string shown in the dashboard)
2. **Private key PEM file** (downloaded once — keep it safe!)

Put the PEM file somewhere on your machine and point Cadence at it:

```bash
# Primary: API key ID + path to your PEM file
KALSHI_API_KEY_ID=your_key_id
KALSHI_PRIVATE_KEY_PATH=/path/to/your/kalshi_private_key.pem

# Alternative: email/password (gets a 24h JWT, less preferred)
KALSHI_EMAIL=you@example.com
KALSHI_PASSWORD=yourpass
```

Every request Cadence sends to Kalshi is signed with your private key:
```
KALSHI-ACCESS-KEY        = your API key ID
KALSHI-ACCESS-TIMESTAMP  = current Unix time in ms
KALSHI-ACCESS-SIGNATURE  = base64(RSA-PSS sign(timestamp + METHOD + path))
```

The signer uses the `cryptography` Python package if available; otherwise it falls back to the `openssl` CLI tool (pre-installed on Mac/Linux, available via Git Bash or WSL on Windows). No `pip install` required for either path.

Without credentials, the dashboard still runs in demo mode so you can explore the UI.

### Risk limits (all in cents)

```bash
CADENCE_EQUITY=5000                    # Starting equity if balance sync off
CADENCE_SYNC_BALANCE=true              # Pull real cash balance from Kalshi
CADENCE_MAX_DRAWDOWN_PCT=10            # Kill switch at this % drawdown from peak (trailing)
CADENCE_DAILY_LOSS_LIMIT=2000          # Flat max loss per day in cents ($20)
CADENCE_DAILY_LOSS_LIMIT_PCT=          # Dynamic: % of today's starting balance (opt-in)
CADENCE_MAX_PER_TRADE=500              # Flat max cost per trade in cents ($5)
CADENCE_MAX_PER_TRADE_PCT=             # Dynamic: % of current equity (opt-in)
CADENCE_MAX_EXPOSURE=10000             # Max total open exposure ($100)
CADENCE_MAX_CONSECUTIVE_LOSSES=5       # Circuit breaker threshold
CADENCE_MIN_PROFIT=2                   # Minimum net profit to trade
```

For the dynamic (`_PCT`) limits: leave blank or omit to disable, set to `2` for 2%, etc. When both the flat cents and % variants are set for the same limit, the smaller (more restrictive) one wins on every trade check.

### Scanner / executor

```bash
CADENCE_INTERVAL=15                    # Seconds between scans
CADENCE_CONTRACTS=1                    # Contracts per leg
CADENCE_MAX_MARKETS=                   # Cap markets per scan (blank = all)
CADENCE_PORT=8050                      # Dashboard port
CADENCE_STATE_FILE=risk_state.json     # Persist risk state across restarts
```

**About `CADENCE_MAX_MARKETS`:** Kalshi has tens of thousands of open markets across series (weather, crypto, stocks, etc.). A full scan normally takes 30–90 seconds. If your scan interval is shorter than a full scan, set this to a smaller number (e.g. `5000`) so the scanner can keep up. Leave blank to scan everything.

Environment variables set in your shell take priority over `.env`. Both beat the defaults.

## Account Balance Tracking

When `CADENCE_SYNC_BALANCE=true` (default), the scanner calls Kalshi's `/portfolio/balance` endpoint on every scan cycle and updates the risk manager with your real cash balance. The risk manager computes:

```
total_equity = cash_balance + open_position_cost_basis
daily_pnl    = total_equity - daily_starting_balance
drawdown_pct = (peak_equity - current_equity) / peak_equity * 100
```

The dashboard displays both **Equity** (synced total) and **Cash Balance** (from Kalshi directly). An indicator shows `LIVE` when the equity number is backed by a real balance sync, or `LOCAL` if it's a simulated starting value.

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
| GET | `/api/risk` | Risk state, equity, drawdown, balance sync info |
| GET | `/api/processes` | Scanner & executor status, uptime, config |
| GET | `/api/fee-curve` | Fee per contract at each price point (1-99c) |
| POST | `/api/scanner/start` | Start the market scanner thread |
| POST | `/api/scanner/stop` | Stop the market scanner thread |
| POST | `/api/executor/start` | Start the auto-executor (body: `{dry_run, contracts}`) |
| POST | `/api/executor/stop` | Stop the auto-executor |
| POST | `/api/config` | Update runtime config (interval, min_profit, etc.) |
| POST | `/api/risk/kill-switch/activate` | Halt all trading |
| POST | `/api/risk/kill-switch/deactivate` | Resume trading |
| POST | `/api/risk/reset-daily` | Reset daily P&L, baseline, peak to current equity |
| GET | `/api/diagnostics` | Full diagnostic dump (auth, trader, processes, telegram) |
| POST | `/api/test-fetch` | Synchronous Kalshi market fetch for debugging |
| POST | `/api/telegram/test` | Send a test Telegram message |

## Telegram Notifications (optional)

Cadence can push updates to a Telegram bot so you don't have to babysit the dashboard. Setup:

1. Message [@BotFather](https://t.me/botfather) on Telegram → `/newbot` → follow prompts → copy the bot token
2. Send `/start` to your new bot
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find the `chat.id` in the JSON
4. Add to `.env`:
   ```bash
   CADENCE_TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   CADENCE_TELEGRAM_CHAT_ID=987654321
   ```
5. Restart the dashboard and visit `POST http://localhost:8050/api/telegram/test` to verify

You'll receive notifications for:
- **Startup/shutdown** — brief message on boot and stop
- **Every successful trade** — event, type, contracts, profit, detail
- **Partial fills and unwinds** — critical for monitoring the unwind path
- **Kill switch activations** — always, never deduped
- **Scanner errors** — deduped within a 60s window so repeated errors don't spam
- **Hourly status** — equity, daily P&L, drawdown, exposure, open positions, trades today

Optional: `CADENCE_TELEGRAM_STATUS_INTERVAL=3600` controls the periodic status interval (default 1 hour).

All Telegram calls run in a background thread so slow network to Telegram never blocks the scanner or executor. Failed sends are logged and retried with exponential backoff on 5xx; 4xx failures (bad token, bad chat id) fail fast.

### Remote commands (opt-in)

By default the bot only sends notifications. To also **receive** commands from Telegram, add:

```bash
CADENCE_TELEGRAM_COMMANDS_ENABLED=true
```

With commands enabled you can message the bot and remotely control Cadence:

| Command | Effect |
|---|---|
| `/help` | List all available commands |
| `/status` | Equity, daily P&L, drawdown, exposure, scanner/executor state |
| `/positions` | Current open positions from Kalshi |
| `/decisions` | Last 10 executor decisions with skip reasons |
| `/config` | Current risk config: drawdown limit, per-trade cap, daily loss cap |
| `/kill` | **Halt all trading immediately** (no confirmation) |
| `/resume` | Deactivate kill switch, resume trading |
| `/reset` | Reset daily P&L and baseline to current equity |
| `/scanner_start` / `/scanner_stop` | Control the scanner thread |
| `/exec_start` | Start auto-executor in **dry run** (no real orders) |
| `/exec_stop` | Stop auto-executor |
| `/exec_live` | Start auto-executor in **LIVE mode** — requires `CONFIRM` reply within 30s |

Security model:
- **Chat ID allowlist**: messages from anyone but your configured `CADENCE_TELEGRAM_CHAT_ID` are silently dropped. No reply, no log of their ID.
- **Opt-in**: you must explicitly set `CADENCE_TELEGRAM_COMMANDS_ENABLED=true`. Without it the bot only sends, never reads.
- **Confirmation for live trading**: `/exec_live` requires a two-step handshake. After the command, the bot asks you to reply `CONFIRM` or `YES` within 30 seconds. Anything else (including `/exec_live` again) cancels.
- **Boot-time skip**: commands queued in Telegram from before the bot started up are skipped, so restarting won't replay old `/kill` commands.

Kill switch does **not** require confirmation — it's a panic button. Better to accidentally halt than accidentally trade.

## Testing

```bash
python test_arbitrage.py       # 22 tests: fee model, detection, migration normalization
python test_risk_manager.py    # 36 tests: kill switch, drawdown, balance sync, dynamic limits
python test_signer.py          #  6 tests: RSA-PSS signature round trip
python test_dashboard.py       #  1 test:  JS syntax check
python test_executor.py        # 21 tests: order body, fill parsing, unwind, safety
python test_notifier.py        # 22 tests: Telegram queue, dedup, retries, format, commands
```

## Project Structure

```
cadence/
  dashboard.py            Single entry point, web server, .env loader
  dashboard.html          Interactive frontend with start/stop controls
  process_controller.py   Scanner & executor thread management
  kalshi_arbitrage.py     Scanner + Kalshi API client + fee model
  risk_manager.py         Risk config, balance sync, trade gating
  executor.py             Order placement via Kalshi trading API
  notifier.py             Telegram notifier (optional, background thread)
  test_arbitrage.py       Scanner and fee model tests
  test_risk_manager.py    Risk management and balance sync tests
  test_signer.py          Kalshi RSA signing tests
  test_executor.py        Order construction and execution safety tests
  test_notifier.py        Telegram notifier tests (no real network calls)
  test_dashboard.py       Dashboard JS syntax check
  .env.example            Configuration template
  .gitignore              Excludes .env, __pycache__, venvs, state files
  requirements.txt        (empty - no external dependencies, stdlib only)
```

## Disclaimer

This software is for educational and research purposes. Trading on Kalshi involves real financial risk. Arbitrage opportunities on efficient markets are rare, fleeting, and may not be executable at the displayed prices due to liquidity constraints, latency, or partial fills. Always start with `--dry-run`, use small position sizes, and monitor actively.
