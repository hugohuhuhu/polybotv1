from __future__ import annotations

from typing import Any

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan
from app.storage.repositories import ScannerRepository
from app.strategy.near_close_order_manager import NearCloseOrderManager
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter


async def execute_near_close_taker_exits(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
    watch_books: dict[str, Any],
) -> list[dict[str, object]]:
    manager = NearCloseOrderManager(settings)
    exits: list[dict[str, object]] = []
    for group in repository.near_close_stop_exit_groups(limit=50):
        if float(group.get("open_size") or 0.0) <= 1e-9:
            continue
        token_id = str(group.get("token_id") or "")
        market_slug = str(group.get("market_slug") or "")
        if "updown" not in market_slug:
            continue
        book = watch_books.get(token_id)
        if book is None or not manager.taker_exit_required(book=book):
            continue
        target_price = manager.taker_exit_price(book=book)
        if target_price is None or target_price <= 0:
            continue
        size = float(group.get("open_size") or 0.0)
        plan = ExecutionPlan(
            opportunity_id=f"stop-exit:{market_slug}:{token_id}",
            summary=f"Taker stop exit on {market_slug} at {target_price:.4f}",
            legs=[
                ExecutionLeg(
                    action="SELL",
                    token_id=token_id,
                    market_slug=market_slug,
                    outcome_label=str(group.get("outcome_label") or "Outcome"),
                    target_price=target_price,
                    size=size,
                    order_type="FAK",
                    post_only=False,
                    metadata={
                        "strategy_variant": "near_close_stop_exit",
                        "stop_trigger_price": settings.near_close_taker_exit_price,
                        "stop_reference_price": target_price,
                    },
                )
            ],
            max_slippage_bps=10.0,
            cancel_conditions=["Stop exit should take immediately available liquidity."],
            requires_manual_approval=False,
            live_trading_allowed=True,
            strategy_type="near_close_stop_exit",
            metadata={"market_slug": market_slug, "token_id": token_id},
        )
        live_result = await live_trader.execute(plan)
        repository.save_live_execution(live_result)
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=plan.opportunity_id,
            status=live_result.status,
            message=live_result.message,
            details={
                "stop_trigger_price": settings.near_close_taker_exit_price,
                "reference_price": target_price,
                "market_slug": market_slug,
                "token_id": token_id,
                "legs": [leg.model_dump() for leg in live_result.leg_results],
            },
        )
        exits.append(
            {
                "market_slug": market_slug,
                "token_id": token_id,
                "status": live_result.status,
                "reference_price": target_price,
                "size": size,
            }
        )
    return exits
