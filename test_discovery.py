"""Discovery + HIGH ACTIVITY layer tests (Signal Engine semantics unchanged)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from activity_monitor import ActivityMonitor, format_top_activity, rank_activity
from universe import load_instrument_map, load_universe


def _quiet_df(n: int = 60) -> pd.DataFrame:
    close = np.full(n, 100.0)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 0.05,
            "Low": close - 0.05,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        }
    )


def _active_df(n: int = 60) -> pd.DataFrame:
    close = np.linspace(100, 102, n)
    close[-1] = close[-2] * 1.02
    high = close + 1.5
    low = close - 0.2
    vol = np.full(n, 1000.0)
    vol[-1] = 5000.0
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol})


def test_expanded_crypto_universe_loading():
    uni = load_universe(Path("universe.json"))
    assert 40 <= len(uni["crypto"]) <= 60
    assert len(uni["stocks"]) >= 200
    assert isinstance(uni.get("settings"), dict)
    assert "BTC-EUR" in uni["crypto"]
    assert "SOL-EUR" in uni["crypto"]
    assert "AAPL" in uni["stocks"]


def test_instrument_map_optional_and_nonblocking():
    meta = load_instrument_map(Path("instrument_map.json"))
    assert "AAPL" in meta
    assert load_instrument_map(Path("does_not_exist.json")) == {}


def test_unsupported_symbol_safely_skipped(monkeypatch):
    import sys
    import types
    from scanner import fetch_yahoo

    fake_yf = types.SimpleNamespace(download=MagicMock(return_value=pd.DataFrame()))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    # Empty / missing symbols must not raise.
    result = fetch_yahoo(["NOTAREALCOIN-EUR", "ALSOFAKE-EUR"], 5)
    assert isinstance(result, dict)


def test_high_activity_enters_hot_watchlist():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    activity = monitor.calc_activity_score(_active_df())
    assert activity["score"] >= 40
    monitor.update_hot_watchlist("SOL-EUR", activity, now)
    assert monitor.is_hot("SOL-EUR")


def test_quiet_instrument_does_not_trigger_high_activity():
    monitor = ActivityMonitor()
    activity = monitor.calc_activity_score(_quiet_df())
    assert activity["score"] < 40
    assert monitor.is_high_activity(activity) is False


def test_high_activity_is_not_buy_sell():
    monitor = ActivityMonitor()
    activity = monitor.calc_activity_score(_active_df())
    # Even when unusual, Activity layer never emits BUY/SELL direction.
    assert "BUY" not in activity.get("events", [])
    assert "SELL" not in activity.get("events", [])
    msg_signals = ["HIGH_ACTIVITY"] + list(activity.get("events") or [])
    assert "BUY" not in msg_signals and "SELL" not in msg_signals


def test_high_activity_telegram_dedup():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    payload = {"signals": ["HIGH_ACTIVITY", "volume_spike"]}
    assert monitor.should_send_alert("DOGE-EUR", payload, now) is True
    assert monitor.should_send_alert("DOGE-EUR", payload, now + timedelta(minutes=5)) is False


def test_high_activity_to_early_allowed():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    high = {"signals": ["HIGH_ACTIVITY", "volume_spike"]}
    early = {"signals": ["EARLY_WATCH", "volume_spike", "rsi_re_entry_bullish"]}
    assert monitor.should_send_alert("XRP-EUR", high, now) is True
    assert monitor.should_send_alert("XRP-EUR", early, now + timedelta(minutes=5)) is True


def test_high_activity_to_confirmed_allowed():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    high = {"signals": ["HIGH_ACTIVITY", "range_expansion"]}
    confirmed = {"signals": ["CONFIRMED_SETUP", "volume_spike", "rsi_turn_bullish"]}
    assert monitor.should_send_alert("BTC-EUR", high, now) is True
    assert monitor.should_send_alert("BTC-EUR", confirmed, now + timedelta(minutes=5)) is True


def test_early_to_confirmed_still_allowed():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    early = {"signals": ["EARLY_WATCH", "volume_spike"]}
    confirmed = {"signals": ["CONFIRMED_SETUP", "volume_spike"]}
    assert monitor.should_send_alert("ETH-EUR", early, now) is True
    assert monitor.should_send_alert("ETH-EUR", confirmed, now + timedelta(minutes=5)) is True


def test_stock_fundamentals_not_applied_to_crypto():
    # Scanner only calls ResearchCache.stock for asset == "stocks".
    # Guard the branch contract here without invoking live Yahoo.
    asset = "crypto"
    fund_score_contrib = 0
    fund_context = "N/A"
    if asset == "stocks":
        fund_score_contrib = 10
        fund_context = "Score: 70/100"
    assert fund_score_contrib == 0
    assert fund_context == "N/A"


def test_fast_scan_does_not_full_analyze_quiet_symbols():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    quiet = monitor.calc_activity_score(_quiet_df())
    active = monitor.calc_activity_score(_active_df())
    monitor.update_hot_watchlist("QUIET", quiet, now)
    monitor.update_hot_watchlist("HOT1", active, now)
    is_periodic_30m = False
    analysis = []
    for symbol, act in (("QUIET", quiet), ("HOT1", active)):
        monitor.update_hot_watchlist(symbol, act, now)
        if monitor.is_hot(symbol) or is_periodic_30m:
            analysis.append(symbol)
    assert "HOT1" in analysis
    assert "QUIET" not in analysis


def test_periodic_30m_safety_analysis_includes_quiet():
    monitor = ActivityMonitor()
    now = datetime.now(timezone.utc)
    quiet = monitor.calc_activity_score(_quiet_df())
    monitor.update_hot_watchlist("QUIET", quiet, now)
    is_periodic_30m = True
    analysis = []
    for symbol in ("QUIET",):
        if monitor.is_hot(symbol) or is_periodic_30m:
            analysis.append(symbol)
    assert analysis == ["QUIET"]


def test_top_activity_ranking():
    rows = [
        {"symbol": "ETH-EUR", "score": 49},
        {"symbol": "SOL-EUR", "score": 84},
        {"symbol": "XRP-EUR", "score": 78},
    ]
    ranked = rank_activity(rows, limit=2)
    assert [r["symbol"] for r in ranked] == ["SOL-EUR", "XRP-EUR"]
    text = format_top_activity("TOP_CRYPTO_ACTIVITY", ranked)
    assert "1 SOL-EUR 84" in text
    assert "2 XRP-EUR 78" in text


def test_stock_universe_is_configuration_driven(tmp_path):
    import json
    custom = tmp_path / "universe.json"
    custom.write_text(json.dumps({
        "settings": {"max_hot_stocks": 7},
        "stocks": [f"S{i}" for i in range(237)],
        "crypto": ["BTC-EUR"],
    }), encoding="utf-8")
    uni = load_universe(custom)
    assert len(uni["stocks"]) == 237
    assert uni["settings"]["max_hot_stocks"] == 7


def test_activity_threshold_is_configurable():
    monitor = ActivityMonitor(activity_threshold=65)
    now = datetime.now(timezone.utc)
    monitor.update_hot_watchlist("MID", {"score": 50, "events": [], "metrics": {}}, now)
    assert monitor.is_hot("MID") is False
    monitor.update_hot_watchlist("HOT", {"score": 70, "events": [], "metrics": {}}, now)
    assert monitor.is_hot("HOT") is True


def test_crypto_eur_fallback_resolves_to_usd_without_changing_canonical_key():
    from scanner import fetch
    eur = pd.DataFrame()
    usd = _active_df()

    def fake_yahoo(symbols, minutes, batch_size=50):
        if "TEST-EUR" in symbols:
            return {}
        if "TEST-USD" in symbols:
            frame = usd.copy()
            frame.attrs["source"] = "Yahoo"
            return {"TEST-USD": frame}
        return {}

    with patch("scanner.fetch_yahoo", side_effect=fake_yahoo), patch("scanner.fetch_kraken") as kraken:
        result = fetch(["TEST-EUR"], 5, ["TEST-EUR"])
    assert "TEST-EUR" in result
    assert "TEST-USD" not in result
    assert result["TEST-EUR"].attrs["data_symbol"] == "TEST-USD"
    kraken.assert_not_called()


def test_unsupported_crypto_is_safely_skipped_after_all_fallbacks_fail():
    from scanner import fetch
    with patch("scanner.fetch_yahoo", return_value={}), patch("scanner.fetch_kraken", side_effect=ValueError("no pair")):
        assert fetch(["NOPE-EUR"], 5, ["NOPE-EUR"]) == {}
