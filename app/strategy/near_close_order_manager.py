from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.models.core import OrderBookSnapshot


@dataclass(slots=True)
class NearCloseOrderState:
    order_id: str
    token_id: str
    market_slug: str
    entry_price: float
    size: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled_size: float = 0.0


class NearCloseOrderManager:
    """Small guardrail helper for near-close maker orders.

    The first live release still uses GTD for hard expiry. This helper keeps the
    cancel/stop predicates centralized so the watch loop and tests use the same
    rules before a live order manager is expanded further.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def entry_cancel_reasons(
        self,
        *,
        book: OrderBookSnapshot,
        minutes_to_end: float | None,
        entry_price: float,
        variant: str = "official",
        crypto_strike_distance: float | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        midpoint = book.midpoint
        if variant == "crypto_updown":
            min_minutes = self.settings.near_close_crypto_updown_min_minutes_to_end
            min_best_ask = self.settings.near_close_crypto_updown_min_best_ask
            min_midpoint = self.settings.near_close_crypto_updown_min_midpoint
            max_spread = self.settings.near_close_crypto_updown_max_spread
        elif variant == "crypto":
            min_minutes = self.settings.near_close_crypto_min_minutes_to_end
            min_best_ask = self.settings.near_close_crypto_min_best_ask
            min_midpoint = self.settings.near_close_crypto_min_midpoint
            max_spread = self.settings.near_close_crypto_max_spread
        else:
            min_minutes = self.settings.near_close_min_minutes_to_end
            min_best_ask = self.settings.near_close_min_best_ask
            min_midpoint = self.settings.near_close_min_midpoint
            max_spread = self.settings.near_close_max_spread
        if minutes_to_end is not None and minutes_to_end < min_minutes:
            reasons.append("too_close_to_end")
        if book.best_ask is None or book.best_ask < min_best_ask:
            reasons.append("best_ask_below_floor")
        if midpoint is None or midpoint < min_midpoint:
            reasons.append("midpoint_below_floor")
        if book.spread is None or book.spread > max_spread:
            reasons.append("spread_too_wide")
        if book.best_ask is not None and entry_price >= book.best_ask:
            reasons.append("would_cross_post_only")
        if (
            variant == "crypto"
            and crypto_strike_distance is not None
            and crypto_strike_distance < self.settings.near_close_crypto_cancel_strike_distance
        ):
            reasons.append("crypto_strike_too_close")
        return reasons

    def hard_stop_required(self, *, book: OrderBookSnapshot, entry_price: float) -> bool:
        midpoint = book.midpoint
        if midpoint is not None and midpoint < entry_price - self.settings.near_close_hard_stop_offset:
            return True
        return bool(book.best_bid is not None and book.best_bid < self.settings.near_close_hard_stop_bid)

    def taker_exit_required(self, *, book: OrderBookSnapshot) -> bool:
        reference_price = book.best_bid if book.best_bid is not None else book.midpoint
        if reference_price is None:
            return False
        return reference_price <= self.settings.near_close_taker_exit_price

    def taker_exit_price(self, *, book: OrderBookSnapshot) -> float | None:
        if book.best_bid is not None:
            return float(book.best_bid)
        if book.midpoint is not None:
            return float(book.midpoint)
        return None

    def emergency_worst_price(self, *, book: OrderBookSnapshot, entry_price: float) -> float | None:
        if book.best_bid is None:
            return None
        return max(
            book.best_bid - self.settings.near_close_emergency_slippage,
            entry_price - self.settings.near_close_emergency_max_loss,
        )

    @staticmethod
    def normalize_open_order(order: dict[str, Any]) -> NearCloseOrderState | None:
        raw_metadata = order.get("metadata") or order.get("response") or {}
        if isinstance(raw_metadata, dict) and raw_metadata.get("strategy_variant") != "near_close_maker":
            return None
        order_id = order.get("id") or order.get("orderID") or order.get("orderId")
        token_id = order.get("token_id") or order.get("tokenID") or order.get("asset_id")
        market_slug = order.get("market_slug") or order.get("market") or ""
        price = order.get("price")
        size = order.get("size") or order.get("original_size") or order.get("remaining_size")
        if order_id is None or token_id is None or price is None or size is None:
            return None
        return NearCloseOrderState(
            order_id=str(order_id),
            token_id=str(token_id),
            market_slug=str(market_slug),
            entry_price=float(price),
            size=float(size),
        )
