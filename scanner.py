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
import activity_monitor
import signal_engine
from universe import load_instrument_map, load_universe
from mobile_signal_report import build_mobile_signal_pdf

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


def fetch_yahoo(symbols: list[str], minutes: int, batch_size: int = 50) -> dict[str, pd.DataFrame]:
    """Fetch Yahoo intraday data in bounded batches so 250+ symbol universes stay reliable."""
    import yfinance as yf

    if not symbols:
        return {}
    batch_size = max(1, min(100, int(batch_size)))
    result: dict[str, pd.DataFrame] = {}
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        try:
            data = yf.download(
                batch, period="10d", interval=f"{minutes}m", group_by="ticker",
                auto_adjust=False, threads=True, progress=False, timeout=25,
            )
        except Exception as exc:
            print(f"Yahoo batch {start // batch_size + 1} failed ({type(exc).__name__})", file=sys.stderr)
            continue
        for symbol in batch:
            try:
                part = data[symbol] if isinstance(data.columns, pd.MultiIndex) else data
                part = part.dropna(subset=["Open", "High", "Low", "Close"])
                if not part.empty:
                    part.attrs["source"] = "Yahoo"
                    result[symbol] = part
            except (KeyError, TypeError):
                continue
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


def _crypto_usd_fallback(symbol: str) -> str | None:
    """Return a Yahoo USD fallback for a canonical BASE-EUR crypto symbol."""
    try:
        base, quote = symbol.upper().split("-", 1)
    except ValueError:
        return None
    if quote != "EUR" or not base:
        return None
    return f"{base}-USD"


def fetch(symbols: list[str], minutes: int, crypto_symbols: list[str], yahoo_batch_size: int = 50) -> dict[str, pd.DataFrame]:
    """Yahoo canonical -> Yahoo USD fallback -> Kraken canonical; failures stay isolated.

    Returned keys always use the configured canonical symbol. When a fallback feed is
    used, the actual market-data ticker is recorded in DataFrame.attrs['data_symbol'].
    """
    result: dict[str, pd.DataFrame] = {}
    if symbols:
        try:
            result.update(fetch_yahoo(symbols, minutes, yahoo_batch_size))
        except Exception as exc:
            print(f"Yahoo {minutes}m unavailable ({type(exc).__name__})", file=sys.stderr)

    # Resolve missing crypto efficiently through Yahoo USD pairs in one/bounded batches.
    fallback_to_canonical: dict[str, str] = {}
    for canonical in crypto_symbols:
        if canonical in result:
            result[canonical].attrs.setdefault("data_symbol", canonical)
            continue
        fallback = _crypto_usd_fallback(canonical)
        if fallback and fallback != canonical:
            fallback_to_canonical[fallback] = canonical
    if fallback_to_canonical:
        try:
            fallback_frames = fetch_yahoo(list(fallback_to_canonical), minutes, yahoo_batch_size)
        except Exception as exc:
            print(f"Yahoo crypto fallback {minutes}m unavailable ({type(exc).__name__})", file=sys.stderr)
            fallback_frames = {}
        for data_symbol, frame in fallback_frames.items():
            canonical = fallback_to_canonical[data_symbol]
            frame.attrs["data_symbol"] = data_symbol
            frame.attrs["canonical_symbol"] = canonical
            frame.attrs["source"] = "Yahoo fallback"
            result[canonical] = frame

    # Last resort: preserve the configured EUR identity and try Kraken spot.
    for canonical in crypto_symbols:
        if canonical in result:
            continue
        try:
            frame = fetch_kraken(canonical, minutes)
            frame.attrs["data_symbol"] = canonical
            frame.attrs["canonical_symbol"] = canonical
            result[canonical] = frame
        except (OSError, ValueError, KeyError, IndexError, TimeoutError) as exc:
            print(f"SKIPPED={canonical} reason=no_feed ({type(exc).__name__})", file=sys.stderr)
    return result


