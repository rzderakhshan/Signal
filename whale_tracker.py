from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram_utils import send_telegram_message

load_dotenv()

API_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz/info")
DISCOVERY_URL = os.getenv("WHALE_DISCOVERY_URL", "https://hyperscan.online/whales")
STATE_PATH = Path(os.getenv("STATE_PATH", ".state/whale_state.json"))
WHALES_PATH = Path(os.getenv("WHALES_PATH", "whales.json"))

TOP_WHALES = int(os.getenv("TOP_WHALES", "10"))
DISCOVERY_CANDIDATES = int(os.getenv("DISCOVERY_CANDIDATES", "30"))
RANK_REFRESH_HOURS = float(os.getenv("RANK_REFRESH_HOURS", "6"))
MIN_ACCOUNT_VALUE = float(os.getenv("MIN_ACCOUNT_VALUE", "250000"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "1000000"))

MIN_FILL_USD = float(os.getenv("MIN_FILL_USD", "75000"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "200000"))
LOOKBACK_MS = int(os.getenv("LOOKBACK_MS", str(15 * 60 * 1000)))
BOOTSTRAP_ALERTS = os.getenv("BOOTSTRAP_ALERTS", "false").lower() == "true"

# Pending / approaching limit-order engine.
# Distance is percentage from the current market mid; proximity is NOT a time prediction.
NEW_LIMIT_MAX_DISTANCE_PCT = float(os.getenv("NEW_LIMIT_MAX_DISTANCE_PCT", "1.50"))
APPROACH_DISTANCE_PCT = float(os.getenv("APPROACH_DISTANCE_PCT", "0.50"))
CLUSTER_MAX_DISTANCE_PCT = float(os.getenv("CLUSTER_MAX_DISTANCE_PCT", "1.00"))
CLUSTER_MIN_WHALES = int(os.getenv("CLUSTER_MIN_WHALES", "2"))
CLUSTER_MIN_NOTIONAL_USD = float(os.getenv("CLUSTER_MIN_NOTIONAL_USD", "500000"))

# Consensus / signal engine
SECONDARY_MIN_SCORE = float(os.getenv("SECONDARY_MIN_SCORE", "68"))
MAIN_MIN_WHALES = int(os.getenv("MAIN_MIN_WHALES", "4"))
MAIN_MIN_WEIGHTED_SHARE = float(os.getenv("MAIN_MIN_WEIGHTED_SHARE", "0.68"))
MAIN_MIN_NOTIONAL_USD = float(os.getenv("MAIN_MIN_NOTIONAL_USD", "5000000"))
STRONG_MIN_WHALES = int(os.getenv("STRONG_MIN_WHALES", "6"))
STRONG_MIN_WEIGHTED_SHARE = float(os.getenv("STRONG_MIN_WEIGHTED_SHARE", "0.78"))
MAX_SINGLE_WHALE_SHARE = float(os.getenv("MAX_SINGLE_WHALE_SHARE", "0.50"))

# Telegram delivery queue: main signals first, secondary alerts bundled to avoid 429 bursts.
TELEGRAM_BUNDLE_MAX_CHARS = int(os.getenv("TELEGRAM_BUNDLE_MAX_CHARS", "3600"))
_PENDING_ALERTS: list[tuple[int, str]] = []


def queue_alert(text: str, priority: int = 50):
    if text:
        _PENDING_ALERTS.append((priority, text))


def flush_alerts():
    if not _PENDING_ALERTS:
        return
    pending = sorted(_PENDING_ALERTS, key=lambda x: x[0], reverse=True)
    _PENDING_ALERTS.clear()

    # Keep main signals visually separate. Bundle lower-priority events so a busy
    # five-minute window produces only a small number of Telegram messages.
    secondary: list[str] = []
    for priority, text in pending:
        if priority >= 90:
            send_telegram_message(text)
        else:
            secondary.append(text)

    if secondary:
        header = "📡 <b>WHALE ACTIVITY DIGEST</b>\n\n"
        chunks: list[str] = []
        current = header
        for msg in secondary:
            piece = msg + "\n\n────────────\n\n"
            if len(current) + len(piece) > TELEGRAM_BUNDLE_MAX_CHARS and current != header:
                chunks.append(current.rstrip("\n─"))
                current = header + piece
            else:
                current += piece
        if current != header:
            chunks.append(current.rstrip("\n─"))
        for chunk in chunks:
            send_telegram_message(chunk)


@dataclass(frozen=True)
class Whale:
    name: str
    address: str
    quality: float = 50.0
    rank: int = 999


