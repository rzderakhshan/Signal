"""Deployment-focused tests: state persistence, dedup, and production workflow config."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from activity_monitor import ActivityMonitor
from scanner import prepare_runtime_state, restore_alert_history, restore_hot_watchlist


WORKFLOW = Path(".github/workflows/scanner.yml")
LEGACY_WORKFLOW = Path(".github/workflows/scan.yml")


def test_prepare_runtime_state_preserves_alert_history():
    now = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
    raw = {
        "alert_history": {
            "BTC-EUR": {
                "time": now.timestamp() - 300,
                "signals": ["EARLY_WATCH", "volume_spike"],
            }
        },
        "hot_watchlist": {"BTC-EUR": now.timestamp() + 1800},
        "last_30m_scan": now.timestamp() - 600,
        "report|old": now.timestamp() - 8 * 86400,
        "report|fresh": now.timestamp() - 3600,
    }
    prepared = prepare_runtime_state(raw, now)
    assert "BTC-EUR" in prepared["alert_history"]
    assert prepared["alert_history"]["BTC-EUR"]["signals"] == ["EARLY_WATCH", "volume_spike"]
    assert prepared["hot_watchlist"]["BTC-EUR"] == now.timestamp() + 1800
    assert prepared["last_30m_scan"] == now.timestamp() - 600
    assert "report|old" not in prepared
    assert prepared["report|fresh"] == now.timestamp() - 3600


def test_restore_alert_history_skips_corrupt_entries():
    now = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
    raw = {
        "GOOD": {"time": now.timestamp(), "signals": ["CONFIRMED_SETUP"]},
        "BAD_TYPE": "not-a-dict",
        "BAD_MISSING": {"signals": ["EARLY_WATCH"]},
        "BAD_TIME": {"time": "nope", "signals": ["EARLY_WATCH"]},
    }
    restored = restore_alert_history(raw)
    assert set(restored) == {"GOOD"}
    assert restored["GOOD"]["signals"] == ["CONFIRMED_SETUP"]
    assert restore_alert_history(None) == {}
    assert restore_hot_watchlist({"X": "bad"}, now.timestamp()) == {}


def test_identical_early_watch_dedup():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    early = {"signals": ["EARLY_WATCH", "volume_spike", "rsi_re_entry_bullish"]}
    assert monitor.should_send_alert("ETH-EUR", early, now) is True
    assert monitor.should_send_alert("ETH-EUR", early, now + timedelta(minutes=5)) is False


def test_identical_confirmed_setup_dedup():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    confirmed = {"signals": ["CONFIRMED_SETUP", "volume_spike", "rsi_turn_bullish"]}
    assert monitor.should_send_alert("ETH-EUR", confirmed, now) is True
    assert monitor.should_send_alert("ETH-EUR", confirmed, now + timedelta(minutes=5)) is False


def test_early_to_confirmed_allowed():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    early = {"signals": ["EARLY_WATCH", "volume_spike", "rsi_re_entry_bullish"]}
    confirmed = {"signals": ["CONFIRMED_SETUP", "volume_spike", "rsi_re_entry_bullish"]}
    assert monitor.should_send_alert("SOL-EUR", early, now) is True
    assert monitor.should_send_alert("SOL-EUR", confirmed, now + timedelta(minutes=5)) is True


def test_persisted_history_blocks_identical_alert_after_restore():
    now = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
    saved = {
        "alert_history": {
            "ADA-EUR": {
                "time": (now - timedelta(minutes=2)).timestamp(),
                "signals": ["EARLY_WATCH", "volume_spike"],
            }
        },
        "hot_watchlist": {},
    }
    prepared = prepare_runtime_state(saved, now)
    monitor = ActivityMonitor()
    monitor.alert_history = restore_alert_history(prepared["alert_history"])
    identical = {"signals": ["EARLY_WATCH", "volume_spike"]}
    assert monitor.should_send_alert("ADA-EUR", identical, now) is False
    upgraded = {"signals": ["CONFIRMED_SETUP", "volume_spike"]}
    assert monitor.should_send_alert("ADA-EUR", upgraded, now) is True


def test_production_workflow_is_live_and_singular():
    assert WORKFLOW.exists()
    assert not LEGACY_WORKFLOW.exists(), "obsolete .github/workflows/scan.yml must be removed"

    text = WORKFLOW.read_text(encoding="utf-8")
    assert "cron: '*/5 * * * *'" in text
    assert "python scanner.py --github-dry-run" not in text
    assert "python scanner.py --test-telegram" in text
    assert "name: Run Scanner" in text
    assert "name: Run Scanner (Dry Run Mode)" not in text
    assert "test_telegram:" in text
    assert "path: .state/" in text
    assert "name: Download State (Cache)" in text
    assert "name: Save State (Cache)" in text

    # Scheduled path must run live production (not only the test-telegram branch).
    assert "else\n            python scanner.py\n          fi" in text.replace("\r\n", "\n")


def test_high_activity_path_is_internal_only_in_scanner_source():
    source = Path("scanner.py").read_text(encoding="utf-8")
    assert "HIGH_ACTIVITY_INTERNAL=" in source
    assert "send_telegram(token, chat_id, format_high_activity_message" not in source


def test_telegram_429_retries_once_and_respects_retry_after(monkeypatch):
    import telegram_utils
    first = MagicMock(status_code=429)
    first.json.return_value = {"parameters": {"retry_after": 2}}
    second = MagicMock(status_code=200)
    post = MagicMock(side_effect=[first, second])
    sleep = MagicMock()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setattr(telegram_utils.requests, "post", post)
    monkeypatch.setattr(telegram_utils.time, "sleep", sleep)
    assert telegram_utils.send_telegram_message("test") is True
    assert post.call_count == 2
    sleep.assert_called_once_with(2)


def test_telegram_429_does_not_retry_more_than_once(monkeypatch):
    import telegram_utils
    limited1 = MagicMock(status_code=429)
    limited1.json.return_value = {"parameters": {"retry_after": 1}}
    limited2 = MagicMock(status_code=429)
    limited2.json.return_value = {"parameters": {"retry_after": 1}}
    post = MagicMock(side_effect=[limited1, limited2])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setattr(telegram_utils.requests, "post", post)
    monkeypatch.setattr(telegram_utils.time, "sleep", MagicMock())
    assert telegram_utils.send_telegram_message("test") is False
    assert post.call_count == 2
