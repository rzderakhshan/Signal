import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from scanner import closed_bars, divergence, wilder_rsi

def test_closed_bars_discards_unfinished():
    now = datetime(2026, 9, 25, 20, 0, 0, tzinfo=timezone.utc)
    # 5m interval
    # Candle 19:50 started at 19:50, closed at 19:55
    # Candle 19:55 started at 19:55, closes at 20:00 -> not closed yet (since cutoff is now - 5m - 90s)
    index = pd.DatetimeIndex([
        now - timedelta(minutes=10),
        now - timedelta(minutes=5)
    ], tz="UTC")
    df = pd.DataFrame({"Close": [100, 101]}, index=index)
    
    closed = closed_bars(df, 5, now)
    assert len(closed) == 1
    assert closed.index[0] == index[0]

def test_divergence_no_lookahead():
    close = np.linspace(100, 80, 40)
    rsi = np.linspace(50, 30, 40)
    
    # First pivot at index 12
    close[12] = 85
    rsi[12] = 20
    
    # Second pivot at index 25
    close[25] = 80
    rsi[25] = 30
    
    df = pd.DataFrame({"Low": close, "High": close + 2})
    rsi_series = pd.Series(rsi)
    
    # `divergence` uses current = len(df) - 3. To make current=25, len must be 28.
    df_sub = df.iloc[:28].copy()
    rsi_sub = rsi_series.iloc[:28].copy()
    
    signals = divergence(df_sub, rsi_sub)
    assert len(signals) == 1
    assert signals[0][0] == "BUY WATCH"
    assert "واگرایی" in signals[0][1]

def test_stale_data_handling():
    now = datetime(2026, 9, 25, 20, 0, 0, tzinfo=timezone.utc)
    # Very old candle
    index = pd.DatetimeIndex([now - timedelta(hours=2)], tz="UTC")
    df = pd.DataFrame({"Close": [100]}, index=index)
    
    closed = closed_bars(df, 5, now)
    # It still returns it, but detect() will filter it
    # We just check it doesn't crash
    assert len(closed) == 1
