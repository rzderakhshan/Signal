import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

class ActivityMonitor:
    def __init__(self):
        self.hot_watchlist = {}
        self.alert_history = {}
        self.cooldown_minutes = 30

    def calc_activity_score(self, df_5m: pd.DataFrame) -> Dict[str, Any]:
        """Calculates Activity Score based on recent volatility, volume, and momentum."""
        if len(df_5m) < 20:
            return {"score": 0, "events": []}
            
        close, high, low, vol = df_5m["Close"], df_5m["High"], df_5m["Low"], df_5m["Volume"]
        
        score = 0
        events = []
        
        # 1. Price change
        pct_change = abs(close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100
        if pct_change > 0.5:
            score += 20
            events.append("price_acceleration")
            
        # 2. Volume spike
        vol_sma = vol.rolling(20).mean()
        if vol.iloc[-1] > vol_sma.iloc[-1] * 2:
            score += 30
            events.append("volume_spike")
            
        # 3. ATR Expansion
        tr = pd.concat(((high - low), (high - close.shift()).abs(), (low - close.shift()).abs()), axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        if tr.iloc[-1] > atr.iloc[-1] * 1.5:
            score += 25
            events.append("range_expansion")
            
        score = min(100, score)
        return {"score": score, "events": events}

    def update_hot_watchlist(self, symbol: str, activity: Dict[str, Any], current_time: datetime):
        """Adds or refreshes symbols in the hot watchlist based on activity."""
        if activity["score"] >= 40 or len(activity["events"]) > 0:
            self.hot_watchlist[symbol] = current_time + timedelta(minutes=60)
            
        # Clean up expired
        expired = [sym for sym, exp in self.hot_watchlist.items() if current_time > exp]
        for sym in expired:
            del self.hot_watchlist[sym]

    def is_hot(self, symbol: str) -> bool:
        return symbol in self.hot_watchlist

    def should_send_alert(self, symbol: str, signal: Dict[str, Any], current_time: datetime) -> bool:
        """Gating mechanism: deduplication and cooldowns."""
        if not signal.get("signals"):
            return False
            
        fingerprint_key = f"{symbol}"
        
        last_alert = self.alert_history.get(fingerprint_key)
        
        # Event-aware dedup: If the new signal contains an event NOT present in the last alert, allow it
        new_events = set(signal.get('signals', []))
        
        if last_alert:
            old_events = set(last_alert.get('signals', []))
            
            # Check cooldown
            if current_time - last_alert['time'] < timedelta(minutes=self.cooldown_minutes):
                # If there are no new events compared to last time, block it
                if not new_events.difference(old_events):
                    return False
                
        self.alert_history[fingerprint_key] = {
            'time': current_time,
            'signals': list(new_events)
        }
        return True
