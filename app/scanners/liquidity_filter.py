from __future__ import annotations

from app.config import Settings
from app.models.core import MarketRecord, Opportunity, OrderBookSnapshot, StrategyType
from app.utils.time_utils import minutes_to


class LiquidityFilter:
    """Shared market and opportunity guardrails."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def market_gate_reason(
        self,
        market: MarketRecord,
        books: dict[str, OrderBookSnapshot],
        *,
        relaxed: bool = False,
    ) -> str | None:
        min_liquidity = self.settings.candidate_min_liquidity if relaxed else self.settings.min_liquidity
        max_spread = self.settings.candidate_max_spread if relaxed else self.settings.max_spread
        min_minutes_to_resolution = (
            self.settings.candidate_min_minutes_to_resolution
            if relaxed
            else self.settings.min_minutes_to_resolution
        )
        if not market.active or market.closed:
            return "inactive"
        if (market.liquidity or 0.0) < min_liquidity:
            return "low_liquidity"
        time_left = minutes_to(market.end_date)
        if (
            not self.settings.allow_near_resolution
            and time_left is not None
            and time_left < min_minutes_to_resolution
        ):
            return "near_resolution"
        if not market.token_ids:
            return "missing_tokens"
        valid_books = [books.get(token_id) for token_id in market.token_ids]
        valid_books = [book for book in valid_books if book is not None]
        if not valid_books:
            return "missing_books"
        if any(book.spread is not None and book.spread > max_spread for book in valid_books):
            return "wide_spread"
        return None

    def allow_market(
        self,
        market: MarketRecord,
        books: dict[str, OrderBookSnapshot],
        *,
        relaxed: bool = False,
    ) -> bool:
        return self.market_gate_reason(market, books, relaxed=relaxed) is None

    def minimum_depth(self, book: OrderBookSnapshot, side: str) -> float:
        best_price = book.best_ask if side == "ask" else book.best_bid
        if best_price is None:
            return 0.0
        return book.depth_for_side(side, best_price)

    def _required_depth(self, opportunity: Opportunity, *, relaxed: bool) -> float:
        if opportunity.strategy_type == StrategyType.STALE_PRICE:
            return 0.0
        if (
            opportunity.strategy_type == StrategyType.LATE_RESOLUTION
            and opportunity.details.get("strategy_variant") == "near_close_maker"
        ):
            variant = opportunity.details.get("near_close_variant")
            if variant == "crypto_updown":
                order_size = self.settings.near_close_crypto_updown_order_size
                min_depth = self.settings.near_close_crypto_updown_min_depth
            elif variant == "crypto":
                order_size = self.settings.near_close_crypto_order_size
                min_depth = self.settings.near_close_min_depth
            else:
                order_size = self.settings.near_close_order_size
                min_depth = self.settings.near_close_min_depth
            return min(order_size, min_depth)
        return self.settings.candidate_min_depth if relaxed else self.settings.min_depth

    def classify_opportunity(self, opportunity: Opportunity) -> str:
        strict_depth = self._required_depth(opportunity, relaxed=False)
        relaxed_depth = self._required_depth(opportunity, relaxed=True)
        min_net_edge = self.settings.min_net_edge
        if (
            opportunity.strategy_type == StrategyType.LATE_RESOLUTION
            and opportunity.details.get("strategy_variant") == "near_close_maker"
        ):
            min_net_edge = self.settings.near_close_min_net_edge
        is_actionable = (
            opportunity.net_edge >= min_net_edge
            and opportunity.available_liquidity >= strict_depth
            and opportunity.max_safe_size >= strict_depth
        )
        if is_actionable:
            return "actionable"
        is_candidate = (
            opportunity.net_edge >= self.settings.candidate_min_net_edge
            and opportunity.available_liquidity >= relaxed_depth
            and opportunity.max_safe_size >= relaxed_depth
        )
        if is_candidate:
            return "candidate"
        return "rejected"

    def annotate_opportunity(self, opportunity: Opportunity) -> bool:
        tier = self.classify_opportunity(opportunity)
        if tier == "rejected":
            return False
        opportunity.details["qualification_tier"] = tier
        opportunity.details["qualification_label"] = (
            "可直接警示" if tier == "actionable" else "備選觀察"
        )
        opportunity.details["qualification_label"] = "可直接警示" if tier == "actionable" else "候選觀察"
        opportunity.details["alert_eligible"] = tier == "actionable"
        return True

    def allow_opportunity(self, opportunity: Opportunity) -> bool:
        return self.annotate_opportunity(opportunity)

    def is_alert_eligible(self, opportunity: Opportunity) -> bool:
        if "alert_eligible" not in opportunity.details:
            self.annotate_opportunity(opportunity)
        return bool(opportunity.details.get("alert_eligible"))
