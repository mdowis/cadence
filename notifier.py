"""
Telegram notifier for Cadence.

Sends trade, risk, and status events to a Telegram chat. Runs in a
background thread so slow Telegram API calls never block the scanner
or executor.

Setup:
  1. Create a bot via @BotFather on Telegram, get the TOKEN
  2. Send /start to your new bot
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat id
  4. Set in .env:
       CADENCE_TELEGRAM_BOT_TOKEN=<token>
       CADENCE_TELEGRAM_CHAT_ID=<chat_id>
"""

import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """
    Background-thread Telegram notifier.

    Thread-safe: any component can call .send() / .notify_*() without
    worrying about blocking. Messages are queued and sent by a worker.

    Rate limiting: identical messages within DEDUP_WINDOW_SECS are
    silently dropped to prevent flooding on repeated events.
    """

    DEDUP_WINDOW_SECS = 60          # suppress identical messages within 60s
    REQUEST_TIMEOUT_SECS = 10
    MAX_QUEUE = 200                 # drop oldest if queue is backed up
    MAX_RETRIES = 3

    def __init__(self, bot_token=None, chat_id=None, enabled=True):
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id else None
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self._queue = queue.Queue(maxsize=self.MAX_QUEUE)
        self._recent = {}  # {message_hash: last_send_time}
        self._recent_lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._sent_count = 0
        self._failed_count = 0
        self._dropped_count = 0

        if self.enabled:
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def stop(self):
        """Signal the worker to stop after draining the queue."""
        self._stop.set()
        if self._worker and self._worker.is_alive():
            # Unblock the queue.get() call
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Send primitives
    # ------------------------------------------------------------------

    def send(self, text, parse_mode="Markdown"):
        """
        Enqueue a message. Non-blocking. Thread-safe.

        Returns True if queued, False if dropped (disabled, full, or dedup).
        """
        if not self.enabled:
            return False

        # Dedup: silently drop identical messages within the window
        now = time.time()
        text_hash = hash(text)
        with self._recent_lock:
            # Clean up stale entries
            stale = [h for h, t in self._recent.items()
                     if now - t > self.DEDUP_WINDOW_SECS]
            for h in stale:
                del self._recent[h]
            if text_hash in self._recent:
                self._dropped_count += 1
                return False
            self._recent[text_hash] = now

        try:
            self._queue.put_nowait((text, parse_mode))
            return True
        except queue.Full:
            # Drop oldest and retry once
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((text, parse_mode))
                return True
            except (queue.Empty, queue.Full):
                self._dropped_count += 1
                return False

    # ------------------------------------------------------------------
    # Event helpers — specific notification types
    # ------------------------------------------------------------------

    def notify_startup(self, equity_cents=None, authenticated=False):
        lines = ["*Cadence started*"]
        if authenticated:
            lines.append("Auth: `OK`")
        else:
            lines.append("Auth: `demo only`")
        if equity_cents is not None:
            lines.append(f"Equity: `${equity_cents / 100:.2f}`")
        self.send("\n".join(lines))

    def notify_trade(self, opp, success, detail, contracts=1):
        mark = "EXECUTED" if success else "FAILED"
        lines = [
            f"*Trade {mark}*",
            f"Event: `{opp.event_ticker}`",
            f"Title: {_escape_md(getattr(opp, 'event_title', '?'))}",
            f"Type: `{opp.type}`",
            f"Contracts: `{contracts}`",
            f"Net profit: `{opp.net_profit_cents}c`",
            f"ROI: `{opp.roi_percent:.1f}%`",
            f"Detail: {_escape_md(detail)}",
        ]
        self.send("\n".join(lines))

    def notify_kill_switch(self, reason, equity_cents=None):
        lines = [
            "*KILL SWITCH ACTIVATED*",
            f"Reason: {_escape_md(reason)}",
        ]
        if equity_cents is not None:
            lines.append(f"Equity: `${equity_cents / 100:.2f}`")
        lines.append("All trading halted. Manual resume required.")
        self.send("\n".join(lines))

    def notify_partial_fill(self, opp, filled, total, unwound):
        lines = [
            "*PARTIAL FILL*",
            f"Event: `{opp.event_ticker}`",
            f"Filled: `{filled}/{total}` legs",
            f"Unwound: `{unwound}` legs",
            "Check your positions.",
        ]
        self.send("\n".join(lines))

    def notify_status(self, equity_cents, daily_pnl_cents, drawdown_pct,
                      exposure_cents, trades_today, open_positions):
        pnl_sign = "+" if daily_pnl_cents >= 0 else ""
        lines = [
            "*Hourly Status*",
            f"Equity: `${equity_cents / 100:.2f}`",
            f"Daily P&L: `{pnl_sign}{daily_pnl_cents}c`",
            f"Drawdown: `{drawdown_pct:.2f}%`",
            f"Exposure: `${exposure_cents / 100:.2f}`",
            f"Open positions: `{open_positions}`",
            f"Trades today: `{trades_today}`",
        ]
        self.send("\n".join(lines))

    def notify_scanner_error(self, error_message):
        # Deduped by content so repeated errors don't spam
        self.send(f"*Scanner Error*\n`{_escape_md(error_message)[:400]}`")

    def notify_shutdown(self):
        self.send("*Cadence stopping*")

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:  # sentinel
                break
            text, parse_mode = item
            self._send_now(text, parse_mode)

    def _send_now(self, text, parse_mode):
        """Actually hit Telegram's API. Handles retries and logs failures."""
        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")

        for attempt in range(self.MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT_SECS) as resp:
                    if 200 <= resp.status < 300:
                        self._sent_count += 1
                        return
            except urllib.error.HTTPError as e:
                # On 400-series, don't retry — likely a bad chat_id / token
                if 400 <= e.code < 500:
                    self._failed_count += 1
                    print(f"  [telegram] HTTP {e.code}: message dropped",
                          flush=True)
                    try:
                        body = e.read().decode("utf-8", errors="ignore")[:200]
                        print(f"  [telegram] body: {body}", flush=True)
                    except Exception:
                        pass
                    return
                # 5xx retry with backoff
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt == self.MAX_RETRIES - 1:
                    self._failed_count += 1
                    print(f"  [telegram] failed after {self.MAX_RETRIES} "
                          f"attempts: {e}", flush=True)
            # Backoff before next attempt
            time.sleep(2 ** attempt)

    # ------------------------------------------------------------------
    # Status for diagnostics
    # ------------------------------------------------------------------

    def get_stats(self):
        return {
            "enabled": self.enabled,
            "queued": self._queue.qsize() if self.enabled else 0,
            "sent": self._sent_count,
            "failed": self._failed_count,
            "dropped": self._dropped_count,
            "chat_id": (self.chat_id[:4] + "..." if self.chat_id else None),
        }


def _escape_md(s):
    """
    Telegram Markdown (v1) doesn't need much escaping — just strip
    characters that commonly break formatting inside our templates.
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    # Strip characters that collide with our markdown usage
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, "")
    return s


def build_from_env():
    """Construct a TelegramNotifier from environment variables."""
    token = os.environ.get("CADENCE_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CADENCE_TELEGRAM_CHAT_ID", "").strip()
    enabled_str = os.environ.get("CADENCE_TELEGRAM_ENABLED", "true").lower()
    enabled = enabled_str in ("true", "1", "yes", "on")
    if not token or not chat_id:
        return TelegramNotifier(enabled=False)
    return TelegramNotifier(bot_token=token, chat_id=chat_id, enabled=enabled)
