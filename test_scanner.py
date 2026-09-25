import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd

from scanner import MarketRow, closed_bars, detect, divergence, fetch_kraken, format_report, is_pivot
from research import ResearchCache


class ScannerTests(unittest.TestCase):
    def test_only_finished_candles(self):
        now = datetime(2026, 9, 25, 14, 12, tzinfo=timezone.utc)
        index = pd.date_range("2026-09-25 13:55", periods=4, freq="5min", tz="UTC")
        frame = pd.DataFrame({"Close": [1, 2, 3, 4]}, index=index)
        result = closed_bars(frame, 5, now)
        self.assertEqual(list(result.Close), [1, 2, 3])

    def test_pivot_needs_right_confirmation(self):
        values = pd.Series([5] * 10 + [1, 2, 3])
        self.assertTrue(is_pivot(values, 10))
        self.assertFalse(is_pivot(values.iloc[:-1], 10))

    def test_divergence_requires_lower_price_and_higher_rsi_low(self):
        rsi = pd.Series([50.] * 90)
        rsi.iloc[30] = 20
        rsi.iloc[87] = 25
        price = pd.DataFrame({"Low": [100.] * 90, "High": [110.] * 90})
        price.loc[30, "Low"] = 90
        price.loc[87, "Low"] = 80
        self.assertIn(("BUY WATCH", "واگرایی مثبت RSI"), divergence(price, rsi))
        price.loc[87, "Low"] = 95
        self.assertEqual(divergence(price, rsi), [])

    def test_price_reentry_alert_uses_closed_candle(self):
        # Wide range to pass volatility filter, followed by outside/inside lower band.
        now = datetime(2026, 9, 25, 14, 12, tzinfo=timezone.utc)
        n = 170
        close = np.full(n, 100.) + np.sin(np.arange(n) / 4) * 0.5
        close[-3], close[-2], close[-1] = 97., 99., 50.
        index = pd.date_range(end="2026-09-25 14:10", periods=n, freq="5min", tz="UTC")
        frame = pd.DataFrame({"Open": close, "High": close + .5, "Low": close - .5,
                              "Close": close, "Volume": np.full(n, 10000)}, index=index)
        frame.iloc[-35, frame.columns.get_loc("High")] = 110.
        alerts = detect(frame, "TEST", "stocks", 5, now)
        self.assertTrue(any(a.side == "BUY WATCH" and "بولینگر" in a.reason for a in alerts))
        self.assertTrue(all(a.price == 99. for a in alerts))

    def test_old_aegis_fundamentals_are_cached_and_missing_data_not_fabricated(self):
        now = datetime(2026, 9, 25, 14, 12, tzinfo=timezone.utc)
        calls = []

        class Ticker:
            info = {"revenueGrowth": .2, "earningsGrowth": .14, "grossMargins": .5,
                    "operatingMargins": .18, "profitMargins": .12, "freeCashflow": 1e9,
                    "forwardPE": 23, "trailingPE": 27, "debtToEquity": 53,
                    "currentRatio": 1.6, "returnOnEquity": .19, "marketCap": 5e10}
            news = []

        def factory(symbol):
            calls.append(symbol)
            return Ticker()

        with TemporaryDirectory() as tmp:
            cache = ResearchCache(Path(tmp) / "cache.json", factory)
            first = cache.stock("ABC", now)
            second = cache.stock("ABC", now)
            self.assertTrue(first["available"])
            self.assertGreater(first["score"], 0)
            self.assertEqual(first["coverage"], 100)
            self.assertEqual(first, second)
            self.assertEqual(calls, ["ABC"])
            empty = ResearchCache(Path(tmp) / "empty.json", lambda symbol: type("Empty", (), {"info": {}, "news": []})()).stock("XYZ", now)
            self.assertIsNone(empty["score"])

    def test_market_report_separates_stock_quality_and_crypto_volatility(self):
        row = MarketRow("ABC", "stocks", "5m", "2026-09-25T16:00+02:00", 100, 1, 3, .2, 1.5, 40)
        coin = MarketRow("BTC-EUR", "crypto", "5m", "2026-09-25T16:00+02:00", 60000, 2, 5, .7, 1.2, 60)
        report = format_report([row, coin], {"ABC": {"available": True, "score": 70, "coverage": 91,
                      "news_count_7d": 2}}, datetime(2026, 9, 25, 14, tzinfo=timezone.utc))
        self.assertIn("ABC", report)
        self.assertIn("بنیادی 70/100", report)
        self.assertIn("BTC-EUR", report)
        self.assertIn("کریپتو: فقط نوسان", report)

    def test_kraken_discards_uncommitted_bar(self):
        data = {"error": [], "result": {"XXBTZEUR": [
            [1000, "10", "11", "9", "10", "10", "10", 1],
            [1300, "10", "12", "9", "11", "11", "12", 2],
            [1600, "11", "99", "1", "99", "99", "2", 1],
        ], "last": 1300}}
        with patch("scanner.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            result = fetch_kraken("BTC-EUR", 5)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["Close"].iloc[-1], 11)
        self.assertEqual(result.attrs["source"], "Kraken spot")


if __name__ == "__main__":
    unittest.main()
