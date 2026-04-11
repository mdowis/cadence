"""Tests for the Telegram notifier (no network calls)."""

import time
import urllib.error
from unittest.mock import patch, MagicMock

from notifier import TelegramNotifier, _escape_md, build_from_env
from kalshi_arbitrage import ArbitrageOpportunity


def _make_opp():
    return ArbitrageOpportunity(
        type="binary",
        event_title="Bitcoin above 100K",
        event_ticker="KXBTC-100K",
        markets=[{"ticker": "T1", "title": "t", "yes_ask": 45, "no_ask": 50}],
        total_cost=95,
        guaranteed_payout=100,
        profit_cents=5,
        roi_percent=5.3,
        fee_cents=2,
        net_profit_cents=3,
    )


# ---- Disabled path ----

def test_disabled_when_no_token():
    n = TelegramNotifier(bot_token=None, chat_id="123")
    assert not n.enabled
    assert n.send("hello") is False


def test_disabled_when_no_chat_id():
    n = TelegramNotifier(bot_token="x", chat_id=None)
    assert not n.enabled


def test_disabled_flag_overrides():
    n = TelegramNotifier(bot_token="x", chat_id="y", enabled=False)
    assert not n.enabled


def test_build_from_env_no_creds(monkeypatch_env):
    n = build_from_env()
    assert not n.enabled


# ---- Enabled path with mocked urlopen ----

class _MockResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_queues_and_delivers():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _MockResponse(status=200)
        n = TelegramNotifier(bot_token="tok", chat_id="42")
        try:
            assert n.send("hello world") is True
            # Wait for worker to process
            for _ in range(50):
                if mock_urlopen.called:
                    break
                time.sleep(0.05)
            assert mock_urlopen.called
            # Verify the call shape
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert "/bot tok".replace(" ", "") in req.full_url or "bottok" in req.full_url
            assert req.method == "POST"
        finally:
            n.stop()


def test_dedup_suppresses_duplicates():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _MockResponse(status=200)
        n = TelegramNotifier(bot_token="tok", chat_id="42")
        try:
            assert n.send("same text") is True
            assert n.send("same text") is False  # deduped
            assert n.send("same text") is False
            # Only one should have been queued
            time.sleep(0.2)
            assert mock_urlopen.call_count == 1
        finally:
            n.stop()


def test_notify_trade_format():
    """notify_trade should produce a well-formed message with the key fields."""
    sent = []
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _MockResponse(status=200)

        def capture(req, timeout=None):
            sent.append(req)
            return _MockResponse(200)

        mock_urlopen.side_effect = capture
        n = TelegramNotifier(bot_token="tok", chat_id="42")
        try:
            opp = _make_opp()
            n.notify_trade(opp, success=True, detail="Placed 2/2 orders")
            for _ in range(50):
                if sent:
                    break
                time.sleep(0.05)
            assert len(sent) == 1
            body = sent[0].data.decode()
            assert "EXECUTED" in body
            assert "KXBTC-100K" in body
            assert "Placed" in body
        finally:
            n.stop()


def test_notify_kill_switch_format():
    sent_bodies = []
    with patch("urllib.request.urlopen") as mock_urlopen:
        def capture(req, timeout=None):
            sent_bodies.append(req.data.decode())
            return _MockResponse(200)
        mock_urlopen.side_effect = capture
        n = TelegramNotifier(bot_token="tok", chat_id="42")
        try:
            n.notify_kill_switch("Drawdown 12%", equity_cents=8800)
            for _ in range(50):
                if sent_bodies:
                    break
                time.sleep(0.05)
            assert "KILL" in sent_bodies[0]
            assert "Drawdown" in sent_bodies[0]
        finally:
            n.stop()


