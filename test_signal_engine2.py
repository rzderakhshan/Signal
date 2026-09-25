import pytest
import pandas as pd
import numpy as np
from signal_engine import analyze_rsi_turning, analyze_price_bollinger, analyze_rsi_bollinger, analyze_market_structure, calculate_technical_score

def test_rsi_bollinger_reentry():
    rsi = pd.Series([50, 40, 55])
    lower = pd.Series([45, 45, 45])
    upper = pd.Series([70, 70, 70])
    res = analyze_rsi_bollinger(rsi, upper, lower)
    assert res["status"] == "rsi_re_entry_bullish"
    assert res["score"] == 15

def test_market_structure_atr_filter():
    high = pd.Series(np.linspace(100, 110, 40))
    low = pd.Series(np.linspace(90, 100, 40))
    
    # Make pivots but with tiny differences, below ATR
    atr = pd.Series([2.0] * 40)
    high[20] = 105.1
    high[30] = 105.15 # difference is 0.05, ATR*0.1 = 0.2. Should be filtered.
    
    res = analyze_market_structure(high, low, atr)
    # Ranging because pivots are filtered out
    assert res == "Ranging"

def test_multi_timeframe_alignment():
    # 5m df
    index_5m = pd.date_range("2026-09-25 10:00", periods=60, freq="5min")
    df_5m = pd.DataFrame({"Close": np.linspace(100, 120, 60), "High": np.linspace(101, 121, 60), "Low": np.linspace(99, 119, 60), "Volume": np.ones(60)*1000}, index=index_5m)
    
    # 15m df (Uptrend)
    index_15m = pd.date_range("2026-09-25 08:00", periods=20, freq="15min")
    df_15m = pd.DataFrame({"Close": np.linspace(50, 200, 20)}, index=index_15m)
    
    res = calculate_technical_score(df_5m, df_15m)
    # The 15m ema50 > ema200 condition should give +10
    # Wait, 20 periods is not enough for ema50/200. Let's make it 250 periods.
    index_15m = pd.date_range("2026-09-20 08:00", periods=250, freq="15min")
    df_15m = pd.DataFrame({"Close": np.linspace(50, 200, 250)}, index=index_15m)
    
    res = calculate_technical_score(df_5m, df_15m)
    # Uptrend in 15m -> but no directional signal, so trend_score is 0
    assert res["technical_score"] >= 0
    assert res["mtf_alignment"] == "ALIGNED_BULLISH"
