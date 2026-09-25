import numpy as np
import pandas as pd
from typing import List, Dict, Any, Tuple

def calc_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, adjust=False, min_periods=length).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False, min_periods=length).mean()
    ratio = gain / loss.replace(0, np.nan)
    res = 100 - (100 / (1 + ratio))
    res = res.mask((loss == 0) & (gain > 0), 100)
    res = res.mask((loss == 0) & (gain == 0), 50)
    return res

def calc_bollinger_bands(series: pd.Series, length: int = 20, num_std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    sma = series.rolling(length).mean()
    std = series.rolling(length).std(ddof=0)
    return sma + (num_std * std), sma, sma - (num_std * std)

def is_pivot(values: pd.Series, index: int, left: int = 10, right: int = 2, low: bool = True) -> bool:
    if index < left or index + right >= len(values): return False
    center = values.iloc[index]
    neighbors = values.iloc[index - left:index + right + 1].drop(values.index[index])
    if not np.isfinite(center) or not np.isfinite(neighbors.to_numpy(dtype=float)).all(): return False
    return bool(center < neighbors.min() if low else center > neighbors.max())

def analyze_rsi_turning(rsi: pd.Series) -> Dict[str, Any]:
    if len(rsi) < 3: return {"status": "neutral", "score": 0}
    r0, r1, r2 = rsi.iloc[-1], rsi.iloc[-2], rsi.iloc[-3]
    if r1 < 30 and r0 > r1 and r0 > r2:
        return {"status": "turning_up_from_oversold", "score": 20}
    if r1 > 70 and r0 < r1 and r0 < r2:
        return {"status": "turning_down_from_overbought", "score": -20}
    return {"status": "neutral", "score": 0}

def analyze_rsi_bollinger(rsi: pd.Series, upper: pd.Series, lower: pd.Series) -> Dict[str, Any]:
    if len(rsi) < 2: return {"status": "neutral", "score": 0}
    r0, r1 = rsi.iloc[-1], rsi.iloc[-2]
    u0, u1 = upper.iloc[-1], upper.iloc[-2]
    l0, l1 = lower.iloc[-1], lower.iloc[-2]
    
    if r1 < l1 and r0 >= l0:
        return {"status": "rsi_re_entry_bullish", "score": 15}
    if r1 > u1 and r0 <= u0:
        return {"status": "rsi_re_entry_bearish", "score": -15}
    return {"status": "neutral", "score": 0}

def analyze_price_bollinger(close: pd.Series, upper: pd.Series, lower: pd.Series) -> Dict[str, Any]:
    if len(close) < 2: return {"status": "neutral", "score": 0}
    c0, c1 = close.iloc[-1], close.iloc[-2]
    u0, u1 = upper.iloc[-1], upper.iloc[-2]
    l0, l1 = lower.iloc[-1], lower.iloc[-2]
    
    if c1 < l1 and c0 >= l0:
        return {"status": "price_re_entry_bullish", "score": 25}
    if c1 > u1 and c0 <= u0:
        return {"status": "price_re_entry_bearish", "score": -25}
    return {"status": "neutral", "score": 0}

def analyze_market_structure(high: pd.Series, low: pd.Series, atr: pd.Series) -> str:
    pivots_high = []
    pivots_low = []
    
    if len(atr) == 0 or np.isnan(atr.iloc[-1]):
        return "Ranging"
        
    atr_val = atr.iloc[-1]
    
    for i in range(len(high) - 3, max(0, len(high) - 30), -1):
        if is_pivot(high, i, 5, 2, low=False): 
            if not pivots_high or abs(high.iloc[i] - pivots_high[-1]) > atr_val * 0.1:
                pivots_high.append(high.iloc[i])
        if is_pivot(low, i, 5, 2, low=True): 
            if not pivots_low or abs(low.iloc[i] - pivots_low[-1]) > atr_val * 0.1:
                pivots_low.append(low.iloc[i])
        
    if len(pivots_high) >= 2 and len(pivots_low) >= 2:
        hh = pivots_high[0] > pivots_high[1]
        hl = pivots_low[0] > pivots_low[1]
        if hh and hl: return "Uptrend"
        if not hh and not hl: return "Downtrend"
    return "Ranging"

def detect_early_watch(df: pd.DataFrame, df_15m: pd.DataFrame = None) -> Dict[str, Any]:
    # Evaluate early watch condition (confluence without full confirmation)
    if len(df) < 50: return {"status": "none", "signals": []}
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    rsi = calc_rsi(close)
    
    confluence_score = 0
    signals = []
    
    # 1. RSI starting to turn but maybe not fully confirmed pivot
    if len(rsi) >= 3:
        if rsi.iloc[-2] < 30 and rsi.iloc[-1] > rsi.iloc[-2]:
            confluence_score += 1
            signals.append("rsi_turning_up")
        elif rsi.iloc[-2] > 70 and rsi.iloc[-1] < rsi.iloc[-2]:
            confluence_score += 1
            signals.append("rsi_turning_down")
            
    # 2. Approaching EMA20/50 (pullback developing)
    ema20 = close.ewm(span=20).mean()
    ema50 = close.ewm(span=50).mean()
    dist_20 = abs(close.iloc[-1] - ema20.iloc[-1]) / ema20.iloc[-1]
    if dist_20 < 0.002: # Very close to EMA20
        confluence_score += 1
        signals.append("approaching_ema20")
        
    # 3. Volume acceleration
    vol_sma = vol.rolling(20).mean()
    if vol.iloc[-1] > vol_sma.iloc[-1] * 1.5:
        confluence_score += 1
        signals.append("volume_acceleration")
        
    if confluence_score >= 2:
        return {"status": "EARLY WATCH", "signals": signals}
    return {"status": "none", "signals": []}

def calculate_technical_score(df: pd.DataFrame, df_15m: pd.DataFrame = None) -> Dict[str, Any]:
    if len(df) < 50: return {"total_score": 0, "signals": []}
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    rsi = calc_rsi(close)
    
    # ATR
    tr = pd.concat(((high - low), (high - close.shift()).abs(), (low - close.shift()).abs()), axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    
    p_up, p_mid, p_low = calc_bollinger_bands(close)
    r_up, r_mid, r_low = calc_bollinger_bands(rsi)
    
    tech_score = 0
    signals = []
    
    # RSI Turning
    rsi_turn = analyze_rsi_turning(rsi)
    tech_score += rsi_turn["score"]
    if rsi_turn["score"] != 0: signals.append(rsi_turn["status"])
    
    # RSI Bollinger
    rsi_bb = analyze_rsi_bollinger(rsi, r_up, r_low)
    tech_score += rsi_bb["score"]
    if rsi_bb["score"] != 0: signals.append(rsi_bb["status"])
        
    # Price Bollinger
    pb = analyze_price_bollinger(close, p_up, p_low)
    tech_score += pb["score"]
    if pb["score"] != 0: signals.append(pb["status"])
    
    mtf_alignment = "N/A"
    # Trend Alignment with 15m
    if df_15m is not None and not df_15m.empty:
        current_time = df.index[-1]
        valid_15m = df_15m[df_15m.index <= current_time]
        if not valid_15m.empty:
            c_15m = valid_15m["Close"]
            ema50_15m = c_15m.ewm(span=50).mean()
            ema200_15m = c_15m.ewm(span=200).mean()
            if not np.isnan(ema50_15m.iloc[-1]) and not np.isnan(ema200_15m.iloc[-1]):
                if ema50_15m.iloc[-1] > ema200_15m.iloc[-1]: 
                    tech_score += 10
                    mtf_alignment = "Bullish"
                else: 
                    tech_score -= 10
                    mtf_alignment = "Bearish"
    else:
        ema50 = close.ewm(span=50).mean()
        ema200 = close.ewm(span=200).mean()
        if ema50.iloc[-1] > ema200.iloc[-1]: 
            tech_score += 10
            mtf_alignment = "Bullish (5m fallback)"
        else: 
            tech_score -= 10
            mtf_alignment = "Bearish (5m fallback)"
    
    # Volume spike and relative volume
    vol_sma = vol.rolling(20).mean()
    relative_volume = vol.iloc[-1] / vol_sma.iloc[-1] if vol_sma.iloc[-1] > 0 else 0
    if relative_volume > 2:
        tech_score += 5 if close.iloc[-1] >= close.iloc[-2] else -5
        signals.append("volume_spike")
        
    ms = analyze_market_structure(high, low, atr)
    
    atr_val = atr.iloc[-1]
    atr_percent = (atr_val / close.iloc[-1]) * 100 if close.iloc[-1] > 0 else 0
    
    range_position = "N/A"
    if len(close) > 20:
        recent_high = high.rolling(20).max().iloc[-1]
        recent_low = low.rolling(20).min().iloc[-1]
        if recent_high > recent_low:
            range_position = f"{((close.iloc[-1] - recent_low) / (recent_high - recent_low) * 100):.1f}%"
    
    momentum = "N/A"
    if len(close) > 10:
        roc = (close.iloc[-1] - close.iloc[-10]) / close.iloc[-10] * 100
        momentum = f"{roc:.2f}%"
    
    return {
        "technical_score": tech_score,
        "signals": signals,
        "rsi": rsi.iloc[-1],
        "market_structure": ms,
        "mtf_alignment": mtf_alignment,
        "relative_volume": f"{relative_volume:.2f}x",
        "atr_percent": f"{atr_percent:.2f}%",
        "range_position": range_position,
        "momentum": momentum
    }
