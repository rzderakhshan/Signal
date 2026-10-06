from __future__ import annotations

import argparse
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from telegram_utils import send_telegram_message

load_dotenv()

API_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz/info")
STATE_PATH = Path(os.getenv("STATE_PATH", ".state/whale_state.json"))
WHALES_PATH = Path(os.getenv("WHALES_PATH", "whales.json"))
MIN_FILL_USD = float(os.getenv("MIN_FILL_USD", "100000"))
MIN_ORDER_USD = float(os.getenv("MIN_ORDER_USD", "250000"))
CONSENSUS_MIN_WHALES = int(os.getenv("CONSENSUS_MIN_WHALES", "2"))
LOOKBACK_MS = int(os.getenv("LOOKBACK_MS", str(15 * 60 * 1000)))
BOOTSTRAP_ALERTS = os.getenv("BOOTSTRAP_ALERTS", "false").lower() == "true"


@dataclass(frozen=True)
class Whale:
    name: str
    address: str


class HyperliquidClient:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.session = requests.Session()

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
            "type": "userFillsByTime",
            "user": address,
            "startTime": start_ms,
            "endTime": end_ms,
            "aggregateByTime": True,
        })

    def open_orders(self, address: str):
        return self._post({"type": "openOrders", "user": address})

    def clearinghouse(self, address: str):
        return self._post({"type": "clearinghouseState", "user": address})


def load_whales() -> list[Whale]:
    raw = json.loads(WHALES_PATH.read_text(encoding="utf-8"))
    whales = []
    for item in raw:
        addr = item["address"].lower().strip()
        if not (addr.startswith("0x") and len(addr) == 42):
            raise ValueError(f"Invalid whale address: {addr}")
        whales.append(Whale(item.get("name", addr[:8]), addr))
    return whales


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "initialized": False, "last_run_ms": 0, "whales": {}, "consensus": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("State file unreadable; starting with empty state")
        return {"version": 1, "initialized": False, "last_run_ms": 0, "whales": {}, "consensus": {}}


def save_state(state: dict[str, Any]):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def fill_key(fill: dict[str, Any]) -> str:
    # tid is best when present; fallback remains stable enough for dedup.
    return str(fill.get("tid") or f"{fill.get('hash')}:{fill.get('oid')}:{fill.get('time')}:{fill.get('coin')}:{fill.get('px')}:{fill.get('sz')}")


def order_key(order: dict[str, Any]) -> str:
    return str(order.get("oid"))


