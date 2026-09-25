"""Daily cached AEGIS fundamental and headline context, separate from trade timing."""
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
        age = now.timestamp() - cached.get("fetched_at", 0)
        if 0 <= age < (24 * 3600 if cached.get("available") else 2 * 3600):
            return cached

        if self.ticker_factory is None:
            import yfinance as yf
            self.ticker_factory = yf.Ticker
        try:
            ticker = self.ticker_factory(symbol)
            info = ticker.info or {}
            # News is context only. A missing news endpoint does not erase fundamentals.
            try:
                news = ticker.news or []
            except Exception:
                news = []
            provider = YahooResearchProvider(info=info, news=news)
            fundamental = FundamentalScorer(provider).score(symbol)
            try:
                headline = EarlySignalDetector(provider).analyze(symbol)
            except Exception:
                headline = {"news_count_7d": None, "top_headlines": []}
            available = fundamental["fundamental_coverage"] >= 35
            snapshot = {
                "fetched_at": now.timestamp(),
                "available": available,
                "score": fundamental["fundamental_score"] if available else None,
                "grade": fundamental["fundamental_grade"],
                "coverage": fundamental["fundamental_coverage"],
                "metrics": fundamental["metrics"],
                "notes": fundamental["notes"][:5],
                "news_count_7d": headline["news_count_7d"],
                "headlines": headline["top_headlines"][:3],
                "source": "Yahoo Finance snapshot / AEGIS scorer",
            }
            self.entries[symbol] = snapshot
            atomic_json(self.path, self.entries)
            return snapshot
        except Exception:
            # Return recent, explicitly stale data only if fetch failed.
            if cached.get("available") and 0 <= age <= 72 * 3600:
                return {**cached, "stale": True}
            snapshot = {"fetched_at": now.timestamp(), "available": False, "score": None,
                        "grade": "UNAVAILABLE", "coverage": 0, "notes": [],
                        "headlines": [], "news_count_7d": None, "source": "Yahoo Finance unavailable"}
            self.entries[symbol] = snapshot
            atomic_json(self.path, self.entries)
            return snapshot
