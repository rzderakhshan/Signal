"""Configurable discovery universe + optional broker metadata mapping."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_UNIVERSE_PATH = Path("universe.json")
DEFAULT_WATCHLIST_PATH = Path("watchlist.json")
DEFAULT_INSTRUMENT_MAP_PATH = Path("instrument_map.json")
MAX_UNIVERSE_SYMBOLS = 1000


def _normalize_symbols(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(s).strip().upper() for s in values if str(s).strip()))


def load_universe(path: Path | None = None) -> dict[str, Any]:
    """Load discovery universe from universe.json, falling back to watchlist.json."""
    candidates = []
    if path is not None:
        candidates.append(path)
    candidates.extend([DEFAULT_UNIVERSE_PATH, DEFAULT_WATCHLIST_PATH])

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            if not candidate.exists():
                continue
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"{candidate} must be a JSON object")
            symbols: dict[str, Any] = {
                "stocks": _normalize_symbols(raw.get("stocks", [])),
                "crypto": _normalize_symbols(raw.get("crypto", [])),
                "settings": raw.get("settings", {}) if isinstance(raw.get("settings", {}), dict) else {},
            }
            if not symbols["stocks"] and not symbols["crypto"]:
                raise ValueError(f"{candidate} is empty")
            total = len(symbols["stocks"]) + len(symbols["crypto"])
            if total > MAX_UNIVERSE_SYMBOLS:
                raise ValueError(
                    f"limit the configurable discovery universe to {MAX_UNIVERSE_SYMBOLS} symbols "
                    f"(got {total}) to keep GitHub Actions runtime reasonable"
                )
            return symbols
        except (OSError, ValueError, TypeError) as exc:
            last_error = exc
            continue
    raise ValueError(str(last_error) if last_error else "no universe file found")


def load_instrument_map(path: Path = DEFAULT_INSTRUMENT_MAP_PATH) -> dict[str, dict[str, Any]]:
    """Optional Yahoo-symbol metadata. Missing/invalid file => empty map (non-blocking)."""
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if str(key).startswith("_") or not isinstance(value, dict):
                continue
            out[str(key).strip().upper()] = value
        return out
    except (OSError, ValueError, TypeError):
        return {}


def instrument_label(symbol: str, instrument_map: dict[str, dict[str, Any]] | None = None) -> str:
    meta = (instrument_map or {}).get(symbol.upper(), {})
    name = meta.get("name")
    return str(name) if name else symbol
