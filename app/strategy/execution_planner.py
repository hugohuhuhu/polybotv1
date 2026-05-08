from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.core import ExecutionLeg, ExecutionPlan, Opportunity, OrderBookSnapshot, PaperTradeResult, StrategyType


class ExecutionPlanner:
    """Build manual-first execution plans from scanner opportunities."""

    def __init__(self, max_leg_size: float | None = None) -> None:
        self.max_leg_size = max_leg_size

    def build_plan(self, opportunity: Opportunity) -> ExecutionPlan:
        legs: list[ExecutionLeg] = []
        size = max(opportunity.max_safe_size, 0.0)
        if self.max_leg_size is not None:
            size = min(size, max(self.max_leg_size, 0.0))
        prices = opportunity.prices
        if opportunity.direction.value == "buy_basket":
            if (
                opportunity.strategy_type == StrategyType.LATE_RESOLUTION
                and opportunity.details.get("strategy_variant") == "near_close_maker"
                and opportunity.token_ids
            ):
                entry_bid = float(prices.get("entry_bid") or opportunity.details.get("entry_bid") or 0.0)
                legs = [
                    ExecutionLeg(
                        action="BUY",
                        token_id=opportunity.token_ids[0],
                        market_slug=opportunity.market_slugs[0],
                        outcome_label=str(opportunity.details.get("outcome_label") or "Outcome"),
                        target_price=entry_bid,
                        size=size,
                        order_type=str(opportunity.details.get("order_type") or "GTD"),
                        post_only=bool(opportunity.details.get("post_only", True)),
                        expiration_sec=int(opportunity.details.get("expiration_sec") or 20),
                        metadata=dict(opportunity.details),
                    )
                ]
            elif "yes_ask" in prices and "no_ask" in prices and len(opportunity.token_ids) >= 2:
                legs = [
                    ExecutionLeg(
                        action="BUY",
                        token_id=opportunity.token_ids[0],
                        market_slug=opportunity.market_slugs[0],
                        outcome_label="Yes",
                        target_price=float(prices["yes_ask"] or 0.0),
                        size=size,
                    ),
                    ExecutionLeg(
                        action="BUY",
                        token_id=opportunity.token_ids[1],
                        market_slug=opportunity.market_slugs[0],
                        outcome_label="No",
                        target_price=float(prices["no_ask"] or 0.0),
                        size=size,
                    ),
                ]
            else:
                for token_id, target_price in zip(opportunity.token_ids, prices.values(), strict=False):
                    if target_price is None:
                        continue
                    legs.append(
                        ExecutionLeg(
                            action="BUY",
                            token_id=token_id,
                            market_slug=opportunity.market_slugs[0],
                            outcome_label="Outcome",
                            target_price=float(target_price),
                            size=size,
                        )
                    )
        elif opportunity.direction.value == "sell_basket":
            for token_id, target_price in zip(opportunity.token_ids, prices.values(), strict=False):
                if target_price is None:
                    continue
                legs.append(
                    ExecutionLeg(
                        action="SELL",
                        token_id=token_id,
                        market_slug=opportunity.market_slugs[0],
                        outcome_label="Outcome",
                        target_price=float(target_price),
                        size=size,
                    )
                )
        else:
            for token_id in opportunity.token_ids:
                legs.append(
                    ExecutionLeg(
                        action="REVIEW",
                        token_id=token_id,
                        market_slug=opportunity.market_slugs[0],
                        outcome_label="Outcome",
                        target_price=0.0,
                        size=0.0,
                    )
                )
        live_trading_allowed = bool(opportunity.details.get("tradable_live")) and not bool(
            opportunity.details.get("requires_inventory")
        )
        return ExecutionPlan(
            opportunity_id=opportunity.opportunity_id,
            summary=opportunity.suggested_action,
            legs=legs,
            max_slippage_bps=10.0,
            cancel_conditions=[
                "Any leg moves beyond the allowed slippage.",
                "Any leg no longer has enough visible depth.",
                "The opportunity is no longer actionable when execution starts.",
            ],
            requires_manual_approval=True,
            live_trading_allowed=live_trading_allowed,
            strategy_type=opportunity.strategy_type.value,
            metadata=dict(opportunity.details),
        )


class PaperTradeSimulator:
    """Simulate fills using current order book state."""

    def __init__(self, fees_bps: float = 0.0) -> None:
        self.fees_bps = fees_bps

    def simulate(self, plan: ExecutionPlan, books: dict[str, OrderBookSnapshot], expected_edge: float) -> PaperTradeResult:
        filled_prices: list[float] = []
        filled_size: float | None = None
        for leg in plan.legs:
            if leg.action == "REVIEW":
                return PaperTradeResult(
                    opportunity_id=plan.opportunity_id,
                    filled=False,
                    notes="Review-only plan cannot be paper-filled.",
                )
            book = books.get(leg.token_id)
            if book is None:
                return PaperTradeResult(
                    opportunity_id=plan.opportunity_id,
                    filled=False,
                    notes="Missing order book snapshot.",
                )
            if leg.action == "BUY":
                if leg.post_only:
                    best = book.best_bid
                    depth = book.depth_for_side("bid", best) if best is not None else 0.0
                    if (
                        best is None
                        or book.best_ask is None
                        or leg.target_price < best
                        or leg.target_price >= book.best_ask
                        or depth < leg.size
                    ):
                        return PaperTradeResult(
                            opportunity_id=plan.opportunity_id,
                            filled=False,
                            notes="Post-only maker buy did not rest at a valid bid in the paper simulation.",
                        )
                    filled_prices.append(leg.target_price)
                    filled_size = leg.size if filled_size is None else min(filled_size, leg.size)
                    continue
                best = book.best_ask
                depth = book.depth_for_side("ask", best) if best is not None else 0.0
                if best is None or best > leg.target_price or depth < leg.size:
                    return PaperTradeResult(
                        opportunity_id=plan.opportunity_id,
                        filled=False,
                        notes="Buy leg did not fill in the paper simulation.",
                    )
                filled_prices.append(best)
            elif leg.action == "SELL":
                best = book.best_bid
                depth = book.depth_for_side("bid", best) if best is not None else 0.0
                if best is None or best < leg.target_price or depth < leg.size:
                    return PaperTradeResult(
                        opportunity_id=plan.opportunity_id,
                        filled=False,
                        notes="Sell leg did not fill in the paper simulation.",
                    )
                filled_prices.append(best)
            filled_size = leg.size if filled_size is None else min(filled_size, leg.size)
        average_entry = sum(filled_prices) / len(filled_prices) if filled_prices else None
        final_size = filled_size or 0.0
        gross_notional = sum(abs(float(price) * float(final_size)) for price in filled_prices)
        estimated_fees_paid = gross_notional * (self.fees_bps / 10_000)
        return PaperTradeResult(
            opportunity_id=plan.opportunity_id,
            filled=True,
            average_entry_price=average_entry,
            filled_size=final_size,
            gross_notional=gross_notional,
            estimated_fees_paid=estimated_fees_paid,
            expected_pnl=expected_edge * final_size,
            notes="Paper fill completed using the current order book snapshot.",
        )


class LiveTradingAdapter(ABC):
    """Optional live-trading interface kept disabled by default."""

    @abstractmethod
    async def execute(self, plan: ExecutionPlan):
        """Execute a plan on a live venue."""
