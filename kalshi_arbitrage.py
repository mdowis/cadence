#!/usr/bin/env python3
"""
Kalshi Arbitrage Detector

Scans Kalshi prediction markets for mispriced opportunities:
1. Binary arbitrage: Yes + No ask prices sum to less than $1.00
2. Multi-outcome arbitrage: All outcomes in an event sum to less/more than $1.00
3. Orderbook depth analysis: Checks if arb is executable at sufficient volume

Supports both authenticated (higher rate limits) and unauthenticated access.

Usage:
    python kalshi_arbitrage.py --demo                    # Verify logic with sample data
    python kalshi_arbitrage.py                           # Scan live (unauthenticated)
    python kalshi_arbitrage.py --api-key-id X --private-key-path key.pem
    python kalshi_arbitrage.py --continuous --interval 15
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field


def _load_dotenv(path=".env"):
    """
    Load .env into os.environ (env vars take priority).

    Looks for .env in:
      1. The current working directory
      2. The directory of this source file (so `python /abs/path/dashboard.py`
         from anywhere still picks up the .env next to the script)

    Returns the path loaded, or None.
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


_DOTENV_LOADED_FROM = _load_dotenv()


# ---------------------------------------------------------------------------
# Minimal HTTP client (stdlib only, no external dependencies)
# ---------------------------------------------------------------------------

