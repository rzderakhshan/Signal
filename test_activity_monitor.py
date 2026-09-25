import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from activity_monitor import ActivityMonitor

def test_activity_score():
    monitor = ActivityMonitor()
    # Create fake 5m data with volume spike
    close = np.linspace(100, 105, 20)
    high = close + 1
    low = close - 1
    vol = np.ones(20) * 1000
    vol[19] = 5000 # volume spike
    
    df = pd.DataFrame({"Close": close, "High": high, "Low": low, "Volume": vol})
    res = monitor.calc_activity_score(df)
    
    assert res["score"] > 0
    assert "volume_spike" in res["events"]

def test_hot_watchlist():
    monitor = ActivityMonitor()
    now = datetime.now()
    
    # Should be added
    monitor.update_hot_watchlist("TSLA", {"score": 50, "events": ["volume_spike"]}, now)
    assert monitor.is_hot("TSLA")
    
    # Fast forward 61 minutes, should expire
    monitor.update_hot_watchlist("XYZ", {"score": 0, "events": []}, now + timedelta(minutes=61))
    assert not monitor.is_hot("TSLA")

def test_alert_gating():
    monitor = ActivityMonitor()
    now = datetime.now()
    
    signal1 = {"signals": ["volume_spike"]}
    signal2 = {"signals": ["volume_spike", "rsi_re_entry_bullish"]}
    
    # First time should pass
    assert monitor.should_send_alert("TSLA", signal1, now) == True
    
    # Second time with same signal within cooldown should fail
    assert monitor.should_send_alert("TSLA", signal1, now + timedelta(minutes=10)) == False
    
    # Second time with NEW event within cooldown should PASS (event-aware)
    assert monitor.should_send_alert("TSLA", signal2, now + timedelta(minutes=15)) == True
    
    # After cooldown should pass even if same
    assert monitor.should_send_alert("TSLA", signal2, now + timedelta(minutes=60)) == True

def test_stage_aware_dedup():
    monitor = ActivityMonitor()
    now = datetime.now()
    
    # EARLY_WATCH alert
    early_signal = {"signals": ["EARLY_WATCH", "volume_spike", "rsi_re_entry_bullish"]}
    assert monitor.should_send_alert("AAPL", early_signal, now) == True
    
    # Same EARLY_WATCH should be blocked
    assert monitor.should_send_alert("AAPL", early_signal, now + timedelta(minutes=5)) == False
    
    # CONFIRMED_SETUP with same underlying signals but new stage
    confirmed_signal = {"signals": ["CONFIRMED_SETUP", "volume_spike", "rsi_re_entry_bullish"]}
    assert monitor.should_send_alert("AAPL", confirmed_signal, now + timedelta(minutes=10)) == True
