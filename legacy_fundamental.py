"""
fundamental_scorer.py
Fundamental-first scoring based on yfinance info.

Important: If data is missing, the score is not artificially set to 50.
Missing data receives a low/unknown grade and is visible through coverage.
"""
from __future__ import annotations
from typing import Dict, Any, Tuple

import numpy as np

from legacy_utils import safe_float, clip


ETF_LIKE = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "XLK", "XLF", "XLE", "XLV", "XLY", "SOXX", "GLD", "TLT"}


class FundamentalScorer:
    def __init__(self, data_provider):
        self.provider = data_provider

    def score(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        if symbol in ETF_LIKE:
            return self._empty(symbol, "ETF_OR_INDEX")

        info = self.provider.get_info(symbol)
        if not info:
            return self._empty(symbol, "NO_INFO")

        metrics = self._extract_metrics(info)
        coverage = self._coverage(metrics)

        growth_pts, growth_notes = self._score_growth(metrics)
        profit_pts, profit_notes = self._score_profitability(metrics)
        health_pts, health_notes = self._score_health(metrics)
        value_pts, value_notes = self._score_value(metrics)
        quality_pts, quality_notes = self._score_quality(metrics)

        raw_total = growth_pts + profit_pts + health_pts + value_pts + quality_pts
        # Penalize very low fundamental data coverage, but do not hide it.
        if coverage < 35:
            total = min(raw_total, 35.0)
        elif coverage < 55:
            total = min(raw_total, 55.0)
        else:
            total = raw_total
        total = round(clip(total), 2)

        if coverage < 25:
            grade = "NO_DATA"
        elif total >= 75:
            grade = "STRONG"
        elif total >= 60:
            grade = "GOOD"
        elif total >= 45:
            grade = "FAIR"
        elif total >= 30:
            grade = "WEAK"
        else:
            grade = "POOR"

        notes = growth_notes + profit_notes + health_notes + value_notes + quality_notes
        if coverage < 55:
            notes.append(f"Low fundamental data coverage: {coverage:.0f}%")

        return {
            "symbol": symbol,
            "fundamental_score": total,
            "fundamental_grade": grade,
            "fundamental_coverage": round(float(coverage), 1),
            "metrics": metrics,
            "notes": notes,
        }

    def _extract_metrics(self, info: Dict[str, Any]) -> Dict[str, Any]:
        # yfinance key mapping
        m = {
            "revenue_growth": safe_float(info.get("revenueGrowth")),
            "earnings_growth": safe_float(info.get("earningsGrowth")),
            "gross_margin": safe_float(info.get("grossMargins")),
            "operating_margin": safe_float(info.get("operatingMargins")),
            "profit_margin": safe_float(info.get("profitMargins")),
            "free_cashflow": safe_float(info.get("freeCashflow")),
            "forward_pe": safe_float(info.get("forwardPE")),
            "trailing_pe": safe_float(info.get("trailingPE")),
            "peg_ratio": safe_float(info.get("pegRatio")),
            "debt_to_equity": safe_float(info.get("debtToEquity")),
            "current_ratio": safe_float(info.get("currentRatio")),
            "return_on_equity": safe_float(info.get("returnOnEquity")),
            "return_on_assets": safe_float(info.get("returnOnAssets")),
            "market_cap": safe_float(info.get("marketCap") or info.get("market_cap")),
            "sector": info.get("sector") or "Unknown",
            "industry": info.get("industry") or "Unknown",
        }
        # normalize yfinance debtToEquity when it is in percent form, e.g. 53 => 0.53
        if m["debt_to_equity"] is not None and m["debt_to_equity"] > 10:
            m["debt_to_equity"] = m["debt_to_equity"] / 100.0
        return m

    def _coverage(self, m: Dict[str, Any]) -> float:
        important = [
            "revenue_growth", "gross_margin", "operating_margin", "profit_margin",
            "free_cashflow", "forward_pe", "trailing_pe", "debt_to_equity",
            "current_ratio", "return_on_equity", "market_cap",
        ]
        have = sum(1 for k in important if m.get(k) is not None)
        return have / len(important) * 100.0

    def _score_growth(self, m: Dict[str, Any]) -> Tuple[float, list]:
        pts = 0.0; notes = []
        rg = m.get("revenue_growth")
        eg = m.get("earnings_growth")
        if rg is not None:
            if rg > 0.30: pts += 14; notes.append(f"Revenue growth excellent: {rg:.1%}")
            elif rg > 0.15: pts += 10; notes.append(f"Revenue growth good: {rg:.1%}")
            elif rg > 0.05: pts += 6; notes.append(f"Revenue growth positive: {rg:.1%}")
            elif rg > 0: pts += 2; notes.append(f"Revenue growth slow: {rg:.1%}")
            else: notes.append(f"Revenue shrinking: {rg:.1%}")
        if eg is not None:
            if eg > 0.30: pts += 11; notes.append(f"Earnings growth excellent: {eg:.1%}")
            elif eg > 0.12: pts += 7; notes.append(f"Earnings growth positive: {eg:.1%}")
            elif eg > 0: pts += 3; notes.append(f"Earnings growth slow: {eg:.1%}")
        return min(25.0, pts), notes

    def _score_profitability(self, m: Dict[str, Any]) -> Tuple[float, list]:
        pts = 0.0; notes = []
        gm = m.get("gross_margin")
        om = m.get("operating_margin")
        pm = m.get("profit_margin")
        if gm is not None:
            if gm > 0.60: pts += 8; notes.append(f"Premium gross margin: {gm:.1%}")
            elif gm > 0.40: pts += 6; notes.append(f"Good gross margin: {gm:.1%}")
            elif gm > 0.25: pts += 3; notes.append(f"Moderate gross margin: {gm:.1%}")
        if om is not None:
            if om > 0.25: pts += 9; notes.append(f"Excellent operating margin: {om:.1%}")
            elif om > 0.12: pts += 6; notes.append(f"Positive operating margin: {om:.1%}")
            elif om > 0.03: pts += 3; notes.append(f"Low operating margin: {om:.1%}")
            elif om < 0: notes.append(f"Negative operating margin: {om:.1%}")
        if pm is not None:
            if pm > 0.20: pts += 8; notes.append(f"Strong profit margin: {pm:.1%}")
            elif pm > 0.10: pts += 5; notes.append(f"Healthy profit margin: {pm:.1%}")
            elif pm > 0.03: pts += 2; notes.append(f"Thin profit margin: {pm:.1%}")
        return min(25.0, pts), notes

    def _score_health(self, m: Dict[str, Any]) -> Tuple[float, list]:
        pts = 0.0; notes = []
        de = m.get("debt_to_equity")
        cr = m.get("current_ratio")
        fcf = m.get("free_cashflow")
        if de is not None:
            if de < 0.35: pts += 7; notes.append(f"Very low debt: D/E {de:.2f}")
            elif de < 0.8: pts += 5; notes.append(f"Manageable debt: D/E {de:.2f}")
            elif de < 1.6: pts += 2; notes.append(f"Moderate debt: D/E {de:.2f}")
            else: notes.append(f"High debt: D/E {de:.2f}")
        if cr is not None:
            if cr > 2.0: pts += 5; notes.append(f"Strong liquidity: current ratio {cr:.2f}")
            elif cr > 1.2: pts += 3; notes.append(f"Acceptable liquidity: current ratio {cr:.2f}")
            elif cr > 0.9: pts += 1; notes.append(f"Tight liquidity: current ratio {cr:.2f}")
            else: notes.append(f"Weak liquidity: current ratio {cr:.2f}")
        if fcf is not None:
            if fcf > 0: pts += 8; notes.append("Positive free cash flow")
            else: notes.append("Negative free cash flow")
        return min(20.0, pts), notes

    def _score_value(self, m: Dict[str, Any]) -> Tuple[float, list]:
        pts = 0.0; notes = []
        pe = m.get("forward_pe") if m.get("forward_pe") and m.get("forward_pe") > 0 else m.get("trailing_pe")
        peg = m.get("peg_ratio")
        if pe is not None and pe > 0:
            if pe < 15: pts += 9; notes.append(f"Cheap P/E: {pe:.1f}")
            elif pe < 25: pts += 7; notes.append(f"Fair P/E: {pe:.1f}")
            elif pe < 40: pts += 4; notes.append(f"Premium P/E: {pe:.1f}")
            elif pe < 70: pts += 1; notes.append(f"Expensive P/E: {pe:.1f}")
            else: notes.append(f"Very expensive P/E: {pe:.1f}")
        if peg is not None and peg > 0:
            if peg < 0.8: pts += 8; notes.append(f"Attractive PEG: {peg:.2f}")
            elif peg < 1.3: pts += 5; notes.append(f"Fair PEG: {peg:.2f}")
            elif peg < 2.2: pts += 2; notes.append(f"PEG premium: {peg:.2f}")
            else: notes.append(f"High PEG: {peg:.2f}")
        return min(17.0, pts), notes

    def _score_quality(self, m: Dict[str, Any]) -> Tuple[float, list]:
        pts = 0.0; notes = []
        roe = m.get("return_on_equity")
        roa = m.get("return_on_assets")
        if roe is not None:
            if roe > 0.25: pts += 6; notes.append(f"High ROE: {roe:.1%}")
            elif roe > 0.12: pts += 4; notes.append(f"Good ROE: {roe:.1%}")
            elif roe > 0.05: pts += 2; notes.append(f"Low positive ROE: {roe:.1%}")
        if roa is not None:
            if roa > 0.10: pts += 4; notes.append(f"High ROA: {roa:.1%}")
            elif roa > 0.04: pts += 2; notes.append(f"Positive ROA: {roa:.1%}")
        return min(13.0, pts), notes

    def _empty(self, symbol: str, reason: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "fundamental_score": 0.0,
            "fundamental_grade": reason,
            "fundamental_coverage": 0.0,
            "metrics": {},
            "notes": [reason],
        }
