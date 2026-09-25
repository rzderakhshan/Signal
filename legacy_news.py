"""
early_signal_detector.py
Looks for catalysts before the price has fully reacted.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import re
from typing import Dict, Any, List

from legacy_utils import clip

POSITIVE_KEYWORDS = {
    "raised guidance": 8, "beat estimates": 7, "earnings beat": 6, "record revenue": 7,
    "contract award": 8, "major contract": 8, "partnership": 5, "collaboration": 4,
    "fda approval": 9, "approval": 4, "breakthrough": 7, "new product": 5,
    "buyback": 5, "upgrade": 5, "outperform": 4, "ai": 3, "cloud": 2,
}
NEGATIVE_KEYWORDS = {
    "guidance cut": -9, "miss estimates": -6, "downgrade": -5, "lawsuit": -5,
    "investigation": -5, "recall": -5, "bankruptcy": -10, "layoffs": -2,
}


class EarlySignalDetector:
    def __init__(self, data_provider):
        self.provider = data_provider

    def analyze(self, symbol: str) -> Dict[str, Any]:
        news = self.provider.get_news(symbol)
        info = self.provider.get_info(symbol)
        now = datetime.now(timezone.utc)
        cutoff_7 = now - timedelta(days=7)
        cutoff_30 = now - timedelta(days=30)

        news_items: List[Dict[str, str]] = []
        news_7 = 0
        news_30 = 0
        keyword_score = 0.0

        for item in news or []:
            title, pub_dt, link = self._extract_news_item(item)
            if not title:
                continue
            if not self._is_relevant(title, symbol, info):
                continue
            title_l = title.lower()
            if pub_dt and pub_dt >= cutoff_30:
                news_30 += 1
                if len(news_items) < 10:
                    news_items.append({
                        "title": title,
                        "published": pub_dt.isoformat() if pub_dt else "",
                        "link": link,
                    })
                if pub_dt >= cutoff_7:
                    news_7 += 1
                    for kw, w in POSITIVE_KEYWORDS.items():
                        if kw in title_l:
                            keyword_score += w
                    for kw, w in NEGATIVE_KEYWORDS.items():
                        if kw in title_l:
                            keyword_score += w

        # velocity: 7d news compared with approximate previous 3 weeks
        velocity_score = 0.0
        if news_30 > 0:
            base = max(1.0, (news_30 - news_7) / 3.0)
            velocity = news_7 / base
            if velocity >= 3: velocity_score = 25
            elif velocity >= 2: velocity_score = 18
            elif velocity >= 1.25: velocity_score = 10
            elif velocity >= 1: velocity_score = 5
        else:
            velocity = 0.0

        count_score = 0.0
        if news_7 >= 8: count_score = 15
        elif news_7 >= 5: count_score = 10
        elif news_7 >= 2: count_score = 5
        elif news_7 >= 1: count_score = 2

        key_score = clip(keyword_score, 0, 45)
        total = clip(velocity_score + count_score + key_score)

        if total >= 70: grade = "STRONG_EARLY"
        elif total >= 45: grade = "MODERATE_EARLY"
        elif total >= 20: grade = "WEAK_EARLY"
        else: grade = "NONE"

        return {
            "early_signal_score": round(total, 2),
            "early_signal_grade": grade,
            "news_count_7d": news_7,
            "news_count_30d": news_30,
            "news_velocity": round(float(velocity), 2),
            "top_headlines": [item["title"] for item in news_items],
            "news_items": news_items,
        }

    def skipped(self, reason="SKIPPED_LOW_FUNDAMENTAL") -> Dict[str, Any]:
        return {
            "early_signal_score": 0.0,
            "early_signal_grade": reason,
            "news_count_7d": 0,
            "news_count_30d": 0,
            "news_velocity": 0.0,
            "top_headlines": [],
            "news_items": [],
        }

    def _extract_news_item(self, item):
        title = ""
        pub_dt = None
        link = ""
        try:
            content = item.get("content", {}) if isinstance(item, dict) else {}
            title = content.get("title") or item.get("title") or ""
            raw = content.get("pubDate") or item.get("providerPublishTime") or item.get("pubDate")
            canonical = content.get("canonicalUrl") or {}
            click_through = content.get("clickThroughUrl") or {}
            link = (
                canonical.get("url")
                or click_through.get("url")
                or item.get("link")
                or ""
            )
            if isinstance(raw, (int, float)):
                pub_dt = datetime.fromtimestamp(raw, tz=timezone.utc)
            elif isinstance(raw, str):
                pub_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pass
        return str(title), pub_dt, str(link)

    def _is_relevant(self, title: str, symbol: str, info: Dict[str, Any]) -> bool:
        """Keep company-specific headlines and remove broad Yahoo feed spillover."""
        title_l = title.lower()
        ticker = symbol.split(".")[0].replace("-", " ")
        if len(ticker) >= 2 and re.search(
            rf"\b{re.escape(ticker.lower())}\b", title_l
        ):
            return True

        names = [
            str(info.get("shortName") or ""),
            str(info.get("longName") or ""),
        ]
        ignored = {
            "inc", "incorporated", "corp", "corporation", "company", "companies",
            "plc", "ltd", "limited", "group", "holding", "holdings", "class",
            "common", "stock", "the", "and",
        }
        tokens = {
            token.lower()
            for name in names
            for token in re.findall(r"[A-Za-z0-9]+", name)
            if len(token) >= 4 and token.lower() not in ignored
        }
        if not tokens:
            return True
        return any(re.search(rf"\b{re.escape(token)}\b", title_l) for token in tokens)