def format_high_activity_message(symbol: str, asset: str, activity: dict) -> str:
    metrics = activity.get("metrics") or {}
    move = float(metrics.get("move_pct") or 0)
    move_txt = f"{move:+.2f}%"
    rel = float(metrics.get("relative_volume") or 0)
    atr = float(metrics.get("atr_percent") or 0)
    return (
        f"⚡ <b>HIGH ACTIVITY</b>\n\n"
        f"Symbol: {symbol}\n"
        f"Asset: {asset.capitalize() if asset != 'crypto' else 'Crypto'}\n\n"
        f"Activity Score: {int(activity.get('score', 0))}/100\n"
        f"5m Move: {move_txt}\n"
        f"Relative Volume: {rel:.1f}x\n"
        f"ATR: {atr:.2f}%\n\n"
        f"Status:\n"
        f"High volatility / unusual market activity detected.\n\n"
        f"⚠️ This is NOT a BUY/SELL signal.\n"
        f"Waiting for directional confirmation."
    )


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


def evaluate_fundamental_context(profile: dict | None, direction: str) -> dict:
    """Direction-relative stock fundamental confirmation. Never creates direction itself."""
    out = {"contribution": 0, "evidence": [], "conflicts": [], "label": "N/A"}
    if not profile or not profile.get("available"):
        return out
    score = float(profile.get("score") or 0)
    catalyst = float(profile.get("catalyst_score") or 0)
    grade = profile.get("grade", "UNKNOWN")
    out["label"] = f"{score:.0f}/100 {grade}; catalyst {catalyst:.0f}/100"

    if direction == "BUY":
        if score >= 65:
            out["contribution"] += 10
            out["evidence"].append("fundamental_quality_supports_buy")
        elif score <= 35:
            out["contribution"] -= 10
            out["conflicts"].append("weak_fundamentals_conflict_with_buy")
        if catalyst >= 45:
            out["contribution"] += 5
            out["evidence"].append("positive_catalyst_supports_buy")
    elif direction == "SELL":
        if score <= 35:
            out["contribution"] += 10
            out["evidence"].append("weak_fundamentals_support_sell")
        elif score >= 65:
            out["contribution"] -= 10
            out["conflicts"].append("strong_fundamentals_conflict_with_sell")
        if catalyst >= 45:
            out["conflicts"].append("positive_catalyst_conflict_with_sell")

    out["contribution"] = max(-10, min(15, out["contribution"]))
    return out


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


STRUCTURED_STATE_KEYS = frozenset({"hot_watchlist", "alert_history", "last_30m_scan", "safety_stock_cursor", "safety_crypto_cursor"})


def prepare_runtime_state(raw_state: dict, now: datetime) -> dict:
    """Expire old scalar stamps only; never wipe alert/dedup or hot-watch state."""
    if not isinstance(raw_state, dict):
        raw_state = {}

    hot = raw_state.get("hot_watchlist") if isinstance(raw_state.get("hot_watchlist"), dict) else {}
    alerts = raw_state.get("alert_history") if isinstance(raw_state.get("alert_history"), dict) else {}
    last_30m = raw_state.get("last_30m_scan")
    safety_stock_cursor = raw_state.get("safety_stock_cursor", 0)
    safety_crypto_cursor = raw_state.get("safety_crypto_cursor", 0)

    cleaned = {
        key: stamp
        for key, stamp in raw_state.items()
        if key not in STRUCTURED_STATE_KEYS
        and isinstance(stamp, (int, float))
        and 0 <= now.timestamp() - stamp < 7 * 86400
    }
    cleaned["hot_watchlist"] = hot
    cleaned["alert_history"] = alerts
    if isinstance(last_30m, (int, float)):
        cleaned["last_30m_scan"] = last_30m
    if isinstance(safety_stock_cursor, (int, float)):
        cleaned["safety_stock_cursor"] = int(safety_stock_cursor)
    if isinstance(safety_crypto_cursor, (int, float)):
        cleaned["safety_crypto_cursor"] = int(safety_crypto_cursor)
    return cleaned


def restore_alert_history(raw: object) -> dict:
    """Restore alert history safely; corrupt entries are skipped (no crash, no wipe-spam)."""
    restored: dict = {}
    if not isinstance(raw, dict):
        return restored
    for key, value in raw.items():
        try:
            if not isinstance(value, dict):
                continue
            ts = value["time"]
            signals = value["signals"]
            restored[str(key)] = {
                "time": datetime.fromtimestamp(float(ts), timezone.utc),
                "signals": list(signals),
            }
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
    return restored