class HTTPError(Exception):
    """Raised for network failures and non-2xx HTTP responses."""
    def __init__(self, message, status_code=None, headers=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body


class HTTPResponse:
    """Response wrapper mimicking the small subset of requests we use."""
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self._body = body

    def json(self):
        if isinstance(self._body, bytes):
            return json.loads(self._body.decode())
        return json.loads(self._body)

    @property
    def text(self):
        return self._body.decode() if isinstance(self._body, bytes) else self._body

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            snippet = ""
            try:
                snippet = self.text[:200]
            except Exception:
                pass
            raise HTTPError(
                f"HTTP {self.status_code}: {snippet}",
                status_code=self.status_code,
                headers=self.headers,
                body=self._body,
            )


class HTTPClient:
    """
    Minimal urllib-based HTTP client.

    Replaces `requests` so the project has zero external dependencies.
    """

    def __init__(self, timeout=30):
        self.timeout = timeout
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        # Append query params
        if params:
            # Filter out None values
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(clean)

        # Merge headers (per-request overrides session headers)
        merged = dict(self.headers)
        if headers:
            merged.update(headers)

        # Encode JSON body if provided
        data = None
        if json is not None:
            import json as _json
            data = _json.dumps(json).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=data, headers=merged, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read()
                return HTTPResponse(resp.status, dict(resp.headers), body)
        except urllib.error.HTTPError as e:
            # HTTPError is a response-like object for 4xx/5xx responses.
            # Return it as a response so the caller can handle 429 etc.
            try:
                body = e.read()
            except Exception:
                body = b""
            return HTTPResponse(e.code, dict(e.headers or {}), body)
        except urllib.error.URLError as e:
            raise HTTPError(f"Network error: {e.reason}") from e
        except (TimeoutError, OSError) as e:
            raise HTTPError(f"Request failed: {e}") from e

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


# ---------------------------------------------------------------------------
# Kalshi RSA-PSS request signer
# ---------------------------------------------------------------------------
#
# Kalshi authenticates API requests by signing a message with your RSA
# private key. You download the private key as a PEM file when you create
# an API key pair in the Kalshi dashboard.
#
# For each request the client must set three headers:
#   KALSHI-ACCESS-KEY        = your API key ID
#   KALSHI-ACCESS-TIMESTAMP  = current Unix time in milliseconds
#   KALSHI-ACCESS-SIGNATURE  = base64(sign_rsa_pss(timestamp + METHOD + path))
#
# The signature uses RSA-PSS padding with SHA-256 and MGF1+SHA-256,
# salt length equal to the digest length. The path is the URL path WITHOUT
# the query string.

class KalshiSigner:
    """
    Signs Kalshi API requests with an RSA private key (PEM file).

    Tries two backends in order:
      1. `cryptography` package (fast, preferred if available)
      2. `openssl` CLI subprocess (zero-install fallback, works on any
         system with openssl installed, which is almost all of them)
    """

    def __init__(self, private_key_path):
        if not os.path.exists(private_key_path):
            raise FileNotFoundError(
                f"Private key file not found: {private_key_path}\n"
                f"Download your PEM file when creating a Kalshi API key and "
                f"set KALSHI_PRIVATE_KEY_PATH in .env"
            )
        self.private_key_path = os.path.abspath(private_key_path)
        self._cached_key = None
        self.backend = None

        # Try cryptography first. Catch broadly because broken/partial
        # installs can raise various errors at import or use time.
        # Suppress stderr during the attempt so broken Rust/cffi installs
        # don't spew panic traces onto the user's terminal.
        _stderr_fd = None
        _devnull_fd = None
        try:
            _stderr_fd = os.dup(2)
            _devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(_devnull_fd, 2)

            from cryptography.hazmat.primitives import serialization
            with open(self.private_key_path, "rb") as f:
                self._cached_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
            self.backend = "cryptography"
        except (ImportError, ModuleNotFoundError):
            pass  # fall through to openssl
        except BaseException:
            # Broken cryptography install, malformed key, or other failure.
            # Don't give up — try openssl fallback below.
            pass
        finally:
            # Restore stderr
            if _stderr_fd is not None:
                try:
                    os.dup2(_stderr_fd, 2)
                    os.close(_stderr_fd)
                except Exception:
                    pass
            if _devnull_fd is not None:
                try:
                    os.close(_devnull_fd)
                except Exception:
                    pass

        if self.backend == "cryptography":
            return

        # Fall back to openssl CLI
        import subprocess
        try:
            subprocess.run(
                ["openssl", "version"],
                capture_output=True, check=True,
            )
            self.backend = "openssl"
        except (FileNotFoundError, subprocess.CalledProcessError):
            raise RuntimeError(
                "Cannot sign Kalshi requests. Need ONE of:\n"
                "  1. The 'cryptography' Python package "
                "(pip install cryptography), or\n"
                "  2. The 'openssl' command-line tool on your PATH\n"
                "openssl is pre-installed on most systems. On Windows try "
                "Git Bash, WSL, or install OpenSSL from slproweb.com."
            )

    def sign(self, timestamp_ms, method, path):
        """
        Sign a Kalshi API request.

        Args:
            timestamp_ms: Current Unix time in milliseconds, as a string or int
            method: HTTP method in uppercase (e.g. "GET", "POST")
            path: URL path WITHOUT query string (e.g. "/trade-api/v2/markets")

        Returns:
            Base64-encoded signature string.
        """
        import base64
        # Strip query string if caller accidentally passed it
        if "?" in path:
            path = path.split("?", 1)[0]

        message = f"{timestamp_ms}{method}{path}".encode("utf-8")

        if self.backend == "cryptography":
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            signature = self._cached_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
            return base64.b64encode(signature).decode("ascii")

        # openssl CLI fallback
        import subprocess
        result = subprocess.run(
            [
                "openssl", "dgst", "-sha256",
                "-sign", self.private_key_path,
                "-sigopt", "rsa_padding_mode:pss",
                "-sigopt", "rsa_pss_saltlen:digest",
            ],
            input=message,
            capture_output=True,
            check=True,
        )
        return base64.b64encode(result.stdout).decode("ascii")


KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

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

# Kalshi's Fixed-Point Migration (Jan-Mar 2026):
#   The integer cents price fields (yes_bid, yes_ask, no_bid, no_ask,
#   last_price, liquidity, etc.) were deprecated on 2026-01-15 and REMOVED
#   on 2026-03-05. Responses now contain *_dollars fields as decimal strings
#   (e.g. "yes_ask_dollars": "0.6500"). Cadence normalizes these back into
#   integer cents so the rest of the codebase keeps working.
#
# See: https://docs.kalshi.com/getting_started/fixed_point_migration

_PRICE_FIELD_MAP = [
    ("yes_bid",        "yes_bid_dollars"),
    ("yes_ask",        "yes_ask_dollars"),
    ("no_bid",         "no_bid_dollars"),
    ("no_ask",         "no_ask_dollars"),
    ("last_price",     "last_price_dollars"),
    ("previous_yes_bid",  "previous_yes_bid_dollars"),
    ("previous_yes_ask",  "previous_yes_ask_dollars"),
    ("previous_price",    "previous_price_dollars"),
    ("liquidity",      "liquidity_dollars"),
]


def _parse_dollars_to_cents(value):
    """
    Parse a Kalshi dollar string like "0.6500" to integer cents.

    Returns None if the value is missing, empty, or unparseable.
    Subpenny prices are rounded to the nearest whole cent.
    """
    if value is None or value == "":
        return None
    try:
        dollars = float(value)
    except (ValueError, TypeError):
        return None
    return round(dollars * 100)


def normalize_market(m):
    """
    Normalize a Kalshi market dict so the rest of the codebase can read
    `yes_ask`, `no_ask`, etc. as integer cents regardless of which API
    format the market was returned in.

    Populates each legacy cents field from its *_dollars counterpart if
    the cents field is missing. Leaves explicit cent values untouched
    (so pre-migration snapshots and demo data still work).

    Mutates and returns `m`.
    """
    if not isinstance(m, dict):
        return m
    for cents_field, dollars_field in _PRICE_FIELD_MAP:
        if m.get(cents_field) is None and dollars_field in m:
            parsed = _parse_dollars_to_cents(m[dollars_field])
            if parsed is not None:
                m[cents_field] = parsed
    return m


class KalshiClient:
    """
    Kalshi API client supporting authenticated and unauthenticated access.

    Authenticated (higher rate limits + trading/portfolio access): Kalshi uses
    RSA-PSS signed requests. You need:
      - An API key ID string (from Kalshi dashboard)
      - A private key PEM file (downloaded when you created the key pair)

    Email/password is also supported as a fallback and gets a 24h JWT.
    """

    MAX_RETRIES = 4
    BASE_DELAY = 0.1  # seconds between paginated requests

    def __init__(self, base_url=KALSHI_API_BASE, api_key_id=None,
                 private_key_path=None, email=None, password=None):
        self.base_url = base_url
        self.session = HTTPClient()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cadence-arbitrage-scanner/1.0",
        })
        self.authenticated = False
        self.api_key_id = None
        self.signer = None

        # Prefer RSA key auth (the standard Kalshi auth method)
        if api_key_id and private_key_path:
            self._auth_with_key(api_key_id, private_key_path)
        # Fall back to email/password login for JWT
        elif email and password:
            self._auth_login(email, password)

    def _auth_with_key(self, api_key_id, private_key_path):
        """Set up RSA-PSS request signing."""
        self.api_key_id = api_key_id
        self.signer = KalshiSigner(private_key_path)
        self.authenticated = True
        print(f"  Authenticated via API key {api_key_id[:8]}... "
              f"(signer: {self.signer.backend})")

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

    def _signing_headers(self, method, url):
        """
        Generate Kalshi-ACCESS-* headers for this request.

        Timestamp is fresh each call (Kalshi rejects stale signatures).
        Path is extracted from URL without query string.
        """
        parsed = urllib.parse.urlparse(url)
        path = parsed.path  # no query string, per Kalshi spec
        timestamp_ms = str(int(time.time() * 1000))
        signature = self.signer.sign(timestamp_ms, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _request(self, method, url, **kwargs):
        """Make a request with retry on 429 (rate limit) responses."""
        for attempt in range(self.MAX_RETRIES):
            # Inject fresh signing headers per attempt (timestamp must be recent)
            call_kwargs = dict(kwargs)
            if self.signer:
                extra = self._signing_headers(method, url)
                existing = call_kwargs.get("headers") or {}
                call_kwargs["headers"] = {**existing, **extra}

            resp = self.session.request(method, url, **call_kwargs)
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

        # Final attempt after all retries
        call_kwargs = dict(kwargs)
        if self.signer:
            extra = self._signing_headers(method, url)
            existing = call_kwargs.get("headers") or {}
            call_kwargs["headers"] = {**existing, **extra}
        resp = self.session.request(method, url, **call_kwargs)
        resp.raise_for_status()
        return resp.json()

    def get_markets(self, limit=200, cursor=None, status="open", event_ticker=None):
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._request("GET", f"{self.base_url}/markets", params=params)

    MAX_PAGES = 500        # Safety cap: 500 pages x 1000 = 500,000 markets
    BASE_DELAY = 0.02      # Seconds between paginated requests (reduced for speed)
    PROGRESS_EVERY = 10    # Log a progress line every N pages

    def get_all_markets(self, status="open", max_markets=None, page_callback=None):
        """
        Page through all markets matching `status`.

        Uses Kalshi's max page size (1000). Detects infinite loops by
        tracking unique tickers — if a page yields no new ones, stops.

        Args:
            status: Kalshi status filter ("open", "closed", etc.)
            max_markets: Optional soft cap. Stop once we've collected this
                many unique markets even if more pages exist.
            page_callback: Optional function(markets_so_far, page_num) called
                after each page. Lets the caller show progressive updates
                (e.g. render partial results on the dashboard).

        Returns:
            List of unique market dicts.
        """
        all_markets = []
        seen_tickers = set()
        cursor = None
        t_start = time.time()
        logged_first_sample = False

        for page in range(self.MAX_PAGES):
            data = self.get_markets(limit=1000, cursor=cursor, status=status)
            markets = data.get("markets", [])
            if not markets:
                break

            # Log the raw keys of the first market on the first page so we
            # can detect future API field renames immediately.
            if not logged_first_sample and markets:
                sample = markets[0]
                price_keys = [k for k in sample.keys()
                              if "bid" in k or "ask" in k or "price" in k]
                print(f"  [scanner] first market ticker={sample.get('ticker')}, "
                      f"price fields: {sorted(price_keys)}", flush=True)
                logged_first_sample = True

            # Normalize each market (populate legacy cents fields from
            # new *_dollars fields if needed) and dedupe by ticker.
            new_count = 0
            for raw in markets:
                ticker = raw.get("ticker")
                if ticker and ticker not in seen_tickers:
                    seen_tickers.add(ticker)
                    all_markets.append(normalize_market(raw))
                    new_count += 1

            # Progressive update: let the caller show partial results
            if page_callback:
                try:
                    page_callback(all_markets, page + 1)
                except Exception as e:
                    print(f"  [scanner] page_callback error: {e}", flush=True)

            # Progress log (periodic, not every page)
            if (page + 1) % self.PROGRESS_EVERY == 0:
                elapsed = time.time() - t_start
                # How many markets have both sides quoted?
                quoted = sum(1 for m in all_markets
                             if m.get("yes_ask") and m.get("no_ask"))
                print(f"  [scanner] page {page + 1}: {len(all_markets)} unique "
                      f"markets ({quoted} with both sides quoted) in {elapsed:.1f}s",
                      flush=True)

            # If the page returned only duplicates, pagination is looping
            if new_count == 0:
                print(f"  [scanner] page {page + 1} yielded 0 new tickers, "
                      f"stopping (have {len(all_markets)})", flush=True)
                break

            # Soft cap
            if max_markets and len(all_markets) >= max_markets:
                print(f"  [scanner] hit max_markets={max_markets} cap, "
                      f"stopping pagination", flush=True)
                break

            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(self.BASE_DELAY)
        else:
            print(f"  [scanner] WARNING: hit MAX_PAGES={self.MAX_PAGES} "
                  f"safety limit. Collected {len(all_markets)} markets. "
                  f"Set CADENCE_MAX_MARKETS to limit scan size, or contact "
                  f"maintainer to raise MAX_PAGES.", flush=True)
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
    private_key_path = args.private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    email = args.email or os.environ.get("KALSHI_EMAIL")
    password = args.password or os.environ.get("KALSHI_PASSWORD")

    client = KalshiClient(
        base_url=KALSHI_API_BASE,
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        email=email,
        password=password,
    )

    if not client.authenticated:
        print("  Running unauthenticated (lower rate limits).")
        print("  Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env,")
        print("  or use --api-key-id and --private-key-path flags.\n")

    return client


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Detector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Authentication (pick one):
  RSA key:        --api-key-id ID --private-key-path /path/to/key.pem
                  or set KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH env vars
  Email/password: --email E --password P
                  or set KALSHI_EMAIL / KALSHI_PASSWORD env vars

Examples:
  %(prog)s --demo                                       # sample data
  %(prog)s --api-key-id myid --private-key-path key.pem # scan live
  %(prog)s --continuous --interval 15                   # rescan every 15s
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
    auth.add_argument("--private-key-path",
                      help="Path to your Kalshi RSA private key PEM file")
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
        except HTTPError as e:
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
