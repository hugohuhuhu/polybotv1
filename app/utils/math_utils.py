from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from app.models.core import BookLevel


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_jsonish_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def sort_book_levels(levels: Iterable[BookLevel], side: str) -> list[BookLevel]:
    reverse = side.lower() == "bid"
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


def normalise_book_levels(levels: Iterable[dict[str, Any] | BookLevel], side: str) -> list[BookLevel]:
    parsed: list[BookLevel] = []
    for level in levels:
        if isinstance(level, BookLevel):
            book_level = level
        else:
            price = safe_float(level.get("price"))
            size = safe_float(level.get("size"))
            if price is None or size is None:
                continue
            if size <= 0:
                continue
            book_level = BookLevel(price=price, size=size)
        parsed.append(book_level)
    return sort_book_levels(parsed, side)


def sum_prices(prices: Iterable[float | None]) -> float | None:
    values = [price for price in prices if price is not None]
    if not values:
        return None
    return sum(values)


def estimate_bps_cost(notional: float, bps: float) -> float:
    return notional * (bps / 10_000)


def estimate_total_cost(edge_legs: int, fees_bps: float, slippage_bps: float) -> float:
    return edge_legs * ((fees_bps + slippage_bps) / 10_000)


def clamp_confidence(value: float) -> float:
    return max(0.0, min(value, 1.0))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()
