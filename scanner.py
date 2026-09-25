"""Closed-candle, watchlist-based intraday screening and Telegram alerts."""
from __future__ import annotations

import argparse
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import telegram_utils

from research import ResearchCache, atomic_json

BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class Alert:
    symbol: str
    asset: str
    timeframe: str
    side: str
    reason: str
    candle: str
    price: float
    rsi: float
    atr_pct: float
    range_pct: float
    position: float
    score: float
    source: str = "Yahoo"

    @property
    def key(self) -> str:
        return "|".join((self.symbol, self.timeframe, self.side, self.reason, self.candle))


@dataclass(frozen=True)
class MarketRow:
    symbol: str
    asset: str
    timeframe: str
    candle: str
    price: float
    atr_pct: float
    range_pct: float
    position: float
    volume_ratio: float | None
    volatility_score: float
    source: str = "Yahoo"


def wilder_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    ratio = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + ratio)
    result = result.mask((loss == 0) & (gain > 0), 100)
    result = result.mask((loss == 0) & (gain == 0), 50)
    return result


def closed_bars(frame: pd.DataFrame, minutes: int, now: datetime) -> pd.DataFrame:
    """Yahoo timestamps candle starts; discard unfinished and stale bars."""
    if frame.empty or frame.index.tz is None:
        return frame.iloc[:0]
    timestamps = frame.index.tz_convert(timezone.utc)
    cutoff = pd.Timestamp(now.astimezone(timezone.utc) - timedelta(minutes=minutes, seconds=90))
    closed = frame.loc[timestamps <= cutoff].sort_index()
    return closed.loc[~closed.index.duplicated(keep="last")]


def is_pivot(values: pd.Series, index: int, left: int = 10, right: int = 2, low: bool = True) -> bool:
    if index < left or index + right >= len(values):
        return False
    center = values.iloc[index]
    neighbors = values.iloc[index - left:index + right + 1].drop(values.index[index])
    if not np.isfinite(center) or not np.isfinite(neighbors.to_numpy(dtype=float)).all():
        return False
    return bool(center < neighbors.min() if low else center > neighbors.max())


def divergence(df: pd.DataFrame, rsi: pd.Series) -> list[tuple[str, str]]:
    """Regular RSI divergence, TORYS defaults: left 10, confirmation 2, lookback 120."""
    current = len(df) - 3
    signals = []
    for low, side, col in ((True, "BUY WATCH", "Low"), (False, "SELL WATCH", "High")):
        if not is_pivot(rsi, current, low=low):
            continue
        found = 0
        for previous in range(current - 1, max(9, current - 120), -1):
            if not is_pivot(rsi, previous, low=low):
                continue
            found += 1
            a, b = float(rsi.iloc[previous]), float(rsi.iloc[current])
            pa, pb = float(df[col].iloc[previous]), float(df[col].iloc[current])
            valid = (pb < pa and b > a and min(a, b) <= 50) if low else (pb > pa and b < a and max(a, b) >= 50)
            if valid:
                signals.append((side, "واگرایی مثبت RSI" if low else "واگرایی منفی RSI"))
                break
            if found >= 9:
                break
    return signals