def restore_hot_watchlist(raw: object, current_ts: float) -> dict:
    restored: dict = {}
    if not isinstance(raw, dict):
        return restored
    for key, value in raw.items():
        try:
            expiry = float(value)
            if expiry > current_ts:
                restored[str(key)] = datetime.fromtimestamp(expiry, timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    return restored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", type=Path, default=Path("watchlist.json"))
    parser.add_argument("--universe", type=Path, default=Path("universe.json"),
                        help="Discovery universe JSON (stocks/crypto). Falls back to --watchlist.")
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
            send_telegram(token, chat_id, "✅ Signal Scanner Telegram Test\nGitHub connection successful.")
            print("Telegram test message accepted")
            return 0
        except Exception as exc:
            print(f"Telegram test failed ({type(exc).__name__}); check bot/channel permissions", file=sys.stderr)
            return 1
    try:
        universe_path = args.universe if args.universe.exists() else args.watchlist
        symbols = load_universe(universe_path)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Invalid universe/watchlist: {exc}", file=sys.stderr)
        return 2
    # Optional broker metadata (Yahoo symbol != Trade Republic availability).
    load_instrument_map()
    settings = symbols.get("settings", {}) if isinstance(symbols.get("settings", {}), dict) else {}
    max_hot_stocks = max(1, int(settings.get("max_hot_stocks", 30)))
    max_hot_crypto = max(1, int(settings.get("max_hot_crypto", 15)))
    activity_threshold = max(0, min(100, int(settings.get("activity_threshold", 40))))
    safety_stock_batch = max(0, int(settings.get("safety_stock_batch", 50)))
    safety_crypto_batch = max(0, int(settings.get("safety_crypto_batch", 20)))
    yahoo_batch_size = max(1, min(100, int(settings.get("yahoo_batch_size", 50))))
    top_activity_limit = max(1, int(settings.get("top_activity_limit", 10)))

    try:
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        # Failed restore must stay safe: empty state, no crash, no forced spam path.
        state = {}
    now = datetime.now(timezone.utc)
    state = prepare_runtime_state(state, now)

    if args.state.parent: args.state.parent.mkdir(parents=True, exist_ok=True)
    if args.research_cache.parent: args.research_cache.parent.mkdir(parents=True, exist_ok=True)

    monitor = activity_monitor.ActivityMonitor(activity_threshold=activity_threshold)
    current_ts = now.timestamp()
    monitor.hot_watchlist = restore_hot_watchlist(state.get("hot_watchlist", {}), current_ts)
    monitor.alert_history = restore_alert_history(state.get("alert_history", {}))
    
    universe = list(dict.fromkeys(symbols["stocks"] + symbols["crypto"]))
    asset_of = {s: "crypto" for s in symbols["crypto"]}
    asset_of.update({s: "stocks" for s in symbols["stocks"]})

    data_5m: dict = {}
    try:
        data_5m = fetch(universe, 5, symbols["crypto"], yahoo_batch_size)
    except Exception as exc:
        print(f"5m feed error ({type(exc).__name__})", file=sys.stderr)

    if not data_5m:
        print("No market data received; check Yahoo availability and ticker spelling", file=sys.stderr)
        return 1

    cache = ResearchCache(args.research_cache)

    last_30m = state.get("last_30m_scan", 0)
    is_periodic_30m = (now.timestamp() - last_30m) > 1800
    if is_periodic_30m:
        state["last_30m_scan"] = now.timestamp()

    summary = {
        "crypto_universe_size": len(symbols["crypto"]),
        "stock_universe_size": len(symbols["stocks"]),
        "crypto_data_valid": 0,
        "stock_data_valid": 0,
        "stock_data_failed": 0,
        "crypto_data_failed": 0,
        "symbols_scanned": 0,
        "fresh_symbols": 0,
        "hot_crypto": 0,
        "hot_stocks": 0,
        "hot_symbols": 0,
        "full_analysis_count": 0,
        "full_stock_analysis_count": 0,
        "full_crypto_analysis_count": 0,
        "high_activity_events": 0,
        "internal_candidates": 0,
        "early_watch": 0,
        "confirmed_setups": 0,
        "telegram_eligible": 0,
        "telegram_sent": 0,
        "telegram_failed": 0,
        "telegram_dedup_blocked": 0,
        "dedup_blocked": 0,
        "stale_or_failed": 0,
        "skipped_symbols": 0,
        "periodic_30m": int(is_periodic_30m),
    }

    # ---------- Phase 1: Fast Activity Scan across discovery universe ----------
    activity_by_symbol: dict[str, dict] = {}
    crypto_ranks: list[dict] = []
    stock_ranks: list[dict] = []
    analysis_candidates: list[str] = []
    report_candidates: list[dict] = []
    unsupported_stock_symbols: list[str] = []
    unsupported_crypto_symbols: list[str] = []

    for symbol in universe:
        asset = asset_of.get(symbol, "stocks")
        try:
            if symbol not in data_5m:
                print(f"SKIPPED={symbol} reason=unsupported_or_missing")
                summary["skipped_symbols"] += 1
                summary["stale_or_failed"] += 1
                summary["crypto_data_failed" if asset == "crypto" else "stock_data_failed"] += 1
                (unsupported_crypto_symbols if asset == "crypto" else unsupported_stock_symbols).append(symbol)
                continue

            df_5m = closed_bars(data_5m[symbol], 5, now)
            if df_5m.empty or len(df_5m) < 50:
                print(f"SKIPPED={symbol} reason=stale_or_short")
                summary["skipped_symbols"] += 1
                summary["stale_or_failed"] += 1
                summary["crypto_data_failed" if asset == "crypto" else "stock_data_failed"] += 1
                continue

            summary["symbols_scanned"] += 1
            summary["fresh_symbols"] += 1
            if asset == "crypto":
                summary["crypto_data_valid"] += 1
                data_symbol = str(data_5m[symbol].attrs.get("data_symbol", symbol))
                if data_symbol != symbol:
                    print(f"CRYPTO_DATA_SYMBOL={symbol}->{data_symbol}")
            else:
                summary["stock_data_valid"] += 1

            activity = monitor.calc_activity_score(df_5m)
            activity_by_symbol[symbol] = activity
            monitor.update_hot_watchlist(symbol, activity, now)
            is_hot = monitor.is_hot(symbol)
            if is_hot:
                summary["hot_symbols"] += 1
                if asset == "crypto":
                    summary["hot_crypto"] += 1
                else:
                    summary["hot_stocks"] += 1

            row = {"symbol": symbol, "asset": asset, "score": int(activity.get("score", 0))}
            if asset == "crypto":
                crypto_ranks.append(row)
            else:
                stock_ranks.append(row)

            # HIGH ACTIVITY is discovery-only. It promotes/ranks the Hot Watchlist,
            # but it must never consume Telegram dedup state or send a message.
            if monitor.is_high_activity(activity):
                summary["high_activity_events"] += 1
                print(
                    f"HIGH_ACTIVITY_INTERNAL={symbol} score={activity.get('score')} "
                    f"rel_vol={activity.get('metrics', {}).get('relative_volume')} "
                    f"move={activity.get('metrics', {}).get('move_pct')}"
                )

            if is_hot:
                analysis_candidates.append(symbol)
        except Exception as exc:
            print(f"SKIPPED={symbol} reason={type(exc).__name__}", file=sys.stderr)
            summary["skipped_symbols"] += 1
            summary["stale_or_failed"] += 1
            summary["crypto_data_failed" if asset == "crypto" else "stock_data_failed"] += 1

    top_crypto = activity_monitor.rank_activity(crypto_ranks, limit=top_activity_limit)
    top_stocks = activity_monitor.rank_activity(stock_ranks, limit=top_activity_limit)
    print(activity_monitor.format_top_activity("TOP_CRYPTO_ACTIVITY", top_crypto))
    print(activity_monitor.format_top_activity("TOP_STOCK_ACTIVITY", top_stocks))

    # Stable priority: highest activity first. Limit expensive full analysis independently
    # from universe size, then add a rotating 30-minute safety batch so quiet symbols are
    # eventually revisited without analyzing 250+ stocks at once.
    hot_stocks = [s for s in analysis_candidates if asset_of.get(s) == "stocks"]
    hot_crypto = [s for s in analysis_candidates if asset_of.get(s) == "crypto"]
    hot_stocks.sort(key=lambda s: -int(activity_by_symbol.get(s, {}).get("score", 0)))
    hot_crypto.sort(key=lambda s: -int(activity_by_symbol.get(s, {}).get("score", 0)))
    analysis_candidates = hot_stocks[:max_hot_stocks] + hot_crypto[:max_hot_crypto]

    if is_periodic_30m:
        def rotating_batch(pool: list[str], cursor_key: str, size: int) -> list[str]:
            valid = [s for s in pool if s in activity_by_symbol]
            if not valid or size <= 0:
                return []
            start = int(state.get(cursor_key, 0) or 0) % len(valid)
            count = min(size, len(valid))
            chosen = [valid[(start + i) % len(valid)] for i in range(count)]
            state[cursor_key] = (start + count) % len(valid)
            return chosen

        analysis_candidates.extend(rotating_batch(symbols["stocks"], "safety_stock_cursor", safety_stock_batch))
        analysis_candidates.extend(rotating_batch(symbols["crypto"], "safety_crypto_cursor", safety_crypto_batch))

    analysis_candidates = list(dict.fromkeys(analysis_candidates))
    analysis_candidates.sort(key=lambda s: -int(activity_by_symbol.get(s, {}).get("score", 0)))
    print(f"FULL_ANALYSIS_PLAN stocks={sum(asset_of.get(s) == 'stocks' for s in analysis_candidates)} "
          f"crypto={sum(asset_of.get(s) == 'crypto' for s in analysis_candidates)} "
          f"periodic={int(is_periodic_30m)}")

    # ---------- Phase 2: Full Signal Analysis only for Hot / rotating safety batch ----------
    data_15m: dict = {}
    if analysis_candidates:
        try:
            data_15m = fetch(analysis_candidates, 15, [s for s in analysis_candidates if asset_of.get(s) == "crypto"], yahoo_batch_size)
        except Exception as exc:
            print(f"15m feed error ({type(exc).__name__})", file=sys.stderr)

    for symbol in analysis_candidates:
        asset = asset_of.get(symbol, "stocks")
        try:
            if symbol not in data_5m:
                continue
            df_5m = closed_bars(data_5m[symbol], 5, now)
            if df_5m.empty or len(df_5m) < 50:
                continue

            activity = activity_by_symbol.get(symbol) or monitor.calc_activity_score(df_5m)
            is_hot = monitor.is_hot(symbol)
            summary["full_analysis_count"] += 1
            summary["full_crypto_analysis_count" if asset == "crypto" else "full_stock_analysis_count"] += 1
            df_15m = closed_bars(data_15m[symbol], 15, now) if symbol in data_15m else None

            early_res = signal_engine.detect_early_watch(df_5m, df_15m)
            tech_res = signal_engine.calculate_technical_score(df_5m, df_15m)

            fund_score_contrib = 0
            fund_context = "N/A"
            fund_coverage = "N/A"
            profile = None
            if asset == "stocks":
                profile = cache.stock(symbol, now)
                if profile.get("available"):
                    fund_context = (f"Score: {profile.get('score', 0)}/100 ({profile.get('grade', 'N/A')}) | "
                                    f"Catalyst: {profile.get('catalyst_score', 0)}/100 ({profile.get('catalyst_grade', 'NONE')})")
                    fund_coverage = f"{profile.get('coverage')}%"
                else:
                    fund_context = "Fundamentals Missing"
                    fund_coverage = "0%"

            all_signals = list(set(early_res.get("signals", []) + tech_res.get("signals", [])))

            legacy_alerts = detect(data_5m[symbol], symbol, asset, 5, now)
            has_legacy_divergence = False
            legacy_diagnostics = []
            for alert in legacy_alerts:
                has_legacy_divergence = True
                legacy_diagnostics.append(f"legacy_divergence_{alert.side}")

            directional_evidence = [
                s for s in all_signals
                if any(k in s for k in ["rsi_turn", "re_entry", "divergence"]) and not s.startswith("legacy_")
            ]
            has_directional = len(directional_evidence) > 0

            bullish_evidence = [
                s for s in all_signals
                if any(k in s for k in ["bullish", "up", "buy"]) and not s.startswith("legacy_")
            ]
            bearish_evidence = [
                s for s in all_signals
                if any(k in s for k in ["bearish", "down", "sell"]) and not s.startswith("legacy_")
            ]

            signal_direction = "NEUTRAL"
            if bullish_evidence and not bearish_evidence:
                signal_direction = "BUY"
            elif bearish_evidence and not bullish_evidence:
                signal_direction = "SELL"
            elif bullish_evidence and bearish_evidence:
                signal_direction = "CONFLICTED"

            fundamental_eval = evaluate_fundamental_context(profile, signal_direction) if asset == "stocks" else {"contribution": 0, "evidence": [], "conflicts": [], "label": "N/A"}
            fund_score_contrib = fundamental_eval["contribution"]
            sig_score = max(0, min(100, tech_res["technical_score"] + fund_score_contrib))

            conflicting_evidence = []
            if signal_direction == "CONFLICTED":
                conflicting_evidence = bullish_evidence + bearish_evidence
            elif signal_direction == "BUY" and tech_res.get("trend_15m") == "Bearish":
                conflicting_evidence.append("15m Trend is Bearish while signal is BUY")
            elif signal_direction == "SELL" and tech_res.get("trend_15m") == "Bullish":
                conflicting_evidence.append("15m Trend is Bullish while signal is SELL")
            conflicting_evidence.extend(fundamental_eval.get("conflicts", []))

            trend_relation = "NEUTRAL"
            if signal_direction == "BUY":
                trend_relation = "WITH_TREND" if tech_res.get("trend_15m") == "Bullish" else "COUNTER_TREND"
            elif signal_direction == "SELL":
                trend_relation = "WITH_TREND" if tech_res.get("trend_15m") == "Bearish" else "COUNTER_TREND"

            stage = "NONE"
            telegram_eligible = False
            block_reason = "NONE"

            if early_res.get("status") == "EARLY WATCH":
                stage = "EARLY_WATCH"

            confirmation_evidence = [
                s for s in all_signals if s not in directional_evidence and not s.startswith("legacy_")
            ] + fundamental_eval.get("evidence", [])
            has_confirmation = len(confirmation_evidence) > 0

            if sig_score >= 60 and has_directional and has_confirmation:
                stage = "CONFIRMED_SETUP"

            if has_legacy_divergence and stage == "NONE":
                stage = "INTERNAL_ONLY"
            elif has_legacy_divergence and not has_directional:
                stage = "INTERNAL_ONLY"

            if stage == "NONE":
                continue

            if signal_direction in ("NEUTRAL", "CONFLICTED"):
                telegram_eligible = False
                block_reason = f"Direction is {signal_direction}"
            elif stage == "EARLY_WATCH":
                if sig_score >= 40 and has_directional:
                    telegram_eligible = True
                else:
                    block_reason = "EARLY_WATCH lacks score or direction"
            elif stage == "CONFIRMED_SETUP":
                if sig_score >= 60 and has_directional and has_confirmation:
                    if trend_relation == "COUNTER_TREND" and sig_score < 75:
                        block_reason = "COUNTER_TREND requires higher score (>=75) for CONFIRMED"
                    else:
                        telegram_eligible = True
                else:
                    block_reason = "CONFIRMED_SETUP lacks score, direction, or confirmation"
            elif stage == "INTERNAL_ONLY":
                block_reason = "INTERNAL_ONLY not eligible"

            if stage == "INTERNAL_ONLY":
                summary["internal_candidates"] += 1
            elif stage == "EARLY_WATCH":
                summary["early_watch"] += 1
            elif stage == "CONFIRMED_SETUP":
                summary["confirmed_setups"] += 1

            if telegram_eligible:
                payload = {"signals": [stage] + all_signals}
                if not monitor.should_send_alert(symbol, payload, now):
                    telegram_eligible = False
                    block_reason = "DEDUP_BLOCKED"
                    summary["dedup_blocked"] += 1
                    summary["telegram_dedup_blocked"] += 1

            if telegram_eligible:
                summary["telegram_eligible"] += 1

            print(f"SYMBOL={symbol}")
            print(f"ASSET_TYPE={asset}")
            print(f"EVENT_STAGE={stage}")
            print(f"SIGNAL_DIRECTION={signal_direction}")
            print(f"5M_TREND={tech_res.get('trend_5m', 'N/A')}")
            print(f"15M_TREND={tech_res.get('trend_15m', 'N/A')}")
            print(f"MTF_ALIGNMENT={tech_res.get('mtf_alignment', 'N/A')}")
            print(f"TREND_RELATION={trend_relation}")
            print(f"ACTIVITY_SCORE={activity['score']}")
            print(f"DIRECTION_SCORE={tech_res.get('direction_score', 0)}")
            print(f"CONFIRMATION_SCORE={tech_res.get('confirmation_score', 0)}")
            print(f"TREND_SCORE={tech_res.get('trend_score', 0)}")
            print(f"STRUCTURE_SCORE={tech_res.get('structure_score', 0)}")
            print(f"FUNDAMENTAL_SCORE_CONTRIBUTION={fund_score_contrib}")
            print(f"SIGNAL_SCORE={sig_score}")
            print(f"RSI_TURN={'YES' if any('rsi_turn' in s for s in directional_evidence) else 'NO'}")
            print(f"RSI_CONTEXT={tech_res.get('rsi', 'N/A')}")
            print(f"NEW_ENGINE_DIVERGENCE={'YES' if any('divergence' in s for s in directional_evidence) else 'NO'}")
            print(f"LEGACY_DIAGNOSTIC={legacy_diagnostics}")
            print(f"RSI_BOLLINGER={'YES' if any('rsi_re_entry' in s for s in directional_evidence) else 'NO'}")
            print(f"PRICE_BOLLINGER={'YES' if any('price_re_entry' in s for s in directional_evidence) else 'NO'}")
            print(f"EMA20_CONTEXT={'YES' if any('ema' in s for s in confirmation_evidence) else 'N/A'}")
            print(f"RELATIVE_VOLUME={tech_res.get('relative_volume', 'N/A')}")
            print(f"VOLUME_SPIKE={'YES' if any('volume_spike' in s for s in confirmation_evidence) else 'NO'}")
            print(f"ATR_PERCENT={tech_res.get('atr_percent', 'N/A')}")
            print(f"RANGE_POSITION={tech_res.get('range_position', 'N/A')}")
            print(f"MOMENTUM={tech_res.get('momentum', 'N/A')}")
            print(f"MARKET_STRUCTURE={tech_res.get('market_structure')}")
            print(f"FUNDAMENTAL_CONTEXT={fund_context}")
            print(f"FUNDAMENTAL_COVERAGE={fund_coverage}")
            print(f"FUNDAMENTAL_GRADE={profile.get('grade', 'N/A') if profile else 'N/A'}")
            print(f"CATALYST_SCORE={profile.get('catalyst_score', 'N/A') if profile else 'N/A'}")
            print(f"CATALYST_GRADE={profile.get('catalyst_grade', 'N/A') if profile else 'N/A'}")
            print(f"FUNDAMENTAL_CONFIRMATION={fundamental_eval.get('evidence', [])}")
            print(f"DIRECTIONAL_EVIDENCE={directional_evidence}")
            print(f"CONFIRMATION_EVIDENCE={confirmation_evidence}")
            print(f"CONFLICTING_EVIDENCE={conflicting_evidence}")
            print(f"HOT_WATCHLIST={is_hot}")
            print(f"FULL_ANALYSIS_REASON={'HOT' if is_hot else 'PERIODIC_30M'}")
            print(f"TELEGRAM_ELIGIBLE={telegram_eligible}")
            print(f"REASONS={all_signals}")
            print(f"BLOCK_REASON={block_reason}")
            print("---")

            # Collect compact review candidates for the hourly mobile PDF.
            # This does not change scoring, stage, or Telegram eligibility.
            if signal_direction in ("BUY", "SELL") and stage in ("EARLY_WATCH", "CONFIRMED_SETUP"):
                report_candidates.append({
                    "symbol": symbol, "asset": asset, "stage": stage, "direction": signal_direction,
                    "signal_score": sig_score, "activity_score": activity.get("score", 0),
                    "trend_5m": tech_res.get("trend_5m", "N/A"), "trend_15m": tech_res.get("trend_15m", "N/A"),
                    "trend_relation": trend_relation, "rsi": tech_res.get("rsi", "N/A"),
                    "atr_percent": tech_res.get("atr_percent", "N/A"), "relative_volume": tech_res.get("relative_volume", "N/A"),
                    "market_structure": tech_res.get("market_structure", "N/A"),
                    "fundamental": fund_context, "coverage": fund_coverage,
                    "catalyst": (profile.get("catalyst_grade", "N/A") if profile else "N/A"),
                    "reasons": all_signals, "conflicts": conflicting_evidence,
                    "closes": [float(x) for x in df_5m["Close"].tail(48).dropna().tolist()],
                })

            if telegram_eligible and not is_dry_run:
                if stage == "EARLY_WATCH":
                    stage_icon = "👀"
                    stage_text = "EARLY WATCH (Developing Setup)"
                else:
                    stage_icon = "🔥"
                    stage_text = "CONFIRMED SETUP"

                msg = (
                    f"{stage_icon} <b>{stage_text} | {symbol} | {asset}</b>\n\n"
                    f"• Direction: {signal_direction}\n"
                    f"• Score: {sig_score}/100 (Activity: {activity['score']})\n"
                    f"• 5m: {tech_res.get('trend_5m', 'N/A')} | 15m: {tech_res.get('trend_15m', 'N/A')} ({tech_res.get('mtf_alignment', 'N/A')})\n"
                    f"• Relation: {trend_relation}\n"
                    f"• Reasons: {', '.join(all_signals)}\n"
                    f"• Fundamental: {fund_context} | Coverage: {fund_coverage}\n"
                    f"• Fundamental confirmation: {', '.join(fundamental_eval.get('evidence', [])) or 'none'}\n"
                    f"• Context: {fund_context}"
                )
                try:
                    send_telegram(token, chat_id, msg)
                    summary["telegram_sent"] += 1
                except Exception as e:
                    summary["telegram_failed"] += 1
                    print(f"Telegram failed ({type(e).__name__}): {symbol}", file=sys.stderr)
        except Exception as exc:
            print(f"SKIPPED={symbol} reason=full_analysis_{type(exc).__name__}", file=sys.stderr)
            summary["skipped_symbols"] += 1

    # One compact PDF per hour, not one PDF every 5-minute scan.
    # The report is a review shortlist, never an automated trade instruction.
    if not is_dry_run and report_candidates:
        interval_minutes = max(15, int(os.getenv("PDF_REPORT_INTERVAL_MINUTES", "60")))
        last_pdf_raw = state.get("last_mobile_pdf_report")
        last_pdf = None
        try:
            last_pdf = datetime.fromisoformat(last_pdf_raw) if last_pdf_raw else None
            if last_pdf and last_pdf.tzinfo is None:
                last_pdf = last_pdf.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            last_pdf = None
        due = last_pdf is None or (now - last_pdf).total_seconds() >= interval_minutes * 60
        if due:
            try:
                report_path = str(Path(".state") / "signal_mobile_review.pdf")
                build_mobile_signal_pdf(report_path, report_candidates, now, max_items=int(os.getenv("PDF_REPORT_MAX_ITEMS", "12")))
                sent = telegram_utils.send_telegram_document(
                    report_path,
                    caption="Signal Scanner - mobile BUY/SELL review shortlist (technical + activity + fundamental context)",
                )
                print(f"MOBILE_PDF_REPORT={'SENT' if sent else 'FAILED'} candidates={len(report_candidates)}")
                if sent:
                    state["last_mobile_pdf_report"] = now.isoformat()
            except Exception as exc:
                print(f"MOBILE_PDF_REPORT=FAILED reason={type(exc).__name__}", file=sys.stderr)

    state["hot_watchlist"] = {k: v.timestamp() for k, v in monitor.hot_watchlist.items()}
    state["alert_history"] = {
        k: {"time": v["time"].timestamp(), "signals": v["signals"]}
        for k, v in monitor.alert_history.items()
    }

    if not is_dry_run:
        try:
            atomic_json(args.state, state)
        except Exception as e:
            print(f"Warning: Failed to save state: {e}", file=sys.stderr)

    print("RUN_SUMMARY")
    print(f"configured_stocks={summary['stock_universe_size']}")
    print(f"configured_crypto={summary['crypto_universe_size']}")
    print(f"valid_stock_data={summary['stock_data_valid']}")
    print(f"valid_crypto_data={summary['crypto_data_valid']}")
    print(f"unsupported_stock_symbols={','.join(unsupported_stock_symbols) if unsupported_stock_symbols else 'NONE'}")
    print(f"unsupported_crypto_symbols={','.join(unsupported_crypto_symbols) if unsupported_crypto_symbols else 'NONE'}")
    print(f"high_activity_internal={summary['high_activity_events']}")
    print(f"confirmed_setup={summary['confirmed_setups']}")
    for k, v in summary.items():
        print(f"{k}={v}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
