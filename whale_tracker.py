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

# Consensus / signal engine
SECONDARY_MIN_SCORE = float(os.getenv("SECONDARY_MIN_SCORE", "68"))
MAIN_MIN_WHALES = int(os.getenv("MAIN_MIN_WHALES", "4"))
MAIN_MIN_WEIGHTED_SHARE = float(os.getenv("MAIN_MIN_WEIGHTED_SHARE", "0.68"))
MAIN_MIN_NOTIONAL_USD = float(os.getenv("MAIN_MIN_NOTIONAL_USD", "5000000"))
STRONG_MIN_WHALES = int(os.getenv("STRONG_MIN_WHALES", "6"))
STRONG_MIN_WEIGHTED_SHARE = float(os.getenv("STRONG_MIN_WEIGHTED_SHARE", "0.78"))
MAX_SINGLE_WHALE_SHARE = float(os.getenv("MAX_SINGLE_WHALE_SHARE", "0.50"))


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
        self.session.headers.update({"User-Agent": "HyperliquidWhaleTracker/3.0"})

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
        "version": 3, "initialized": False, "last_run_ms": 0,
        "whales": {}, "consensus": {}, "ranking": {}, "ranking_updated_ms": 0,
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
        r = requests.get(DISCOVERY_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0 WhaleTracker/3.0"})
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
        send_telegram_message("\n".join(parts))

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
        result[f"{coin}:{side}"] = {
            "coin": coin, "side": side, "count": len(rows), "addresses": [x[0] for x in rows],
            "notional": raw_notional, "weighted_share": share, "largest_share": largest_share,
            "quality_avg": quality_avg, "score": score,
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


def format_consensus(c: dict[str, Any], whales: list[Whale]) -> str:
    wm = {w.address: w for w in whales}
    names = ", ".join(f"#{wm[a].rank} {short_addr(a)}" for a in c["addresses"] if a in wm)
    emoji = "🟢" if c["side"] == "LONG" else "🔴"
    return (
        f"🔥🔥 <b>MAIN WHALE SIGNAL — {consensus_grade(c)}</b>\n\n"
        f"{emoji} <b>{html.escape(c['coin'])} {c['side']}</b>\n"
        f"Aligned whales: <b>{c['count']}/{len(whales)}</b>\n"
        f"Weighted consensus: <b>{c['weighted_share']*100:.0f}%</b>\n"
        f"Consensus score: <b>{c['score']:.0f}/100</b>\n"
        f"Combined position: <b>{money(c['notional'])}</b>\n"
        f"Average whale quality: <b>{c['quality_avg']:.0f}/100</b>\n"
        f"Participants: {html.escape(names)}\n\n"
        "This is the strongest signal class in the tracker because multiple high-quality whales agree."
    )


def run_once(test_telegram: bool = False, refresh_ranking_now: bool = False):
    if test_telegram:
        ok = send_telegram_message("✅ <b>Hyperliquid Whale Tracker v3</b>\nTelegram connection is working.")
        raise SystemExit(0 if ok else 2)

    state = load_state()
    client = HyperliquidClient()
    whales = refresh_ranking(client, state, force=refresh_ranking_now)
    now_ms = int(time.time() * 1000)
    first_run = not state.get("initialized", False)
    start_ms = max(now_ms - LOOKBACK_MS, int(state.get("last_run_ms", 0)) - 3000)

    all_positions: dict[str, dict[str, dict[str, Any]]] = {}
    event_coins: set[str] = set()

    for whale in whales:
        ws = state.setdefault("whales", {}).setdefault(whale.address, {"fills": [], "orders": {}, "positions": {}})
        try:
            ch = client.clearinghouse(whale.address)
            positions = positions_from_state(ch)
            all_positions[whale.address] = positions

            fills = client.fills(whale.address, start_ms, now_ms)
            known_fills = set(ws.get("fills", []))
            current_fill_keys: list[str] = []
            new_fills: list[dict[str, Any]] = []
            for fill in fills:
                key = fill_key(fill); current_fill_keys.append(key)
                if key not in known_fills:
                    new_fills.append(fill)
            new_fills.sort(key=lambda x: int(x.get("time", 0)))

            orders = client.open_orders(whale.address)
            current_orders = {order_key(o): o for o in orders}
            previous_orders = ws.get("orders", {}) or {}
            should_alert = (not first_run) or BOOTSTRAP_ALERTS

            if should_alert:
                for fill in new_fills:
                    notional = abs(fnum(fill.get("px")) * fnum(fill.get("sz")))
                    if notional < MIN_FILL_USD:
                        continue
                    action, _, _ = action_label(fill)
                    score = event_score(whale, notional, positions.get(str(fill.get("coin"))), action)
                    event_coins.add(str(fill.get("coin")))
                    if score >= SECONDARY_MIN_SCORE:
                        send_telegram_message(format_fill(whale, fill, positions.get(str(fill.get("coin"))), score))

                for oid, order in current_orders.items():
                    if oid not in previous_orders:
                        notional = abs(fnum(order.get("limitPx")) * fnum(order.get("sz")))
                        if notional >= MIN_ORDER_USD:
                            score = event_score(whale, notional, positions.get(str(order.get("coin"))), "NEW ORDER")
                            event_coins.add(str(order.get("coin")))
                            if score >= SECONDARY_MIN_SCORE:
                                send_telegram_message(format_order(whale, order, "NEW", score))

                for oid, old_order in previous_orders.items():
                    if oid not in current_orders:
                        notional = abs(fnum(old_order.get("limitPx")) * fnum(old_order.get("sz")))
                        if notional >= MIN_ORDER_USD:
                            score = event_score(whale, notional, positions.get(str(old_order.get("coin"))), "REMOVED ORDER")
                            if score >= SECONDARY_MIN_SCORE + 5:
                                send_telegram_message(format_order(whale, old_order, "REMOVED/FILLED", score))

            ws["fills"] = list(dict.fromkeys((ws.get("fills", []) + current_fill_keys)))[-5000:]
            ws["orders"] = current_orders; ws["positions"] = positions; ws["updated_ms"] = now_ms
        except Exception:
            logging.exception("Failed whale %s", whale.address)

    new_consensus = build_consensus(whales, all_positions)
    old_consensus = state.get("consensus", {}) or {}
    if (not first_run) or BOOTSTRAP_ALERTS:
        for key, c in new_consensus.items():
            if not qualifies_main(c):
                continue
            old = old_consensus.get(key)
            old_score = fnum((old or {}).get("score"))
            old_count = int((old or {}).get("count", 0))
            stronger = not old or c["count"] > old_count or c["score"] >= old_score + 4
            # Main signal is event-driven; a new/stronger consensus after activity on that coin.
            if stronger and (not event_coins or c["coin"] in event_coins):
                send_telegram_message(format_consensus(c, whales))

    state["consensus"] = new_consensus
    state["last_run_ms"] = now_ms
    state["initialized"] = True
    state["version"] = 3
    save_state(state)
    logging.info("Done. whales=%d first_run=%s top=%s", len(whales), first_run, ",".join(short_addr(w.address) for w in whales))


def main():
    parser = argparse.ArgumentParser(description="Dynamic Hyperliquid whale ranking + consensus tracker")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--refresh-ranking", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    run_once(test_telegram=args.test_telegram, refresh_ranking_now=args.refresh_ranking)


if __name__ == "__main__":
    main()