def detect(df: pd.DataFrame, symbol: str, asset: str, minutes: int, now: datetime, source: str = "Yahoo") -> list[Alert]:
    df = closed_bars(df, minutes, now)
    if len(df) < 155:
        return []
    if not all(c in df for c in ("Open", "High", "Low", "Close", "Volume")):
        return []
    values = df[["Open", "High", "Low", "Close", "Volume"]].tail(400).apply(pd.to_numeric, errors="coerce")
    if values[["Open", "High", "Low", "Close"]].isna().any().any() or (values["Close"] <= 0).any():
        return []
    df = values
    age = now.astimezone(timezone.utc) - df.index[-1].to_pydatetime().astimezone(timezone.utc)
    if age > timedelta(minutes=minutes * 2 + 5):
        return []
    if asset == "stocks" and (now.astimezone(BERLIN).weekday() >= 5 or df["Volume"].tail(20).sum() <= 0):
        return []
    high, low, close = df["High"], df["Low"], df["Close"]
    rsi = wilder_rsi(close)
    if not np.isfinite(rsi.iloc[-1]):
        return []
    tr = pd.concat(((high - low), (high - close.shift()).abs(), (low - close.shift()).abs()), axis=1).max(axis=1)
    atr_pct = float(tr.tail(14).mean() / close.iloc[-1] * 100)
    top, bottom = float(high.tail(48).max()), float(low.tail(48).min())
    range_pct = (top - bottom) / float(close.iloc[-1]) * 100
    position = (float(close.iloc[-1]) - bottom) / (top - bottom) if top > bottom else 0.5
    if atr_pct < 0.15 or range_pct < 0.8:
        return []
    mean = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    upper, lower = mean + 2 * std, mean - 2 * std
    events = divergence(df, rsi)
    if close.iloc[-2] < lower.iloc[-2] and close.iloc[-1] >= lower.iloc[-1] and position <= 0.45:
        events.append(("BUY WATCH", "بازگشت قیمت به باند بولینگر"))
    if close.iloc[-2] > upper.iloc[-2] and close.iloc[-1] <= upper.iloc[-1] and position >= 0.55:
        events.append(("SELL WATCH", "بازگشت قیمت به باند بولینگر"))
    # A confirmed pivot belongs to the candle two bars before the current one;
    # the notification timestamp is the *confirmation* candle, never the pivot.
    stamp = df.index[-1].tz_convert(BERLIN).isoformat(timespec="minutes")
    results = []
    for side, reason in events:
        proximity = 1 - position if side == "BUY WATCH" else position
        score = round(atr_pct * 2 + min(range_pct, 10) + proximity * 4 + (2 if "واگرایی" in reason else 0), 2)
        results.append(Alert(symbol, asset, f"{minutes}m", side, reason, stamp, float(close.iloc[-1]), float(rsi.iloc[-1]), round(atr_pct, 2), round(range_pct, 2), round(position, 2), score, source))
    return results


def market_row(frame: pd.DataFrame, symbol: str, asset: str, minutes: int, now: datetime, source: str = "Yahoo") -> MarketRow | None:
    df = closed_bars(frame, minutes, now)
    if len(df) < 50 or not all(col in df for col in ("High", "Low", "Close", "Volume")):
        return None
    df = df[["High", "Low", "Close", "Volume"]].tail(60).apply(pd.to_numeric, errors="coerce")
    if df[["High", "Low", "Close"]].isna().any().any() or df["Close"].iloc[-1] <= 0:
        return None
    if now.astimezone(timezone.utc) - df.index[-1].to_pydatetime().astimezone(timezone.utc) > timedelta(minutes=minutes * 2 + 5):
        return None
    if asset == "stocks" and (now.astimezone(BERLIN).weekday() >= 5 or df["Volume"].tail(20).sum() <= 0):
        return None
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(((high - low), (high - close.shift()).abs(), (low - close.shift()).abs()), axis=1).max(axis=1)
    atr = float(tr.tail(14).mean() / close.iloc[-1] * 100)
    top, bottom = float(high.tail(48).max()), float(low.tail(48).min())
    width = (top - bottom) / float(close.iloc[-1]) * 100
    pos = (float(close.iloc[-1]) - bottom) / (top - bottom) if top > bottom else 0.5
    recent = float(df["Volume"].tail(3).mean())
    baseline = float(df["Volume"].iloc[-23:-3].mean())
    volume_ratio = round(recent / baseline, 2) if baseline > 0 else None
    # Descriptive priority, never a probability of return.
    score = round(min(atr, 4) * 12 + min(width, 12) * 3 + min(volume_ratio or 0, 3) * 4, 1)
    return MarketRow(symbol, asset, f"{minutes}m", df.index[-1].tz_convert(BERLIN).isoformat(timespec="minutes"),
                     float(close.iloc[-1]), round(atr, 2), round(width, 2), round(pos, 2), volume_ratio, score, source)


def fetch_yahoo(symbols: list[str], minutes: int) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    if not symbols:
        return {}
    data = yf.download(symbols, period="10d", interval=f"{minutes}m", group_by="ticker", auto_adjust=False, threads=False, progress=False, timeout=20)
    result = {}
    for symbol in symbols:
        try:
            part = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
            part = part.dropna(subset=["Open", "High", "Low", "Close"])
            if not part.empty:
                part.attrs["source"] = "Yahoo"
                result[symbol] = part
        except (KeyError, TypeError):
            pass
    return result