def positions_from_state(ch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ap in ch.get("assetPositions", []):
        p = ap.get("position", {})
        coin = p.get("coin")
        szi = fnum(p.get("szi"))
        if not coin or abs(szi) <= 0:
            continue
        out[coin] = {
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "entry": fnum(p.get("entryPx")),
            "value": abs(fnum(p.get("positionValue"))),
            "upnl": fnum(p.get("unrealizedPnl")),
            "liq": fnum(p.get("liquidationPx")),
            "leverage": (p.get("leverage") or {}).get("value"),
        }
    return out


def _action_label(fill: dict[str, Any]) -> tuple[str, str]:
    """Return a concise action label + emoji based on Hyperliquid's fill direction."""
    direction = str(fill.get("dir", "Fill")).strip()
    d = direction.lower()
    if "open long" in d:
        return "NEW LONG", "🟢"
    if "open short" in d:
        return "NEW SHORT", "🔴"
    if "close long" in d:
        return "REDUCE/CLOSE LONG", "🟠"
    if "close short" in d:
        return "REDUCE/CLOSE SHORT", "🟠"
    if str(fill.get("side")) == "B":
        return direction.upper(), "🟢"
    return direction.upper(), "🔴"


def format_fill(whale: Whale, fill: dict[str, Any], pos: dict[str, Any] | None) -> str:
    coin = html.escape(str(fill.get("coin", "?")))
    action, emoji = _action_label(fill)
    px = fnum(fill.get("px"))
    sz = abs(fnum(fill.get("sz")))
    notional = px * sz
    lines = [
        "🐋 <b>WHALE ALERT</b>",
        "",
        f"<b>{html.escape(whale.name)}</b>  <code>{short_addr(whale.address)}</code>",
        f"{emoji} <b>{html.escape(action)}</b> — <b>{coin}</b>",
        "",
        f"Executed: <b>{money(notional)}</b>",
        f"Price: <b>{px:,.6g}</b>",
        f"Size: <b>{sz:,.6g}</b>",
    ]
    if pos:
        lines.extend([
            f"Position: <b>{money(pos['value'])}</b> {pos['side']}",
            f"Entry: <b>{pos['entry']:,.6g}</b>",
            f"uPnL: <b>{money(pos['upnl'])}</b>",
        ])
        if pos.get("leverage"):
            lines.append(f"Leverage: <b>{pos.get('leverage')}x</b>")
        if pos.get("liq"):
            lines.append(f"Liquidation: <b>{pos['liq']:,.6g}</b>")
    if fill.get("closedPnl") not in (None, "0", "0.0"):
        lines.append(f"Realized PnL: <b>{money(fnum(fill.get('closedPnl')))}</b>")
    lines.extend([
        "",
        f"<a href=\"https://app.hyperliquid.xyz/explorer/address/{whale.address}\">View wallet on Hyperliquid ↗</a>",
    ])
    return "\n".join(lines)


def format_order(whale: Whale, order: dict[str, Any], status: str) -> str:
    coin = html.escape(str(order.get("coin", "?")))
    px = fnum(order.get("limitPx"))
    sz = abs(fnum(order.get("sz")))
    ntl = px * sz
    side = "BUY" if order.get("side") == "B" else "SELL"
    emoji = "🟦" if side == "BUY" else "🟥"
    title = "NEW LIMIT ORDER" if status == "NEW" else "LIMIT ORDER REMOVED/FILLED"
    return "\n".join([
        "📌 <b>WHALE ORDER ALERT</b>",
        "",
        f"<b>{html.escape(whale.name)}</b>  <code>{short_addr(whale.address)}</code>",
        f"{emoji} <b>{title}</b> — <b>{coin}</b>",
        "",
        f"Side: <b>{side}</b>",
        f"Limit price: <b>{px:,.6g}</b>",
        f"Order value: <b>{money(ntl)}</b>",
        f"Size: <b>{sz:,.6g}</b>",
        f"Order ID: <code>{order.get('oid')}</code>",
        "",
        f"<a href=\"https://app.hyperliquid.xyz/explorer/address/{whale.address}\">View wallet on Hyperliquid ↗</a>",
    ])


def consensus_map(all_positions: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    buckets: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for whale_name, positions in all_positions.items():
        for coin, pos in positions.items():
            buckets.setdefault((coin, pos["side"]), []).append((whale_name, pos))
    result = {}
    for (coin, side), rows in buckets.items():
        if len(rows) >= CONSENSUS_MIN_WHALES:
            key = f"{coin}:{side}"
            result[key] = {
                "coin": coin,
                "side": side,
                "count": len(rows),
                "names": [r[0] for r in rows],
                "notional": sum(r[1]["value"] for r in rows),
            }
    return result


def run_once(test_telegram: bool = False):
    if test_telegram:
        ok = send_telegram_message("✅ <b>Hyperliquid Whale Tracker</b>\nTelegram connection is working.")
        raise SystemExit(0 if ok else 2)

    whales = load_whales()
    state = load_state()
    client = HyperliquidClient()
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
            all_positions[whale.name] = positions

            fills = client.fills(whale.address, start_ms, now_ms)
            known_fills = set(ws.get("fills", []))
            current_fill_keys = []
            new_fills = []
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
            if should_alert:
                for fill in new_fills:
                    notional = abs(fnum(fill.get("px")) * fnum(fill.get("sz")))
                    if notional >= MIN_FILL_USD:
                        event_coins.add(str(fill.get("coin")))
                        send_telegram_message(format_fill(whale, fill, positions.get(str(fill.get("coin")))))

                for oid, order in current_orders.items():
                    if oid not in previous_orders:
                        notional = abs(fnum(order.get("limitPx")) * fnum(order.get("sz")))
                        if notional >= MIN_ORDER_USD:
                            event_coins.add(str(order.get("coin")))
                            send_telegram_message(format_order(whale, order, "NEW"))

                for oid, old_order in previous_orders.items():
                    if oid not in current_orders:
                        notional = abs(fnum(old_order.get("limitPx")) * fnum(old_order.get("sz")))
                        if notional >= MIN_ORDER_USD:
                            send_telegram_message(format_order(whale, old_order, "REMOVED/FILLED"))

            # Keep a bounded rolling dedup window.
            ws["fills"] = list(dict.fromkeys((ws.get("fills", []) + current_fill_keys)))[-5000:]
            ws["orders"] = current_orders
            ws["positions"] = positions
            ws["updated_ms"] = now_ms
        except Exception:
            logging.exception("Failed whale %s", whale.address)

    new_consensus = consensus_map(all_positions)
    old_consensus = state.get("consensus", {}) or {}
    if (not first_run) or BOOTSTRAP_ALERTS:
        for key, c in new_consensus.items():
            # Only announce new/stronger consensus, and preferably after activity on that coin.
            old = old_consensus.get(key)
            stronger = not old or int(c["count"]) > int(old.get("count", 0))
            if stronger and (not event_coins or c["coin"] in event_coins):
                emoji = "🟢" if c["side"] == "LONG" else "🔴"
                names = ", ".join(c["names"])
                send_telegram_message(
                    f"🔥 <b>WHALE CONSENSUS</b>\n\n"
                    f"{emoji} <b>{html.escape(c['coin'])} {c['side']}</b>\n"
                    f"Agreement: <b>{c['count']}/{len(whales)} whales</b>\n"
                    f"Combined position: <b>{money(c['notional'])}</b>\n"
                    f"Whales: {html.escape(names)}\n\n"
                    f"⚠️ This is whale-flow information, not an automatic trade instruction."
                )

    state["consensus"] = new_consensus
    state["last_run_ms"] = now_ms
    state["initialized"] = True
    state["version"] = 1
    save_state(state)
    logging.info("Done. whales=%d first_run=%s", len(whales), first_run)


def main():
    parser = argparse.ArgumentParser(description="Hyperliquid whale fill/order tracker")
    parser.add_argument("--test-telegram", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    run_once(test_telegram=args.test_telegram)


if __name__ == "__main__":
    main()