def test_http_400_does_not_retry_forever():
    """Bad token / chat_id should fail fast, not loop."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        err = urllib.error.HTTPError(
            "url", 400, "Bad Request", {}, MagicMock(read=lambda: b"bad")
        )
        mock_urlopen.side_effect = err
        n = TelegramNotifier(bot_token="tok", chat_id="42")
        try:
            n.send("test")
            time.sleep(0.3)
            # Should have been called ONCE (not retried on 4xx)
            assert mock_urlopen.call_count == 1
            assert n._failed_count == 1
        finally:
            n.stop()


def test_escape_md_strips_special_chars():
    assert _escape_md("hello *world*") == "hello world"
    assert _escape_md("a_b_c") == "abc"
    assert _escape_md("`code`") == "code"
    assert _escape_md(None) == ""
    assert _escape_md(42) == "42"


def test_stats_reporting():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _MockResponse(200)
        n = TelegramNotifier(bot_token="tok", chat_id="4242")
        try:
            stats = n.get_stats()
            assert stats["enabled"] is True
            assert stats["chat_id"].startswith("4242")  # truncated
            assert stats["sent"] == 0
            n.send("first")
            time.sleep(0.2)
            stats = n.get_stats()
            assert stats["sent"] >= 1
        finally:
            n.stop()


def monkeypatch_env(*args, **kwargs):
    """Dummy to satisfy test_build_from_env_no_creds signature."""
    return None


# ---- Command handling ----

def _quiet_notifier_send(notifier):
    """Replace _send_now so the outbound queue never hits real urlopen."""
    notifier._send_now = lambda text, parse_mode: None


def _make_authorized_message(text, chat_id="42", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


def test_command_disabled_by_default():
    """commands_enabled must be explicit opt-in."""
    n = TelegramNotifier(bot_token="tok", chat_id="42")
    try:
        assert n.commands_enabled is False
    finally:
        n.stop()


def test_register_command():
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    try:
        n.register_command("ping", lambda args: "pong", "reply with pong")
        assert "ping" in n.registered_commands()
        assert n.registered_commands()["ping"] == "reply with pong"
    finally:
        n.stop()


def test_handle_update_dispatches_command():
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    _quiet_notifier_send(n)
    try:
        calls = []
        n.register_command("status", lambda args: calls.append(args) or "ok")
        n._handle_update(_make_authorized_message("/status"))
        assert calls == [[]]
    finally:
        n.stop()


def test_handle_update_passes_args():
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    _quiet_notifier_send(n)
    try:
        captured = []
        n.register_command("echo", lambda args: captured.append(args) or " ".join(args))
        n._handle_update(_make_authorized_message("/echo hello world"))
        assert captured == [["hello", "world"]]
    finally:
        n.stop()


def test_handle_update_strips_bot_mention():
    """/cmd@BotName args should dispatch to 'cmd'."""
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    _quiet_notifier_send(n)
    try:
        fired = []
        n.register_command("status", lambda args: fired.append(True))
        n._handle_update(_make_authorized_message("/status@CadenceBot"))
        assert fired == [True]
    finally:
        n.stop()


def test_unauthorized_chat_silently_dropped():
    """Messages from the wrong chat id must not dispatch commands."""
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    _quiet_notifier_send(n)
    try:
        fired = []
        n.register_command("kill", lambda args: fired.append(True))
        # Attacker tries from a different chat
        n._handle_update(_make_authorized_message("/kill", chat_id="999"))
        assert fired == []
    finally:
        n.stop()


def test_unknown_command_replies_help_hint():
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    sends = []
    n.send = lambda text, **kw: sends.append(text)
    try:
        n._handle_update(_make_authorized_message("/doesnotexist"))
        assert any("Unknown command" in s for s in sends)
    finally:
        n.stop()


def test_non_slash_message_ignored():
    """Plain text (not CONFIRM) should be ignored silently."""
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    sends = []
    n.send = lambda text, **kw: sends.append(text)
    fired = []
    n.register_command("status", lambda args: fired.append(True))
    try:
        n._handle_update(_make_authorized_message("hello there"))
        assert fired == []
        assert sends == []
    finally:
        n.stop()


def test_confirmation_flow():
    """Two-step confirmation: first /cmd, then CONFIRM reply."""
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    _quiet_notifier_send(n)
    try:
        executed = []

        def handler(args):
            if "__confirmed__" in args:
                executed.append("yes")
                return "LIVE trading started."
            return n.register_confirmation("exec_live")

        n.register_command("exec_live", handler, "start live trading")

        # Step 1: /exec_live → pending
        n._handle_update(_make_authorized_message("/exec_live", update_id=10))
        assert executed == []
        assert "exec_live" in n._pending_confirmations

        # Step 2: CONFIRM → executes
        n._handle_update(_make_authorized_message("CONFIRM", update_id=11))
        assert executed == ["yes"]
        assert "exec_live" not in n._pending_confirmations
    finally:
        n.stop()


def test_confirmation_expires():
    """Stale confirmations are cleaned up, not executed."""
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    _quiet_notifier_send(n)
    try:
        executed = []
        n.register_command(
            "exec_live",
            lambda args: executed.append(1) if "__confirmed__" in args
                         else n.register_confirmation("exec_live"),
        )
        n._handle_update(_make_authorized_message("/exec_live", update_id=20))
        # Force the pending timestamp into the past
        ts, args = n._pending_confirmations["exec_live"]
        n._pending_confirmations["exec_live"] = (
            ts - n.CONFIRMATION_TTL_SECS - 1, args,
        )
        # Now the CONFIRM should NOT execute anything
        sends = []
        n.send = lambda text, **kw: sends.append(text)
        n._handle_update(_make_authorized_message("CONFIRM", update_id=21))
        assert executed == []
        assert any("expired" in s.lower() or "nothing" in s.lower() for s in sends)
    finally:
        n.stop()


def test_command_exception_is_caught_and_reported():
    n = TelegramNotifier(
        bot_token="tok", chat_id="42", commands_enabled=True,
    )
    sends = []
    n.send = lambda text, **kw: sends.append(text)
    try:
        def broken(args):
            raise ValueError("boom")
        n.register_command("broken", broken)
        n._handle_update(_make_authorized_message("/broken"))
        assert any("failed" in s.lower() and "boom" in s for s in sends)
    finally:
        n.stop()


if __name__ == "__main__":
    import os
    # Make sure env isn't set so build_from_env returns disabled
    for k in ("CADENCE_TELEGRAM_BOT_TOKEN", "CADENCE_TELEGRAM_CHAT_ID"):
        os.environ.pop(k, None)

    test_disabled_when_no_token()
    test_disabled_when_no_chat_id()
    test_disabled_flag_overrides()
    test_build_from_env_no_creds(None)
    test_send_queues_and_delivers()
    test_dedup_suppresses_duplicates()
    test_notify_trade_format()
    test_notify_kill_switch_format()
    test_http_400_does_not_retry_forever()
    test_escape_md_strips_special_chars()
    test_stats_reporting()
    test_command_disabled_by_default()
    test_register_command()
    test_handle_update_dispatches_command()
    test_handle_update_passes_args()
    test_handle_update_strips_bot_mention()
    test_unauthorized_chat_silently_dropped()
    test_unknown_command_replies_help_hint()
    test_non_slash_message_ignored()
    test_confirmation_flow()
    test_confirmation_expires()
    test_command_exception_is_caught_and_reported()
    print("All 22 notifier tests passed!")