class HyperliquidClient:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "HyperliquidWhaleTracker/7.0"})

    def _post(self, payload: dict[str, Any]):
        last_error = None
        for attempt in range(3):
            try:
                r = self.session.post(API_URL, json=payload, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1 + attempt * 2)
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"Hyperliquid request failed: {last_error}")

    def fills(self, address: str, start_ms: int, end_ms: int):
        return self._post({
            "type": "userFillsByTime", "user": address,
            "startTime": start_ms, "endTime": end_ms, "aggregateByTime": True,
        })

    def open_orders(self, address: str):
        return self._post({"type": "openOrders", "user": address})

    def clearinghouse(self, address: str):
        return self._post({"type": "clearinghouseState", "user": address})

    def portfolio(self, address: str):
        return self._post({"type": "portfolio", "user": address})

    def all_mids(self):
        return self._post({"type": "allMids"})


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def money(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000_000:
        return f"{sign}${x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"{sign}${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{sign}${x/1_000:.1f}K"
    return f"{sign}${x:.2f}"


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def load_seed_whales() -> list[Whale]:
    if not WHALES_PATH.exists():
        return []
    raw = json.loads(WHALES_PATH.read_text(encoding="utf-8"))
    out = []
    for i, item in enumerate(raw):
        addr = str(item["address"]).lower().strip()
        if addr.startswith("0x") and len(addr) == 42:
            out.append(Whale(item.get("name", short_addr(addr)), addr, fnum(item.get("quality"), 50), i + 1))
    return out


def load_state() -> dict[str, Any]:
    blank = {
        "version": 7, "initialized": False, "last_run_ms": 0,
        "whales": {}, "consensus": {}, "ranking": {}, "ranking_updated_ms": 0,
        "signals": {"active_main": {}, "active_whale": {}, "pending_limits": {}, "limit_clusters": {}, "history": [], "daily_seq": {}},
    }
    if not STATE_PATH.exists():
        return blank
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        for k, v in blank.items():
            data.setdefault(k, v)
        return data
    except Exception:
        logging.exception("State file unreadable; starting with empty state")
        return blank


def save_state(state: dict[str, Any]):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def ensure_signal_state(state: dict[str, Any]) -> dict[str, Any]:
    signals = state.setdefault("signals", {})
    signals.setdefault("active_main", {})
    signals.setdefault("active_whale", {})
    signals.setdefault("pending_limits", {})
    signals.setdefault("limit_clusters", {})
    signals.setdefault("history", [])
    signals.setdefault("daily_seq", {})
    return signals


def next_signal_id(state: dict[str, Any], prefix: str, coin: str, side: str, now_ms: int) -> str:
    signals = ensure_signal_state(state)
    day = time.strftime("%Y%m%d", time.gmtime(now_ms / 1000))
    seqs = signals["daily_seq"]
    seqs[day] = int(seqs.get(day, 0)) + 1
    safe_coin = re.sub(r"[^A-Za-z0-9]", "", str(coin).upper())[:12] or "COIN"
    return f"{prefix}-{safe_coin}-{side}-{day}-{seqs[day]:03d}"


def close_result_pct(side: str, open_price: float, close_price: float) -> float:
    if open_price <= 0 or close_price <= 0:
        return 0.0
    move = (close_price - open_price) / open_price * 100.0
    return move if side == "LONG" else -move


def duration_text(opened_ms: int, closed_ms: int) -> str:
    seconds = max(0, int((closed_ms - opened_ms) / 1000))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def archive_signal(signals: dict[str, Any], sig: dict[str, Any]):
    history = signals.setdefault("history", [])
    history.append(sig)
    signals["history"] = history[-500:]


def positions_from_state(ch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ap in ch.get("assetPositions", []):
        p = ap.get("position", {})
        coin = p.get("coin")
        szi = fnum(p.get("szi"))
        if not coin or abs(szi) <= 0:
            continue
        out[str(coin)] = {
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "entry": fnum(p.get("entryPx")),
            "value": abs(fnum(p.get("positionValue"))),
            "upnl": fnum(p.get("unrealizedPnl")),
            "liq": fnum(p.get("liquidationPx")),
            "leverage": (p.get("leverage") or {}).get("value"),
        }
    return out


def account_value(ch: dict[str, Any]) -> float:
    return fnum((ch.get("marginSummary") or {}).get("accountValue"))


def total_position_value(ch: dict[str, Any]) -> float:
    return sum(p["value"] for p in positions_from_state(ch).values())


def portfolio_bucket(portfolio: list[Any], name: str) -> dict[str, Any]:
    for row in portfolio or []:
        if isinstance(row, list) and len(row) >= 2 and row[0] == name:
            return row[1] or {}
    return {}


def last_pnl(bucket: dict[str, Any]) -> float:
    h = bucket.get("pnlHistory") or []
    if not h:
        return 0.0
    try:
        return float(h[-1][1])
    except Exception:
        return 0.0


def score_whale(ch: dict[str, Any], portfolio: list[Any]) -> tuple[float, dict[str, float]]:
    """Quality score 0..100 using public Hyperliquid account + PnL history."""
    av = account_value(ch)
    pos = total_position_value(ch)
    month = last_pnl(portfolio_bucket(portfolio, "perpMonth"))
    alltime = last_pnl(portfolio_bucket(portfolio, "perpAllTime"))
    week = last_pnl(portfolio_bucket(portfolio, "perpWeek"))

    # Return-like components are bounded so one huge wallet cannot dominate.
    month_roi = month / max(av, 100_000)
    all_roi = alltime / max(av, 100_000)
    week_roi = week / max(av, 100_000)
    leverage = pos / max(av, 1.0)

    profitability = 45 * (0.5 + 0.5 * math.tanh(month_roi * 1.8))
    consistency = 20 * (0.5 + 0.5 * math.tanh(all_roi * 0.7))
    recent = 15 * (0.5 + 0.5 * math.tanh(week_roi * 2.0))
    size_score = 10 * clamp(math.log10(max(pos, 1.0) / 1_000_000 + 1) / 2.0, 0, 1)
    account_score = 10 * clamp(math.log10(max(av, 1.0) / 250_000 + 1) / 2.0, 0, 1)

    # Penalize extreme effective leverage and negative month/all-time PnL.
    penalty = 0.0
    if leverage > 20:
        penalty += 8
    elif leverage > 12:
        penalty += 4
    if month < 0:
        penalty += min(12, abs(month_roi) * 12)
    if alltime < 0:
        penalty += min(8, abs(all_roi) * 5)

    score = clamp(profitability + consistency + recent + size_score + account_score - penalty, 0, 100)
    return score, {
        "account_value": av, "position_value": pos, "month_pnl": month,
        "alltime_pnl": alltime, "week_pnl": week, "effective_leverage": leverage,
    }


def discover_addresses(seed: list[Whale]) -> list[str]:
    """Discover current directional whales from HyperScan; seeds are always fallback candidates."""
    addresses = [w.address for w in seed]
    try:
        r = requests.get(DISCOVERY_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0 WhaleTracker/7.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        found: list[str] = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"/wallets/(0x[a-fA-F0-9]{40})", a.get("href", ""))
            if m:
                addr = m.group(1).lower()
                if addr not in found:
                    found.append(addr)
        # Fallback regex for embedded app data / JSON.
        if not found:
            found = list(dict.fromkeys(x.lower() for x in re.findall(r"0x[a-fA-F0-9]{40}", r.text)))
        addresses = found[:DISCOVERY_CANDIDATES] + addresses
    except Exception:
        logging.exception("Whale discovery failed; using seed/current ranking fallback")
    return list(dict.fromkeys(addresses))[: max(DISCOVERY_CANDIDATES, TOP_WHALES)]


def refresh_ranking(client: HyperliquidClient, state: dict[str, Any], force: bool = False) -> list[Whale]:
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - int(state.get("ranking_updated_ms", 0))
    if not force and state.get("ranking") and age_ms < RANK_REFRESH_HOURS * 3600 * 1000:
        ranked = state["ranking"].get("selected", [])
        if ranked:
            return [Whale(x["name"], x["address"], fnum(x.get("quality")), int(x.get("rank", 999))) for x in ranked]

    seed = load_seed_whales()
    candidates = discover_addresses(seed)
    rows: list[dict[str, Any]] = []
    for addr in candidates:
        try:
            ch = client.clearinghouse(addr)
            av = account_value(ch)
            pos = total_position_value(ch)
            if av < MIN_ACCOUNT_VALUE or pos < MIN_POSITION_USD:
                continue
            pf = client.portfolio(addr)
            score, metrics = score_whale(ch, pf)
            rows.append({"address": addr, "quality": score, **metrics})
            time.sleep(0.03)
        except Exception:
            logging.exception("Ranking failed for %s", addr)

    # Prefer positive recent performance; score already contains penalties.
    rows.sort(key=lambda x: (x["quality"], x["month_pnl"], x["alltime_pnl"]), reverse=True)
    selected = []
    seed_names = {w.address: w.name for w in seed}
    for rank, row in enumerate(rows[:TOP_WHALES], 1):
        name = seed_names.get(row["address"], f"Whale-{rank:02d}")
        selected.append({"name": name, "rank": rank, **row})

    if not selected:
        # Fail safe: never stop monitoring merely because discovery/ranking failed.
        fallback = seed[:TOP_WHALES]
        selected = [{"name": w.name, "address": w.address, "quality": w.quality, "rank": i + 1}
                    for i, w in enumerate(fallback)]

    old_addresses = [x.get("address") for x in state.get("ranking", {}).get("selected", [])]
    new_addresses = [x.get("address") for x in selected]
    changed = old_addresses and old_addresses != new_addresses
    state["ranking"] = {"selected": selected, "candidates_scored": len(rows)}
    state["ranking_updated_ms"] = now_ms

    if changed:
        entrants = [x for x in selected if x["address"] not in old_addresses]
        exits = [a for a in old_addresses if a not in new_addresses]
        parts = ["🔄 <b>WHALE RANKING UPDATED</b>", ""]
        if entrants:
            parts.append("New Top-10:")
            for x in entrants[:5]:
                parts.append(f"• #{x['rank']} <code>{short_addr(x['address'])}</code> quality <b>{x['quality']:.0f}/100</b>")
        if exits:
            parts.append("\nRemoved:")
            for a in exits[:5]:
                parts.append(f"• <code>{short_addr(a)}</code>")
        queue_alert("\n".join(parts), priority=80)

    return [Whale(x["name"], x["address"], fnum(x.get("quality")), int(x.get("rank", 999))) for x in selected]


def fill_key(fill: dict[str, Any]) -> str:
    return str(fill.get("tid") or f"{fill.get('hash')}:{fill.get('oid')}:{fill.get('time')}:{fill.get('coin')}:{fill.get('px')}:{fill.get('sz')}")


def order_key(order: dict[str, Any]) -> str:
    return str(order.get("oid"))


def action_label(fill: dict[str, Any]) -> tuple[str, str, str]:
    direction = str(fill.get("dir", "Fill")).strip()
    d = direction.lower()
    if "open long" in d:
        return "NEW LONG", "🟢", "LONG"
    if "open short" in d:
        return "NEW SHORT", "🔴", "SHORT"
    if "close long" in d:
        return "REDUCE/CLOSE LONG", "🟠", "LONG"
    if "close short" in d:
        return "REDUCE/CLOSE SHORT", "🟠", "SHORT"
    side = "LONG" if str(fill.get("side")) == "B" else "SHORT"
    return direction.upper(), "🟢" if side == "LONG" else "🔴", side


def event_score(whale: Whale, notional: float, pos: dict[str, Any] | None, action: str) -> float:
    q = whale.quality
    size_strength = 100 * clamp(math.log10(max(notional, 1) / 50_000 + 1) / 2.1, 0, 1)
    conviction = 50.0
    if pos:
        conviction = 100 * clamp(math.log10(max(pos.get("value", 0.0), 1) / 500_000 + 1) / 2.0, 0, 1)
    action_bonus = 100 if action.startswith("NEW") else 80 if "ADD" in action else 55
    return clamp(0.58 * q + 0.22 * size_strength + 0.12 * conviction + 0.08 * action_bonus, 0, 100)


def signal_grade(score: float) -> str:
    if score >= 88:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 72:
        return "B"
    return "C"


def format_fill(whale: Whale, fill: dict[str, Any], pos: dict[str, Any] | None, score: float) -> str:
    coin = html.escape(str(fill.get("coin", "?")))
    action, emoji, _ = action_label(fill)
    px = fnum(fill.get("px"))
    sz = abs(fnum(fill.get("sz")))
    notional = px * sz
    grade = signal_grade(score)
    lines = [
        "🐋 <b>WHALE SIGNAL</b>", "",
        f"Grade: <b>{grade}</b> · Signal score: <b>{score:.0f}/100</b>",
        f"Whale rank: <b>#{whale.rank}</b> · Quality: <b>{whale.quality:.0f}/100</b>",
        f"<b>{html.escape(whale.name)}</b>  <code>{short_addr(whale.address)}</code>",
        f"{emoji} <b>{html.escape(action)}</b> — <b>{coin}</b>", "",
        f"Executed: <b>{money(notional)}</b>", f"Price: <b>{px:,.6g}</b>", f"Size: <b>{sz:,.6g}</b>",
    ]
    if pos:
        lines.extend([
            f"Position: <b>{money(pos['value'])}</b> {pos['side']}", f"Entry: <b>{pos['entry']:,.6g}</b>",
            f"uPnL: <b>{money(pos['upnl'])}</b>",
        ])
        if pos.get("leverage"):
            lines.append(f"Leverage: <b>{pos.get('leverage')}x</b>")
        if pos.get("liq"):
            lines.append(f"Liquidation: <b>{pos['liq']:,.6g}</b>")
    lines.extend(["", f"<a href=\"https://app.hyperliquid.xyz/explorer/address/{whale.address}\">View wallet ↗</a>"])
    return "\n".join(lines)



def order_side(order: dict[str, Any]) -> tuple[str, str]:
    """Return display side and directional signal side."""
    if str(order.get("side")) == "B":
        return "BUY", "LONG"
    return "SELL", "SHORT"


def distance_pct(limit_px: float, market_px: float) -> float:
    if limit_px <= 0 or market_px <= 0:
        return 999.0
    return abs(limit_px - market_px) / market_px * 100.0


def format_pending_limit(
    whale: Whale,
    order: dict[str, Any],
    signal_id: str,
    market_px: float,
    score: float,
    lifecycle: str,
) -> str:
    coin = html.escape(str(order.get("coin", "?")))
    px = fnum(order.get("limitPx"))
    sz = abs(fnum(order.get("sz")))
    ntl = px * sz
    side, direction = order_side(order)
    dist = distance_pct(px, market_px)

    if lifecycle == "NEW":
        title = "🟡 <b>PENDING WHALE SIGNAL</b>"
        fa = "توضیح: سفارش لیمیت جدید و نزدیک قیمت فعلی است؛ هنوز معامله فعال نشده است."
    else:
        title = "🟠 <b>LIMIT APPROACHING</b>"
        fa = "توضیح: این سفارش قبلاً وجود داشته اما حالا قیمت به محدوده فعال‌شدن آن نزدیک شده است. نزدیکی قیمت به معنی تضمین زمان اجرا نیست."

    return "\n".join([
        title, "",
        f"ID: <code>{html.escape(signal_id)}</code>",
        f"Grade: <b>{signal_grade(score)}</b> · Score: <b>{score:.0f}/100</b>",
        f"Whale: <b>#{whale.rank}</b> · quality <b>{whale.quality:.0f}/100</b> · <code>{short_addr(whale.address)}</code>",
        f"Coin: <b>{coin}</b> · Direction: <b>{direction}</b>",
        f"Limit: <b>{px:,.6g}</b>",
        f"Current: <b>{market_px:,.6g}</b>",
        f"Distance: <b>{dist:.2f}%</b>",
        f"Order value: <b>{money(ntl)}</b>",
        f"Order ID: <code>{order.get('oid')}</code>",
        "",
        fa,
    ])


def format_limit_activated(
    whale: Whale,
    pending: dict[str, Any],
    fill: dict[str, Any],
    pos: dict[str, Any] | None,
) -> str:
    px = fnum(fill.get("px"))
    coin = html.escape(str(fill.get("coin", pending.get("coin", "?"))))
    crossed = bool(fill.get("crossed"))
    execution = "MARKET/TAKER" if crossed else "LIMIT/MAKER FILL"
    lines = [
        "✅ <b>PENDING SIGNAL ACTIVATED</b>", "",
        f"ID: <code>{html.escape(str(pending.get('id')))}</code>",
        f"Whale: <b>#{whale.rank}</b> · <code>{short_addr(whale.address)}</code>",
        f"Coin: <b>{coin}</b> · Direction: <b>{pending.get('side')}</b>",
        f"Filled at: <b>{px:,.6g}</b>",
        f"Execution: <b>{execution}</b>",
    ]
    if pos:
        lines.extend([
            f"Whale position entry: <b>{fnum(pos.get('entry')):,.6g}</b>",
            f"Position: <b>{money(fnum(pos.get('value')))}</b>",
        ])
    lines.extend([
        "",
        "توضیح: سفارش منتظر اجرا شده و از حالت Pending به معامله فعال تبدیل شده است.",
    ])
    return "\n".join(lines)


def format_market_entry_fa(
    whale: Whale,
    fill: dict[str, Any],
    pos: dict[str, Any] | None,
    score: float,
) -> str:
    base = format_fill(whale, fill, pos, score)
    crossed = bool(fill.get("crossed"))
    if crossed:
        note = "توضیح: این Fill به‌صورت Taker/Market-like اجرا شده؛ یعنی ورود بالفعل انجام شده و سفارش منتظر نیست."
    else:
        note = "توضیح: این معامله اجرا شده است؛ داده Fill نشان می‌دهد ورود بالفعل انجام شده، هرچند می‌تواند اجرای سفارش Limit/Maker باشد."
    return base + "\n\n" + note


def build_limit_clusters(
    nearby_orders: list[dict[str, Any]],
    mids: dict[str, float],
) -> dict[str, dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in nearby_orders:
        buckets.setdefault((row["coin"], row["side"]), []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for (coin, side), rows in buckets.items():
        unique_whales = {r["whale"].address for r in rows}
        total = sum(r["notional"] for r in rows)
        if len(unique_whales) < CLUSTER_MIN_WHALES or total < CLUSTER_MIN_NOTIONAL_USD:
            continue
        prices = [r["limit_px"] for r in rows]
        market = mids.get(coin, 0.0)
        key = f"{coin}:{side}"
        out[key] = {
            "coin": coin,
            "side": side,
            "count": len(unique_whales),
            "orders": len(rows),
            "notional": total,
            "min_px": min(prices),
            "max_px": max(prices),
            "market_px": market,
            "whales": sorted(unique_whales),
        }
    return out


def format_limit_cluster(c: dict[str, Any]) -> str:
    return "\n".join([
        "🔥 <b>WHALE LIMIT CLUSTER</b>", "",
        f"Coin: <b>{html.escape(str(c['coin']))}</b> · Direction: <b>{c['side']}</b>",
        f"Whales: <b>{c['count']}</b> · Orders: <b>{c['orders']}</b>",
        f"Cluster zone: <b>{c['min_px']:,.6g} – {c['max_px']:,.6g}</b>",
        f"Current: <b>{c['market_px']:,.6g}</b>",
        f"Combined orders: <b>{money(c['notional'])}</b>",
        "",
        "توضیح: چند نهنگ در یک محدوده نزدیک به قیمت فعلی سفارش هم‌جهت دارند. این ناحیه می‌تواند محدوده مهم فعال‌شدن سفارش‌ها باشد؛ تضمین حمایت/مقاومت یا زمان اجرا نیست.",
    ])


def format_order(whale: Whale, order: dict[str, Any], status: str, score: float) -> str:
    coin = html.escape(str(order.get("coin", "?")))
    px = fnum(order.get("limitPx")); sz = abs(fnum(order.get("sz"))); ntl = px * sz
    side = "BUY" if order.get("side") == "B" else "SELL"
    title = "NEW LIMIT ORDER" if status == "NEW" else "LIMIT ORDER REMOVED/FILLED"
    return "\n".join([
        "📌 <b>WHALE ORDER SIGNAL</b>", "",
        f"Grade: <b>{signal_grade(score)}</b> · Score: <b>{score:.0f}/100</b>",
        f"Whale rank: <b>#{whale.rank}</b> · Quality: <b>{whale.quality:.0f}/100</b>",
        f"<b>{html.escape(whale.name)}</b>  <code>{short_addr(whale.address)}</code>",
        f"{'🟦' if side == 'BUY' else '🟥'} <b>{title}</b> — <b>{coin}</b>", "",
        f"Side: <b>{side}</b>", f"Limit price: <b>{px:,.6g}</b>", f"Order value: <b>{money(ntl)}</b>",
        f"Size: <b>{sz:,.6g}</b>", f"Order ID: <code>{order.get('oid')}</code>",
    ])


def build_consensus(whales: list[Whale], all_positions: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    qmap = {w.address: max(1.0, w.quality) for w in whales}
    buckets: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    totals_by_coin: dict[str, float] = {}
    for addr, positions in all_positions.items():
        for coin, pos in positions.items():
            weighted = pos["value"] * qmap.get(addr, 50.0) / 100.0
            totals_by_coin[coin] = totals_by_coin.get(coin, 0.0) + weighted
            buckets.setdefault((coin, pos["side"]), []).append((addr, pos))

    result: dict[str, dict[str, Any]] = {}
    for (coin, side), rows in buckets.items():
        weighted_side = sum(p["value"] * qmap.get(addr, 50.0) / 100.0 for addr, p in rows)
        total = max(totals_by_coin.get(coin, 0.0), 1.0)
        share = weighted_side / total
        raw_notional = sum(p["value"] for _, p in rows)
        largest = max((p["value"] for _, p in rows), default=0.0)
        largest_share = largest / max(raw_notional, 1.0)
        quality_avg = sum(qmap.get(addr, 50.0) for addr, _ in rows) / max(len(rows), 1)
        score = clamp(45 * share + 30 * (len(rows) / max(len(whales), 1)) + 25 * (quality_avg / 100.0), 0, 100)
        avg_entry = sum(p.get("entry", 0.0) * p["value"] for _, p in rows if p.get("entry", 0.0) > 0) / max(
            sum(p["value"] for _, p in rows if p.get("entry", 0.0) > 0), 1.0
        )
        whale_entries = []
        for addr, p in rows:
            whale = next((w for w in whales if w.address == addr), None)
            whale_entries.append({
                "address": addr,
                "rank": whale.rank if whale else 999,
                "quality": whale.quality if whale else qmap.get(addr, 50.0),
                "entry": fnum(p.get("entry")),
                "value": fnum(p.get("value")),
                "upnl": fnum(p.get("upnl")),
                "leverage": p.get("leverage"),
            })
        whale_entries.sort(key=lambda x: (x["rank"], -x["value"]))
        result[f"{coin}:{side}"] = {
            "coin": coin, "side": side, "count": len(rows), "addresses": [x[0] for x in rows],
            "notional": raw_notional, "weighted_share": share, "largest_share": largest_share,
            "quality_avg": quality_avg, "score": score, "avg_entry": avg_entry,
            "whale_entries": whale_entries,
        }
    return result


def qualifies_main(c: dict[str, Any]) -> bool:
    return (
        c["count"] >= MAIN_MIN_WHALES and c["weighted_share"] >= MAIN_MIN_WEIGHTED_SHARE
        and c["notional"] >= MAIN_MIN_NOTIONAL_USD and c["largest_share"] <= MAX_SINGLE_WHALE_SHARE
    )


def consensus_grade(c: dict[str, Any]) -> str:
    if c["count"] >= STRONG_MIN_WHALES and c["weighted_share"] >= STRONG_MIN_WEIGHTED_SHARE:
        return "A+ MAIN"
    return "A MAIN"


def format_main_open(c: dict[str, Any], whales: list[Whale], signal_id: str, open_price: float) -> str:
    wm = {w.address: w for w in whales}
    emoji = "🟢" if c["side"] == "LONG" else "🔴"
    lines = [
        f"🚀 <b>OPEN MAIN SIGNAL — {consensus_grade(c)}</b>",
        f"ID: <code>{html.escape(signal_id)}</code>",
        "",
        f"{emoji} <b>{html.escape(c['coin'])} {c['side']}</b>",
        f"Signal Entry: <b>{open_price:,.6g}</b>",
        f"Average Whale Entry: <b>{fnum(c.get('avg_entry')):,.6g}</b>",
        f"Aligned whales: <b>{c['count']}/{len(whales)}</b>",
        f"Weighted consensus: <b>{c['weighted_share']*100:.0f}%</b>",
        f"Consensus score: <b>{c['score']:.0f}/100</b>",
        f"Combined position: <b>{money(c['notional'])}</b>",
        f"Average whale quality: <b>{c['quality_avg']:.0f}/100</b>",
        "",
        "<b>Whale entries:</b>",
    ]
    entries = c.get("whale_entries") or []
    for row in entries:
        addr = row.get("address", "")
        rank = row.get("rank", wm.get(addr).rank if addr in wm else 999)
        entry = fnum(row.get("entry"))
        pos_value = fnum(row.get("value"))
        q = fnum(row.get("quality"))
        lev = row.get("leverage")
        lev_text = f" · {lev}x" if lev else ""
        lines.append(
            f"• #{rank} <code>{short_addr(addr)}</code> · Entry <b>{entry:,.6g}</b> · Position <b>{money(pos_value)}</b> · Q {q:.0f}{lev_text}"
        )
    return "\n".join(lines)

def format_main_update(c: dict[str, Any], whales: list[Whale], sig: dict[str, Any]) -> str:
    emoji = "🟢" if c["side"] == "LONG" else "🔴"
    lines = [
        "🟠 <b>UPDATE MAIN SIGNAL</b>",
        f"ID: <code>{html.escape(sig['id'])}</code>",
        "",
        f"{emoji} <b>{html.escape(c['coin'])} {c['side']}</b>",
        f"Signal Entry: <b>{fnum(sig.get('open_price')):,.6g}</b>",
        f"Current Avg Whale Entry: <b>{fnum(c.get('avg_entry')):,.6g}</b>",
        f"Whales: <b>{sig.get('last_count', sig.get('open_count', 0))} → {c['count']}/{len(whales)}</b>",
        f"Weighted consensus: <b>{sig.get('last_share', sig.get('open_share', 0))*100:.0f}% → {c['weighted_share']*100:.0f}%</b>",
        f"Score: <b>{sig.get('last_score', sig.get('open_score', 0)):.0f} → {c['score']:.0f}/100</b>",
        f"Combined position: <b>{money(c['notional'])}</b>",
    ]
    entries = c.get("whale_entries") or []
    if entries:
        lines.extend(["", "<b>Current whale entries:</b>"])
        for row in entries:
            lines.append(
                f"• #{row.get('rank', 999)} <code>{short_addr(row.get('address',''))}</code> · Entry <b>{fnum(row.get('entry')):,.6g}</b> · Position <b>{money(fnum(row.get('value')))}</b>"
            )
    return "\n".join(lines)

def format_signal_close(sig: dict[str, Any], close_price: float, closed_ms: int, reason: str, label: str = "MAIN") -> str:
    pct = close_result_pct(sig["side"], fnum(sig.get("open_price")), close_price)
    emoji = "✅" if pct >= 0 else "❌"
    return (
        f"🔴 <b>CLOSE {label} SIGNAL</b>\n"
        f"ID: <code>{html.escape(sig['id'])}</code>\n\n"
        f"<b>{html.escape(sig['coin'])} {sig['side']}</b>\n"
        f"Signal Entry: <b>{fnum(sig.get('open_price')):,.6g}</b>\n"
        + (f"Avg Whale Entry at Open: <b>{fnum(sig.get('open_avg_whale_entry')):,.6g}</b>\n" if fnum(sig.get('open_avg_whale_entry')) > 0 else "")
        + f"Close: <b>{close_price:,.6g}</b>\n"
        f"{emoji} Result: <b>{pct:+.2f}%</b>\n"
        f"Duration: <b>{duration_text(int(sig.get('opened_ms', closed_ms)), closed_ms)}</b>\n"
        f"Reason: <b>{html.escape(reason)}</b>"
    )


def format_whale_lifecycle(whale: Whale, fill: dict[str, Any], pos: dict[str, Any] | None, score: float, signal_id: str, lifecycle: str) -> str:
    base = format_fill(whale, fill, pos, score)
    title = {"OPEN": "🚀 <b>OPEN WHALE SIGNAL</b>", "UPDATE": "🟠 <b>UPDATE WHALE SIGNAL</b>"}.get(lifecycle, "🐋 <b>WHALE SIGNAL</b>")
    lines = base.splitlines()
    if lines:
        lines[0] = title
    lines.insert(1, f"ID: <code>{html.escape(signal_id)}</code>")
    return "\n".join(lines)


def run_once(test_telegram: bool = False, refresh_ranking_now: bool = False):
    if test_telegram:
        ok = send_telegram_message("✅ <b>Hyperliquid Whale Tracker v7</b>\nSignal lifecycle + whale entry prices + Telegram delivery are working.")
        raise SystemExit(0 if ok else 2)

    state = load_state()
    signals = ensure_signal_state(state)
    client = HyperliquidClient()
    whales = refresh_ranking(client, state, force=refresh_ranking_now)
    now_ms = int(time.time() * 1000)
    first_run = not state.get("initialized", False)
    start_ms = max(now_ms - LOOKBACK_MS, int(state.get("last_run_ms", 0)) - 3000)

    try:
        mids_raw = client.all_mids() or {}
        mids = {str(k): fnum(v) for k, v in mids_raw.items()}
    except Exception:
        logging.exception("Could not load market mids; lifecycle will use consensus entry fallback")
        mids = {}

    all_positions: dict[str, dict[str, dict[str, Any]]] = {}
    event_coins: set[str] = set()
    nearby_limit_orders: list[dict[str, Any]] = []
    current_whale_addresses = {w.address for w in whales}

    for whale in whales:
        ws = state.setdefault("whales", {}).setdefault(whale.address, {"fills": [], "orders": {}, "positions": {}})
        try:
            previous_positions = ws.get("positions", {}) or {}
            ch = client.clearinghouse(whale.address)
            positions = positions_from_state(ch)
            all_positions[whale.address] = positions

            fills = client.fills(whale.address, start_ms, now_ms)
            known_fills = set(ws.get("fills", []))
            current_fill_keys: list[str] = []
            new_fills: list[dict[str, Any]] = []
            for fill in fills:
                key = fill_key(fill)
                current_fill_keys.append(key)
                if key not in known_fills:
                    new_fills.append(fill)
            new_fills.sort(key=lambda x: int(x.get("time", 0)))

            orders = client.open_orders(whale.address)
            current_orders = {order_key(o): o for o in orders}
            previous_orders = ws.get("orders", {}) or {}
            should_alert = (not first_run) or BOOTSTRAP_ALERTS

            # Track qualifying nearby orders on every run so old orders can become relevant later.
            for oid, order in current_orders.items():
                coin_o = str(order.get("coin"))
                limit_px = fnum(order.get("limitPx"))
                sz_o = abs(fnum(order.get("sz")))
                notional_o = abs(limit_px * sz_o)
                market_o = mids.get(coin_o, 0.0)
                side_display, side_signal = order_side(order)
                dist_o = distance_pct(limit_px, market_o)

                if notional_o >= MIN_ORDER_USD and dist_o <= CLUSTER_MAX_DISTANCE_PCT:
                    nearby_limit_orders.append({
                        "whale": whale, "oid": oid, "coin": coin_o, "side": side_signal,
                        "limit_px": limit_px, "market_px": market_o, "notional": notional_o,
                    })

                pending_key = f"{whale.address}:{oid}"
                pending = signals["pending_limits"].get(pending_key)
                is_new_order = oid not in previous_orders

                if is_new_order and notional_o >= MIN_ORDER_USD and dist_o <= NEW_LIMIT_MAX_DISTANCE_PCT:
                    score_o = event_score(whale, notional_o, positions.get(coin_o), "NEW ORDER")
                    if score_o >= SECONDARY_MIN_SCORE:
                        sid = next_signal_id(state, "PEND", coin_o, side_signal, now_ms)
                        pending = {
                            "id": sid, "type": "PENDING_LIMIT", "coin": coin_o, "side": side_signal,
                            "whale": whale.address, "oid": oid, "limit_price": limit_px,
                            "order_value": notional_o, "created_ms": now_ms, "status": "PENDING",
                            "approach_alerted": dist_o <= APPROACH_DISTANCE_PCT,
                        }
                        signals["pending_limits"][pending_key] = pending
                        queue_alert(format_pending_limit(whale, order, sid, market_o, score_o, "NEW"), priority=84)
                elif pending:
                    pending["last_market_px"] = market_o
                    pending["last_distance_pct"] = dist_o
                    pending["updated_ms"] = now_ms
                    if (not pending.get("approach_alerted")) and dist_o <= APPROACH_DISTANCE_PCT:
                        score_o = event_score(whale, notional_o, positions.get(coin_o), "APPROACHING ORDER")
                        pending["approach_alerted"] = True
                        pending["approach_ms"] = now_ms
                        queue_alert(format_pending_limit(whale, order, pending["id"], market_o, score_o, "APPROACHING"), priority=88)

            if should_alert:
                # Fills are actual executions. If a fill belongs to a tracked pending Limit,
                # preserve that Pending Signal ID and transition it to ACTIVE.
                for fill in new_fills:
                    coin = str(fill.get("coin"))
                    px = fnum(fill.get("px"))
                    notional = abs(px * fnum(fill.get("sz")))
                    if notional < MIN_FILL_USD:
                        continue
                    action, _, fill_side = action_label(fill)
                    pos = positions.get(coin)
                    score = event_score(whale, notional, pos, action)
                    event_coins.add(coin)
                    whale_key = f"{whale.address}:{coin}"
                    active = signals["active_whale"].get(whale_key)

                    fill_oid = str(fill.get("oid")) if fill.get("oid") is not None else ""
                    pending_key = f"{whale.address}:{fill_oid}" if fill_oid else ""
                    pending = signals["pending_limits"].get(pending_key) if pending_key else None

                    if pending and action.startswith("NEW "):
                        if active and active.get("id") != pending.get("id"):
                            closed = dict(active)
                            closed.update({"closed_ms": now_ms, "close_price": px, "close_reason": "Replaced by activated pending limit signal", "status": "CLOSED"})
                            queue_alert(format_signal_close(closed, px, now_ms, closed["close_reason"], "WHALE"), priority=90)
                            archive_signal(signals, closed)
                        active = {
                            "id": pending["id"], "type": "WHALE", "coin": coin, "side": fill_side,
                            "open_price": px, "opened_ms": now_ms, "whale": whale.address,
                            "whale_rank": whale.rank, "whale_quality": whale.quality, "status": "OPEN",
                            "source": "PENDING_LIMIT", "limit_price": pending.get("limit_price"),
                        }
                        signals["active_whale"][whale_key] = active
                        activated = dict(pending)
                        activated.update({"status": "ACTIVATED", "activated_ms": now_ms, "fill_price": px})
                        archive_signal(signals, activated)
                        signals["pending_limits"].pop(pending_key, None)
                        queue_alert(format_limit_activated(whale, pending, fill, pos), priority=94)
                        continue

                    if action.startswith("NEW ") and score >= SECONDARY_MIN_SCORE:
                        if active and active.get("side") != fill_side:
                            closed = dict(active)
                            closed.update({"closed_ms": now_ms, "close_price": px, "close_reason": "Whale position flipped direction", "status": "CLOSED"})
                            queue_alert(format_signal_close(closed, px, now_ms, closed["close_reason"], "WHALE"), priority=90)
                            archive_signal(signals, closed)
                            signals["active_whale"].pop(whale_key, None)
                            active = None
                        if not active:
                            sid = next_signal_id(state, "WHL", coin, fill_side, now_ms)
                            active = {
                                "id": sid, "type": "WHALE", "coin": coin, "side": fill_side,
                                "open_price": px, "opened_ms": now_ms, "whale": whale.address,
                                "whale_rank": whale.rank, "whale_quality": whale.quality, "status": "OPEN",
                                "source": "TAKER" if bool(fill.get("crossed")) else "EXECUTED_FILL",
                            }
                            signals["active_whale"][whale_key] = active
                            queue_alert(format_whale_lifecycle(whale, fill, pos, score, sid, "OPEN") + "\n\n" +
                                        ("توضیح: ورود Taker/Market-like انجام شده و معامله فعال است." if bool(fill.get("crossed"))
                                         else "توضیح: Fill اجرا شده و معامله فعال است؛ ممکن است اجرای Limit/Maker باشد."),
                                        priority=88 if score >= 88 else 78)
                        else:
                            queue_alert(format_whale_lifecycle(whale, fill, pos, score, active["id"], "UPDATE"), priority=72)
                    elif "CLOSE" in action and active:
                        if pos and pos.get("side") == active.get("side"):
                            if score >= SECONDARY_MIN_SCORE:
                                queue_alert(format_whale_lifecycle(whale, fill, pos, score, active["id"], "UPDATE"), priority=72)
                        else:
                            closed = dict(active)
                            closed.update({"closed_ms": now_ms, "close_price": px, "close_reason": "Whale closed the tracked position", "status": "CLOSED"})
                            queue_alert(format_signal_close(closed, px, now_ms, closed["close_reason"], "WHALE"), priority=90)
                            archive_signal(signals, closed)
                            signals["active_whale"].pop(whale_key, None)
                    elif score >= SECONDARY_MIN_SCORE:
                        if active:
                            queue_alert(format_whale_lifecycle(whale, fill, pos, score, active["id"], "UPDATE"), priority=70)
                        else:
                            queue_alert(format_market_entry_fa(whale, fill, pos, score), priority=68)

                # Orders that disappeared: distinguish a real activation from cancellation.
                fill_oids = {str(f.get("oid")) for f in new_fills if f.get("oid") is not None}
                for oid, old_order in previous_orders.items():
                    if oid in current_orders:
                        continue
                    pending_key = f"{whale.address}:{oid}"
                    pending = signals["pending_limits"].get(pending_key)
                    if not pending:
                        continue

                    if oid in fill_oids:
                        # Matching fill will have handled activation above.
                        continue

                    removed = dict(pending)
                    removed.update({"status": "CANCELLED_OR_REMOVED", "closed_ms": now_ms})
                    archive_signal(signals, removed)
                    signals["pending_limits"].pop(pending_key, None)
                    queue_alert("\n".join([
                        "⚪ <b>PENDING LIMIT REMOVED</b>", "",
                        f"ID: <code>{html.escape(str(pending['id']))}</code>",
                        f"Coin: <b>{html.escape(str(pending['coin']))}</b> · Direction: <b>{pending['side']}</b>",
                        f"Limit: <b>{fnum(pending.get('limit_price')):,.6g}</b>",
                        "",
                        "توضیح: سفارش دیگر در Order Book باز نیست و Fill متناظر در بازه بررسی پیدا نشد؛ بنابراین آن را Cancel/Removed در نظر می‌گیریم، نه معامله فعال.",
                    ]), priority=55)

            ws["fills"] = list(dict.fromkeys((ws.get("fills", []) + current_fill_keys)))[-5000:]
            ws["orders"] = current_orders
            ws["positions"] = positions
            ws["updated_ms"] = now_ms

            # Safety net: if a tracked whale signal disappeared without a fill in our lookback, close it.
            for wkey, active in list(signals["active_whale"].items()):
                if active.get("whale") != whale.address:
                    continue
                coin = active.get("coin")
                pos = positions.get(coin)
                if not pos or pos.get("side") != active.get("side"):
                    close_px = mids.get(coin, fnum(active.get("open_price")))
                    closed = dict(active)
                    closed.update({"closed_ms": now_ms, "close_price": close_px, "close_reason": "Whale position is no longer open", "status": "CLOSED"})
                    queue_alert(format_signal_close(closed, close_px, now_ms, closed["close_reason"], "WHALE"), priority=90)
                    archive_signal(signals, closed)
                    signals["active_whale"].pop(wkey, None)
        except Exception:
            logging.exception("Failed whale %s", whale.address)

    # Close individual signals for whales that left the dynamic Top-10.
    for wkey, active in list(signals["active_whale"].items()):
        if active.get("whale") not in current_whale_addresses:
            coin = active.get("coin")
            close_px = mids.get(coin, fnum(active.get("open_price")))
            closed = dict(active)
            closed.update({"closed_ms": now_ms, "close_price": close_px, "close_reason": "Whale left the dynamic Top-10", "status": "CLOSED"})
            queue_alert(format_signal_close(closed, close_px, now_ms, closed["close_reason"], "WHALE"), priority=88)
            archive_signal(signals, closed)
            signals["active_whale"].pop(wkey, None)

    # Aggregate near-price Limit Orders across whales.
    current_clusters = build_limit_clusters(nearby_limit_orders, mids)
    previous_clusters = signals.get("limit_clusters", {}) or {}
    for ckey, cluster in current_clusters.items():
        prev = previous_clusters.get(ckey)
        should_send = (
            not prev
            or int(prev.get("count", 0)) != int(cluster["count"])
            or abs(fnum(prev.get("notional")) - cluster["notional"]) / max(cluster["notional"], 1.0) >= 0.20
            or abs(fnum(prev.get("market_px")) - cluster["market_px"]) / max(cluster["market_px"], 1.0) >= 0.005
        )
        if should_send and ((not first_run) or BOOTSTRAP_ALERTS):
            queue_alert(format_limit_cluster(cluster), priority=91)
    signals["limit_clusters"] = current_clusters

    new_consensus = build_consensus(whales, all_positions)
    active_main = signals["active_main"]

    # 1) Close or update already-open main signals.
    for key, sig in list(active_main.items()):
        c = new_consensus.get(key)
        still_valid = bool(c and qualifies_main(c))
        if not still_valid:
            opposite = "SHORT" if sig["side"] == "LONG" else "LONG"
            flipped = new_consensus.get(f"{sig['coin']}:{opposite}")
            reason = f"Consensus flipped to {opposite}" if flipped and qualifies_main(flipped) else "Consensus dropped below main threshold"
            close_px = mids.get(sig["coin"], fnum(sig.get("open_price")))
            closed = dict(sig)
            closed.update({"closed_ms": now_ms, "close_price": close_px, "close_reason": reason, "status": "CLOSED"})
            queue_alert(format_signal_close(closed, close_px, now_ms, reason, "MAIN"), priority=100)
            archive_signal(signals, closed)
            active_main.pop(key, None)
            continue

        count_changed = int(c["count"]) != int(sig.get("last_count", sig.get("open_count", 0)))
        share_changed = abs(c["weighted_share"] - fnum(sig.get("last_share", sig.get("open_share", 0)))) >= 0.08
        score_changed = abs(c["score"] - fnum(sig.get("last_score", sig.get("open_score", 0)))) >= 8
        if count_changed or share_changed or score_changed:
            queue_alert(format_main_update(c, whales, sig), priority=92)
        sig["last_count"] = c["count"]
        sig["last_share"] = c["weighted_share"]
        sig["last_score"] = c["score"]
        sig["last_notional"] = c["notional"]
        sig["last_avg_whale_entry"] = c.get("avg_entry", 0.0)
        sig["last_whale_entries"] = c.get("whale_entries", [])
        sig["updated_ms"] = now_ms

    # 2) Open newly-qualified main signals. Signal ID stays with it until close.
    for key, c in new_consensus.items():
        if not qualifies_main(c) or key in active_main:
            continue
        open_px = mids.get(c["coin"], fnum(c.get("avg_entry")))
        sid = next_signal_id(state, "MAIN", c["coin"], c["side"], now_ms)
        sig = {
            "id": sid, "type": "MAIN", "coin": c["coin"], "side": c["side"],
            "open_price": open_px, "opened_ms": now_ms, "status": "OPEN",
            "open_count": c["count"], "open_share": c["weighted_share"], "open_score": c["score"],
            "last_count": c["count"], "last_share": c["weighted_share"], "last_score": c["score"],
            "last_notional": c["notional"],
            "open_avg_whale_entry": c.get("avg_entry", 0.0),
            "open_whale_entries": c.get("whale_entries", []),
        }
        active_main[key] = sig
        queue_alert(format_main_open(c, whales, sid, open_px), priority=100)

    state["consensus"] = new_consensus
    state["last_run_ms"] = now_ms
    state["initialized"] = True
    state["version"] = 7
    save_state(state)
    flush_alerts()
    logging.info(
        "Done. whales=%d first_run=%s alerts_flushed=true active_main=%d active_whale=%d top=%s",
        len(whales), first_run, len(signals["active_main"]), len(signals["active_whale"]),
        ",".join(short_addr(w.address) for w in whales),
    )


def main():
    parser = argparse.ArgumentParser(description="Dynamic Hyperliquid whale ranking + signal lifecycle tracker")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--refresh-ranking", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    run_once(test_telegram=args.test_telegram, refresh_ranking_now=args.refresh_ranking)


if __name__ == "__main__":
    main()