def fetch_kraken(symbol: str, minutes: int) -> pd.DataFrame:
    """Public Kraken spot OHLC, with its final uncommitted candle removed."""
    base, quote = symbol.upper().split("-", 1)
    pair = ("XBT" if base == "BTC" else base) + quote
    query = urllib.parse.urlencode({"pair": pair, "interval": minutes})
    req = urllib.request.Request(f"https://api.kraken.com/0/public/OHLC?{query}", headers={"User-Agent": "intraday-signal-scanner/1.1"})
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.load(response)
    if payload.get("error"):
        raise ValueError("Kraken pair unavailable")
    rows = next((v for k, v in payload.get("result", {}).items() if k != "last"), [])
    if len(rows) < 3:
        raise ValueError("Kraken OHLC has insufficient data")
    committed = rows[:-1]
    frame = pd.DataFrame(committed, columns=["Time", "Open", "High", "Low", "Close", "VWAP", "Volume", "Count"])
    frame.index = pd.to_datetime(frame.pop("Time"), unit="s", utc=True)
    frame = frame[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce").dropna()
    frame.attrs["source"] = "Kraken spot"
    return frame


def fetch(symbols: list[str], minutes: int, crypto_symbols: list[str]) -> dict[str, pd.DataFrame]:
    crypto = set(crypto_symbols)
    result = {}
    failed_crypto = []
    for symbol in symbols:
        if symbol not in crypto:
            continue
        try:
            result[symbol] = fetch_kraken(symbol, minutes)
        except (OSError, ValueError, KeyError, IndexError):
            failed_crypto.append(symbol)
    yahoo_symbols = [symbol for symbol in symbols if symbol not in crypto] + failed_crypto
    if yahoo_symbols:
        try:
            result.update(fetch_yahoo(yahoo_symbols, minutes))
        except Exception as exc:
            print(f"Yahoo {minutes}m unavailable ({type(exc).__name__})", file=sys.stderr)
    return result


def send_telegram(token: str, chat_id: str, msg: str) -> None:
    # We ignore the token/chat_id arguments and rely on telegram_utils via .env
    success = telegram_utils.send_telegram_message(msg)
    if not success:
        raise RuntimeError("Telegram did not accept the message")


def load_local_env(path: Path) -> None:
    """Read only the two Telegram settings; existing process variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SIGNAL_CHAT_ID"}:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def format_alert(a: Alert, research: dict | None = None) -> str:
    category = "کریپتو" if a.asset == "crypto" else "سهام"
    if a.asset == "crypto":
        context = "بنیادی شرکتی: برای کریپتو کاربرد ندارد."
    elif research and research.get("available"):
        context = (f"بنیادی AEGIS: {research['score']:.0f}/100 | پوشش داده: {research['coverage']:.0f}%"
                   + (" | دادهٔ قدیمی" if research.get("stale") else ""))
        if research.get("notes"):
            context += f"\nنکته: {research['notes'][0]}"
        if research.get("headlines"):
            context += f"\nتیتر اخیر: {research['headlines'][0][:140]}"
    else:
        context = "بنیادی: دادهٔ کافی در دسترس نیست؛ امتیاز ساختگی محاسبه نشد."
    return (f"{'🟢' if a.side == 'BUY WATCH' else '🔴'} {a.side} | {a.symbol} | {category} | {a.timeframe}\n"
            f"{a.reason}\nتأیید: {a.candle} (Berlin)\nقیمت مرجع {a.source}: {a.price:.6g}\n"
            f"RSI: {a.rsi:.1f} | ATR: {a.atr_pct:.2f}% | دامنهٔ ۴۸ کندل: {a.range_pct:.2f}%\n"
            f"جایگاه در دامنه: {a.position:.0%} | اولویت تکنیکال: {a.score:.1f}\n{context}\n"
            "هشدار بررسی است؛ قیمت و امکان معامله را در Trade Republic بررسی کنید.")


def format_report(rows: list[MarketRow], profiles: dict[str, dict], now: datetime) -> str:
    stocks = [r for r in rows if r.asset == "stocks"]
    crypto = [r for r in rows if r.asset == "crypto"]
    qualified = [r for r in stocks if profiles.get(r.symbol, {}).get("available")]
    qualified.sort(key=lambda r: r.volatility_score * .65 + profiles[r.symbol]["score"] * .35, reverse=True)
    crypto.sort(key=lambda r: r.volatility_score, reverse=True)
    unknown = [r for r in stocks if not profiles.get(r.symbol, {}).get("available")]
    unknown.sort(key=lambda r: r.volatility_score, reverse=True)
    lines = [f"📊 دیده‌بان بازار | {now.astimezone(BERLIN):%Y-%m-%d %H:%M} Berlin",
             "رتبهٔ نوسان و کیفیت، پیش‌بینی سود یا سیگنال ورود نیست.", "", "سهام: کیفیت بنیادی + نوسان اخیر"]
    for r in qualified[:5]:
        p = profiles[r.symbol]
        fresh_news = p.get("news_count_7d")
        news_text = f" | خبر ۷ روز: {fresh_news}" if fresh_news is not None else ""
        stale_text = " | دادهٔ قدیمی" if p.get("stale") else ""
        lines.append(f"• {r.symbol} | نوسان {r.volatility_score:.0f} | بنیادی {p['score']:.0f}/100 (پوشش {p['coverage']:.0f}%){stale_text} | ATR {r.atr_pct:.2f}% | جایگاه {r.position:.0%}{news_text}")
        if p.get("headlines"):
            lines.append(f"  تیتر: {p['headlines'][0][:110]}")
    if not qualified:
        lines.append("• دادهٔ بنیادی کافی برای سهام فعلی موجود نیست.")
    if unknown:
        lines.append("سهام پرنوسان با دادهٔ بنیادی ناکافی: " + ", ".join(r.symbol for r in unknown[:5]))
    lines.append("\nکریپتو: فقط نوسان و حجم نسبی")
    for r in crypto[:5]:
        vol = f"{r.volume_ratio:.1f}×" if r.volume_ratio is not None else "ناموجود"
        lines.append(f"• {r.symbol} | نوسان {r.volatility_score:.0f} | ATR {r.atr_pct:.2f}% | دامنه {r.range_pct:.2f}% | جایگاه {r.position:.0%} | حجم {vol}")
    if not crypto:
        lines.append("• دادهٔ تازه‌ای از کریپتو دریافت نشد.")
    lines.append("\nمنبع: Yahoo برای سهام/بنیادی، Kraken spot یا Yahoo برای کریپتو. قیمت Trade Republic را جدا بررسی کنید.")
    return "\n".join(lines)[:4000]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", type=Path, default=Path("watchlist.json"))
    parser.add_argument("--state", type=Path, default=Path(".state/state.json"))
    parser.add_argument("--research-cache", type=Path, default=Path(".state/research_cache.json"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--github-dry-run", action="store_true")
    parser.add_argument("--report-now", action="store_true", help="Generate the volatility and fundamental digest now")
    parser.add_argument("--test-telegram", action="store_true", help="Send one harmless connection check")
    args = parser.parse_args()
    load_local_env(args.env_file)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", os.getenv("SIGNAL_CHAT_ID", ""))
    
    is_dry_run = args.dry_run or args.github_dry_run
    
    if not is_dry_run and not (token and chat_id):
        print("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in local .env or environment", file=sys.stderr)
        return 2
    if args.test_telegram:
        if args.dry_run:
            print("TEST: Telegram connection message would be sent")
            return 0
        try:
            send_telegram(token, chat_id, "✅ اتصال دیده‌بان بازار به کانال Signal برقرار شد. این پیام آزمایشی است.")
            print("Telegram test message accepted")
            return 0
        except Exception as exc:
            print(f"Telegram test failed ({type(exc).__name__}); check bot/channel permissions", file=sys.stderr)
            return 1
    try:
        watchlist = json.loads(args.watchlist.read_text(encoding="utf-8"))
        symbols = {asset: list(dict.fromkeys(str(s).strip().upper() for s in watchlist.get(asset, []) if str(s).strip()))
                   for asset in ("stocks", "crypto")}
        if not symbols["stocks"] and not symbols["crypto"]:
            raise ValueError("watchlist is empty")
        if len(symbols["stocks"] + symbols["crypto"]) > 40:
            raise ValueError("limit the watchlist to 40 symbols to reduce source throttling")
    except (OSError, ValueError, TypeError) as exc:
        print(f"Invalid watchlist: {exc}", file=sys.stderr)
        return 2
    try:
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    now = datetime.now(timezone.utc)
    state = {key: stamp for key, stamp in state.items()
             if isinstance(stamp, (int, float)) and 0 <= now.timestamp() - stamp < 7 * 86400}
             
    # Ensure state directory exists
    if args.state.parent:
        args.state.parent.mkdir(parents=True, exist_ok=True)
    if args.research_cache.parent:
        args.research_cache.parent.mkdir(parents=True, exist_ok=True)
        
    if not is_dry_run:
        try:
            atomic_json(args.state, state)
        except Exception as e:
            print(f"Warning: Failed to save state: {e}", file=sys.stderr)
    alerts = []
    rows = []
    received_any = False
    universe = list(dict.fromkeys(symbols["stocks"] + symbols["crypto"]))
    for minutes in (5, 15):
        try:
            data = fetch(universe, minutes, symbols["crypto"])
        except Exception as exc:
            print(f"{minutes}m feed error ({type(exc).__name__})", file=sys.stderr)
            continue
        received_any |= bool(data)
        print(f"{minutes}m: received {len(data)}/{len(universe)} symbols; missing: {sorted(set(universe) - set(data))}")
        for asset, names in symbols.items():
            for symbol in names:
                if symbol in data:
                    source = data[symbol].attrs.get("source", "Yahoo")
                    alerts.extend(detect(data[symbol], symbol, asset, minutes, now, source))
                    if minutes == 5:
                        row = market_row(data[symbol], symbol, asset, minutes, now, source)
                        if row is not None:
                            rows.append(row)
    if not received_any:
        print("No market data received; check Yahoo availability and ticker spelling", file=sys.stderr)
        return 1
    alerts.sort(key=lambda a: a.score, reverse=True)
    rows.sort(key=lambda r: r.volatility_score, reverse=True)
    print(f"Fresh market rows: {len(rows)}; highest volatility: {[r.symbol for r in rows[:5]]}; new alert candidates: {len(alerts)}")
    report_key = "report|" + now.astimezone(BERLIN).strftime("%Y-%m-%dT%H")
    report_due = args.report_now or args.dry_run or (now.astimezone(BERLIN).hour in {9, 12, 16, 20} and report_key not in state)
    cache = ResearchCache(args.research_cache)
    profiles = {}
    needed = {a.symbol for a in alerts[:8] if a.asset == "stocks"}
    if report_due:
        needed.update(r.symbol for r in rows if r.asset == "stocks")
    for symbol in sorted(needed):
        profiles[symbol] = cache.stock(symbol, now)
        # First uncached run is deliberately small, but avoid bursting info/news calls.
        if profiles[symbol].get("fetched_at", 0) >= now.timestamp() - 2 and len(needed) > 1:
            time.sleep(0.2)
    for alert in alerts[:8]:
        if alert.key in state:
            continue
        msg = format_alert(alert, profiles.get(alert.symbol))
        if is_dry_run:
            print(f"[DRY_RUN_ALERT_GENERATED] symbol={alert.symbol} side={alert.side} score={alert.score} reason={alert.reason}")
        else:
            try:
                send_telegram(token, chat_id, msg)
            except Exception as exc:
                # HTTP errors can include the request URL, which contains the bot token.
                print(f"Telegram send failed ({type(exc).__name__}); check bot/channel permissions", file=sys.stderr)
                return 1
            state[alert.key] = now.timestamp()
            try:
                atomic_json(args.state, state)
            except Exception as e:
                print(f"Warning: Failed to save state: {e}", file=sys.stderr)
    if report_due and rows:
        message = format_report(rows, profiles, now)
        if is_dry_run:
            print(f"[DRY_RUN_REPORT_GENERATED] {len(rows)} fresh market rows processed.")
        else:
            try:
                send_telegram(token, chat_id, message)
            except Exception as exc:
                print(f"Telegram report failed ({type(exc).__name__}); check bot/channel permissions", file=sys.stderr)
                return 1
            state[report_key] = now.timestamp()
            try:
                atomic_json(args.state, state)
            except Exception as e:
                print(f"Warning: Failed to save state: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
