"""Cached stock research: slow fundamentals + faster catalyst/news context.

This module is stock-only. Crypto never receives corporate fundamental scores.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from legacy_fundamental import FundamentalScorer
from legacy_news import EarlySignalDetector


class YahooResearchProvider:
    def __init__(self, info=None, news=None):
        self.info = info
        self.news = news

    def get_info(self, symbol):
        return self.info if self.info is not None else {}

    def get_news(self, symbol):
        return self.news if self.news is not None else []


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ResearchCache:
    FUNDAMENTAL_TTL = 60 * 60
    NEWS_TTL = 60 * 60
    STALE_FUNDAMENTAL_MAX = 72 * 3600

    def __init__(self, path: Path, ticker_factory=None):
        self.path = path
        try:
            self.entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(self.entries, dict):
                self.entries = {}
        except (OSError, ValueError):
            self.entries = {}
        self.ticker_factory = ticker_factory

    def stock(self, symbol: str, now: datetime) -> dict:
        cached = self.entries.get(symbol, {})
        ts = now.timestamp()
        fund_at = cached.get("fundamental_fetched_at", cached.get("fetched_at", 0))
        news_at = cached.get("news_fetched_at", cached.get("fetched_at", 0))
        fund_fresh = cached.get("available") and 0 <= ts - fund_at < self.FUNDAMENTAL_TTL
        news_fresh = 0 <= ts - news_at < self.NEWS_TTL and "catalyst_score" in cached
        if fund_fresh and news_fresh:
            return cached

        if self.ticker_factory is None:
            import yfinance as yf
            self.ticker_factory = yf.Ticker

        ticker = None
        snapshot = dict(cached)
        try:
            ticker = self.ticker_factory(symbol)
            if not fund_fresh:
                info = ticker.info or {}
                fundamental = FundamentalScorer(YahooResearchProvider(info=info)).score(symbol)
                available = fundamental["fundamental_coverage"] >= 35
                snapshot.update({
                    "fundamental_fetched_at": ts,
                    "fetched_at": ts,  # backward compatibility
                    "available": available,
                    "score": fundamental["fundamental_score"] if available else None,
                    "grade": fundamental["fundamental_grade"],
                    "coverage": fundamental["fundamental_coverage"],
                    "metrics": fundamental["metrics"],
                    "notes": fundamental["notes"][:8],
                    "source": "Yahoo Finance snapshot / AEGIS scorer",
                })

            if not news_fresh:
                # News is independent: a failed endpoint must not erase fundamentals.
                try:
                    info_for_names = (ticker.info or {}) if not snapshot.get("metrics") else {}
                except Exception:
                    info_for_names = {}
                try:
                    news = ticker.news or []
                    catalyst = EarlySignalDetector(YahooResearchProvider(info=info_for_names, news=news)).analyze(symbol)
                    snapshot.update({
                        "news_fetched_at": ts,
                        "catalyst_score": catalyst.get("early_signal_score", 0.0),
                        "catalyst_grade": catalyst.get("early_signal_grade", "NONE"),
                        "news_count_7d": catalyst.get("news_count_7d"),
                        "news_count_30d": catalyst.get("news_count_30d"),
                        "news_velocity": catalyst.get("news_velocity"),
                        "headlines": catalyst.get("top_headlines", [])[:3],
                    })
                except Exception:
                    snapshot.setdefault("catalyst_score", 0.0)
                    snapshot.setdefault("catalyst_grade", "UNAVAILABLE")
                    snapshot.setdefault("headlines", [])
                    snapshot.setdefault("news_count_7d", None)

            self.entries[symbol] = snapshot
            atomic_json(self.path, self.entries)
            return snapshot
        except Exception:
            age = ts - fund_at
            if cached.get("available") and 0 <= age <= self.STALE_FUNDAMENTAL_MAX:
                return {**cached, "stale": True}
            snapshot = {
                "fundamental_fetched_at": ts, "news_fetched_at": ts, "fetched_at": ts,
                "available": False, "score": None, "grade": "UNAVAILABLE", "coverage": 0,
                "metrics": {}, "notes": [], "headlines": [], "news_count_7d": None,
                "catalyst_score": 0.0, "catalyst_grade": "UNAVAILABLE",
                "source": "Yahoo Finance unavailable",
            }
            self.entries[symbol] = snapshot
            atomic_json(self.path, self.entries)
            return snapshot
