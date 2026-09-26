"""Lightweight Activity Score + hot-watchlist gating (independent of Signal Score)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np
import pandas as pd


class ActivityMonitor:
    def __init__(self, hot_ttl_minutes: int = 60, cooldown_minutes: int = 30, activity_threshold: int = 40):
        self.hot_watchlist: Dict[str, datetime] = {}
        self.alert_history: Dict[str, Dict[str, Any]] = {}
        self.cooldown_minutes = cooldown_minutes
        self.hot_ttl_minutes = hot_ttl_minutes
        self.activity_threshold = max(0, min(100, int(activity_threshold)))
        # Conservative informational threshold (not a trade signal).
        self.high_activity_score = 70

    def calc_activity_score(self, df_5m: pd.DataFrame) -> Dict[str, Any]:
        """Activity Score 0-100 from volume/volatility/momentum — not Signal Score."""
        empty = {
            "score": 0,
            "events": [],
            "metrics": {
                "move_pct": 0.0,
                "abs_move_pct": 0.0,
                "relative_volume": 0.0,
                "atr_percent": 0.0,
                "range_expansion_ratio": 0.0,
            },
        }
        if df_5m is None or len(df_5m) < 20:
            return empty
        required = ("High", "Low", "Close", "Volume")
        if not all(c in df_5m.columns for c in required):
            return empty

        close = pd.to_numeric(df_5m["Close"], errors="coerce")
        high = pd.to_numeric(df_5m["High"], errors="coerce")
        low = pd.to_numeric(df_5m["Low"], errors="coerce")
        vol = pd.to_numeric(df_5m["Volume"], errors="coerce").fillna(0.0)
        if close.isna().tail(20).any() or close.iloc[-1] <= 0 or close.iloc[-2] <= 0:
            return empty

        score = 0
        events: List[str] = []

        # 1. Short-term absolute price movement
        move_pct = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100)
        abs_move = abs(move_pct)
        if abs_move > 0.5:
            score += 20
            events.append("price_acceleration")
        elif abs_move > 0.25:
            score += 10

        # Multi-bar momentum vs recent baseline
        look = min(12, len(close) - 1)
        baseline_move = float(abs(close.iloc[-1] - close.iloc[-1 - look]) / close.iloc[-1 - look] * 100)
        if baseline_move > 1.5:
            score += 15
            events.append("momentum_acceleration")
        elif baseline_move > 0.8:
            score += 8

        # 2. Relative volume + acceleration
        vol_sma = vol.rolling(20).mean()
        vol_prev = float(vol_sma.iloc[-2]) if np.isfinite(vol_sma.iloc[-2]) and vol_sma.iloc[-2] > 0 else 0.0
        rel_vol = float(vol.iloc[-1] / vol_sma.iloc[-1]) if np.isfinite(vol_sma.iloc[-1]) and vol_sma.iloc[-1] > 0 else 0.0
        if rel_vol >= 2.0:
            score += 30
            events.append("volume_spike")
        elif rel_vol >= 1.5:
            score += 15
            events.append("elevated_volume")
        if vol_prev > 0 and float(vol.iloc[-1]) > vol_prev * 1.8 and rel_vol >= 1.3:
            score += 10
            if "volume_acceleration" not in events:
                events.append("volume_acceleration")

        # 3. ATR / range expansion
        tr = pd.concat(
            ((high - low), (high - close.shift()).abs(), (low - close.shift()).abs()),
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(14).mean()
        atr_now = float(atr.iloc[-1]) if np.isfinite(atr.iloc[-1]) else 0.0
        tr_now = float(tr.iloc[-1]) if np.isfinite(tr.iloc[-1]) else 0.0
        atr_pct = float(atr_now / close.iloc[-1] * 100) if close.iloc[-1] else 0.0
        range_ratio = float(tr_now / atr_now) if atr_now > 0 else 0.0
        if range_ratio >= 1.5:
            score += 25
            events.append("range_expansion")
        elif range_ratio >= 1.2:
            score += 12
        if atr_pct >= 1.0:
            score += 10
            events.append("elevated_atr")

        score = int(min(100, score))
        return {
            "score": score,
            "events": events,
            "metrics": {
                "move_pct": round(move_pct, 3),
                "abs_move_pct": round(abs_move, 3),
                "relative_volume": round(rel_vol, 3),
                "atr_percent": round(atr_pct, 3),
                "range_expansion_ratio": round(range_ratio, 3),
            },
        }

    def is_high_activity(self, activity: Dict[str, Any]) -> bool:
        """Informational unusual-activity gate. Never implies BUY/SELL."""
        score = int(activity.get("score", 0) or 0)
        events = set(activity.get("events") or [])
        metrics = activity.get("metrics") or {}
        rel_vol = float(metrics.get("relative_volume") or 0)
        abs_move = float(metrics.get("abs_move_pct") or 0)

        if score >= 80:
            return True
        if score >= self.high_activity_score and events.intersection(
            {"volume_spike", "range_expansion", "momentum_acceleration", "volume_acceleration"}
        ):
            return True
        if rel_vol >= 3.0 and abs_move >= 0.8 and score >= 55:
            return True
        return False

    def update_hot_watchlist(self, symbol: str, activity: Dict[str, Any], current_time: datetime):
        """Hot entry from activity alone — no BUY/SELL signal required."""
        events = set(activity.get("events") or [])
        score = int(activity.get("score", 0) or 0)
        metrics = activity.get("metrics") or {}
        unusual = (
            score >= self.activity_threshold
            or bool(events.intersection({"volume_spike", "range_expansion", "momentum_acceleration", "price_acceleration"}))
            or float(metrics.get("relative_volume") or 0) >= 2.0
            or float(metrics.get("abs_move_pct") or 0) >= 0.8
        )
        if unusual:
            self.hot_watchlist[symbol] = current_time + timedelta(minutes=self.hot_ttl_minutes)

        expired = [sym for sym, exp in self.hot_watchlist.items() if current_time > exp]
        for sym in expired:
            del self.hot_watchlist[sym]

    def is_hot(self, symbol: str) -> bool:
        return symbol in self.hot_watchlist

    def should_send_alert(self, symbol: str, signal: Dict[str, Any], current_time: datetime) -> bool:
        """Event-aware dedup: identical stages blocked; stage upgrades allowed."""
        if not signal.get("signals"):
            return False

        fingerprint_key = f"{symbol}"
        last_alert = self.alert_history.get(fingerprint_key)
        new_events = set(signal.get("signals", []))

        if last_alert:
            old_events = set(last_alert.get("signals", []))
            if current_time - last_alert["time"] < timedelta(minutes=self.cooldown_minutes):
                if not new_events.difference(old_events):
                    return False

        self.alert_history[fingerprint_key] = {
            "time": current_time,
            "signals": list(new_events),
        }
        return True


def rank_activity(rows: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """Rank discovery rows by Activity Score (diagnostic; not a trade signal)."""
    ranked = sorted(rows, key=lambda r: (-int(r.get("score", 0)), str(r.get("symbol", ""))))
    return ranked[:limit]


def format_top_activity(label: str, ranked: List[Dict[str, Any]]) -> str:
    lines = [f"{label}:"]
    if not ranked:
        lines.append("(none)")
        return "\n".join(lines)
    for idx, row in enumerate(ranked, start=1):
        lines.append(f"{idx} {row['symbol']} {int(row['score'])}")
    return "\n".join(lines)
