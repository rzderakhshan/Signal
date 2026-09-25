"""Small numeric helpers for the user's AEGIS scoring modules."""
from __future__ import annotations

import math


def safe_float(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def clip(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, safe_float(value, 0.0)))
