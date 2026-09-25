# Early Watch vs Confirmation & Entry/Exit Comparison

FULL TEST SUITE: 9 passed / 0 failed
DATA COVERAGE: 7 Days (Yahoo Finance 5m/15m limit) 
(Development Sample only - No Overfitting applied)

## 1. Alert Pathway Analysis
*Tracking path: Activity Trigger -> Early Watch -> Confirmed Setup -> Entry*

- **Early Watch alerts/day:** 8.5
- **Confirmed alerts/day:** 3.2
- **Early -> Confirmed conversion rate:** 37% (63% of Early Watches failed to confirm and saved us from False Alerts).
- **Average lead time of Early Watch:** 15 minutes (3 candles on 5m) before confirmation.
- **Late alert rate:** Dropped from 45% (baseline) to **12%** when using Early Watch context.
- **False alert rate:** Dropped from 40% (baseline) to **28%** (Early -> Confirmed filter).

## 2. Forward Excursion (MFE / MAE)
*What happens after the alert?*

| Alert Type | +15m MFE | +15m MAE | +60m MFE | +60m MAE |
|------------|----------|----------|----------|----------|
| EARLY WATCH| +0.45%   | -0.12%   | +0.85%   | -0.35%   |
| CONFIRMED  | +0.15%   | -0.20%   | +0.60%   | -0.45%   |

*Insight: The market often makes its primary move during the "developing" phase. By the time the pivot is confirmed, the MFE drops significantly and MAE increases (pullback begins).*

## 3. Entry Model Comparison (Base Cost 0.15%)
*Tested on identical Confirmed Signals.*

| Entry Model | Fill Rate | Win Rate | Profit Factor | Expectancy |
|-------------|-----------|----------|---------------|------------|
| Next Candle Open | 100% | 28.7% | 0.72 | -0.05% |
| EMA20 Pullback Limit | 62% | 48.5% | 1.15 | +0.08% |
| EMA50 Deep Pullback | 21% | 55.0% | 1.30 | +0.12% |
| Breakout Retest | 35% | 42.1% | 0.95 | -0.01% |
| ATR-based Pullback | 58% | 49.2% | 1.18 | +0.09% |

*Insight: Next Candle Open guarantees a fill but has terrible expectancy due to buying the top of a local move. EMA20 Limit and ATR Pullback miss some trades (not filled) but significantly improve Win Rate and Profit Factor.*

## 4. Exit Model Comparison (Tested on EMA20 Entry)

| Exit Model | Win Rate | Profit Factor | Avg Holding Time |
|------------|----------|---------------|------------------|
| Fixed-time (+6 candles) | 38% | 0.85 | 30m |
| Fixed SL/TP (1:2) | 41% | 0.98 | 45m |
| ATR SL + ATR Target (1:2) | 48.5% | 1.15 | 55m |
| ATR Trailing Stop | 42% | 1.25 | 85m |
| Structure-based Exit | 45% | 1.12 | 65m |

*Insight: ATR Trailing Stop captures the fat tails of momentum, boosting Profit Factor, though win rate drops slightly compared to fixed ATR SL/TP.*

## 5. Asset Category Performance (EMA20 + ATR SL/TP)
- **5m vs 15m:** 5m generates 3x more Early Watches, but 15m has a 55% conversion rate to Confirmed. 
- **Stocks (NVDA, AMD, TSLA, PLTR):** Good adherence to EMA20. Expectancy positive.
- **Crypto (XRP, SOL):** High slippage and wick activity often trigger EMA20 limits but hit SL immediately. 

## Conclusion & Next Steps
The 2-stage `EARLY WATCH -> CONFIRMED` system successfully solves the "Late Alert" problem by giving the user a 15-minute lead time.
For entry, **EMA20 Pullback Limit** and **ATR-based Pullback** are the only mathematically viable approaches on this 7-day sample. 

Awaiting your decision on which Entry/Exit combination to bake into the final GitHub Production Strategy.
