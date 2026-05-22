from __future__ import annotations

from typing import Any

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan
from app.storage.repositories import ScannerRepository
from app.strategy.near_close_order_manager import NearCloseOrderManager
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter


def _split_cancel_response(order_ids: list[str], response: object) -> tuple[list[str], list[str]]:
    if not isinstance(response, dict):
        return order_ids, []
    canceled = response.get("canceled")
    if isinstance(canceled, list):
        canceled_ids = [str(order_id).strip() for order_id in canceled if str(order_id).strip()]
    else:
        canceled_ids = []
    not_canceled = response.get("not_canceled")
    if isinstance(not_canceled, dict):
        uncertain_ids = [str(order_id).strip() for order_id in not_canceled if str(order_id).strip()]
    else:
        uncertain_ids = []
    if not canceled_ids and not uncertain_ids:
        return order_ids, []
    return canceled_ids, uncertain_ids


def _stop_exit_result_needs_second_chance(live_result: object) -> bool:
    status = str(getattr(live_result, "status", "") or "").lower()
    if status not in {"failed", "partial_failure"}:
        return False
    message = str(getattr(live_result, "message", "") or "").lower()
    return "no orders found" in message or "no match" in message or "fak order" in message


def _fallback_stop_exit_book(
    *,
    repository: ScannerRepository,
    settings: Settings,
    token_id: str,
) -> Any | None:
    if not settings.near_close_stop_exit_stale_orderbook_enabled:
        return None
    books = repository.latest_orderbooks_for_tokens(
        [token_id],
        max_age_seconds=max(float(settings.near_close_stop_exit_stale_orderbook_max_age_sec), 0.0),
    )
    return books.get(token_id)


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
        book_source = "watch"
        if book is None:
            book = _fallback_stop_exit_book(repository=repository, settings=settings, token_id=token_id)
            book_source = "stored_orderbook" if book is not None else "missing"
        size = float(group.get("open_size") or 0.0)
        entry_price = 0.0
        if size > 1e-9:
            entry_price = float(group.get("open_cost_basis") or 0.0) / size
        reference_price = manager.taker_exit_reference_price(book=book) if book is not None else None
        if book is None or not manager.taker_exit_required(book=book, entry_price=entry_price):
            continue
        target_price = manager.taker_exit_price(book=book)
        if target_price is None or target_price <= 0:
            continue
        assumed_fill = bool(group.get("assumed_fill"))
        source_order_id = str(group.get("source_order_id") or "").strip()
        cancel_response = None
        if assumed_fill and not settings.near_close_assume_submitted_filled_stop_exit:
            continue
        if assumed_fill and source_order_id:
            try:
                cancel_response = await live_trader.cancel_orders([source_order_id])
                canceled_ids, uncertain_ids = _split_cancel_response([source_order_id], cancel_response)
                if source_order_id in canceled_ids and source_order_id not in uncertain_ids:
                    repository.mark_live_orders_cancelled(
                        [source_order_id],
                        status="qualification_cancelled",
                        cancel_response=cancel_response,
                    )
                    exits.append(
                        {
                            "market_slug": market_slug,
                            "token_id": token_id,
                            "status": "entry_cancelled_before_assumed_stop_exit",
                            "reference_price": reference_price,
                            "target_price": target_price,
                            "size": size,
                        }
                    )
                    continue
            except Exception as exc:
                cancel_response = {"error": str(exc)}
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
                        "stop_reference_price": reference_price,
                        "stop_entry_price": entry_price,
                        "stop_limit_price": target_price,
                        "stop_slippage": settings.near_close_emergency_slippage,
                        "stop_orderbook_source": book_source,
                        "assumed_fill_stop_exit": assumed_fill,
                        "source_order_id": source_order_id or None,
                        "source_cancel_response": cancel_response,
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
        second_chance_result = None
        second_chance_target = None
        if settings.near_close_second_chance_exit_enabled and _stop_exit_result_needs_second_chance(live_result):
            second_chance_target = max(float(settings.near_close_second_chance_exit_price), 0.01)
            second_chance_plan = ExecutionPlan(
                opportunity_id=f"stop-exit-second-chance:{market_slug}:{token_id}",
                summary=f"Second-chance taker stop exit on {market_slug} at {second_chance_target:.4f}",
                legs=[
                    ExecutionLeg(
                        action="SELL",
                        token_id=token_id,
                        market_slug=market_slug,
                        outcome_label=str(group.get("outcome_label") or "Outcome"),
                        target_price=second_chance_target,
                        size=size,
                        order_type="FAK",
                        post_only=False,
                        metadata={
                            "strategy_variant": "near_close_stop_exit",
                            "stop_exit_stage": "second_chance",
                            "stop_trigger_price": settings.near_close_taker_exit_price,
                            "stop_reference_price": reference_price,
                            "stop_entry_price": entry_price,
                            "stop_limit_price": second_chance_target,
                            "first_stop_limit_price": target_price,
                            "stop_orderbook_source": book_source,
                            "first_stop_status": live_result.status,
                            "first_stop_message": live_result.message,
                            "assumed_fill_stop_exit": assumed_fill,
                            "source_order_id": source_order_id or None,
                            "source_cancel_response": cancel_response,
                        },
                    )
                ],
                max_slippage_bps=10.0,
                cancel_conditions=["Second-chance stop exit should take any immediately available liquidity."],
                requires_manual_approval=False,
                live_trading_allowed=True,
                strategy_type="near_close_stop_exit",
                metadata={"market_slug": market_slug, "token_id": token_id, "stop_exit_stage": "second_chance"},
            )
            second_chance_result = await live_trader.execute(second_chance_plan)
            repository.save_live_execution(second_chance_result)
            repository.save_execution_event(
                source="watch",
                mode="live",
                opportunity_id=second_chance_plan.opportunity_id,
                status=second_chance_result.status,
                message=second_chance_result.message,
                details={
                    "stop_exit_stage": "second_chance",
                    "first_status": live_result.status,
                    "first_message": live_result.message,
                    "reference_price": reference_price,
                    "entry_price": entry_price,
                    "first_target_price": target_price,
                    "target_price": second_chance_target,
                    "orderbook_source": book_source,
                    "assumed_fill_stop_exit": assumed_fill,
                    "market_slug": market_slug,
                    "token_id": token_id,
                    "legs": [leg.model_dump() for leg in second_chance_result.leg_results],
                },
            )
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=plan.opportunity_id,
            status=live_result.status,
            message=live_result.message,
            details={
                "stop_trigger_price": settings.near_close_taker_exit_price,
                "reference_price": reference_price,
                "entry_price": entry_price,
                "target_price": target_price,
                "slippage": settings.near_close_emergency_slippage,
                "orderbook_source": book_source,
                "assumed_fill_stop_exit": assumed_fill,
                "source_order_id": source_order_id or None,
                "source_cancel_response": cancel_response,
                "second_chance_enabled": settings.near_close_second_chance_exit_enabled,
                "second_chance_attempted": second_chance_result is not None,
                "second_chance_target_price": second_chance_target,
                "second_chance_status": getattr(second_chance_result, "status", None),
                "market_slug": market_slug,
                "token_id": token_id,
                "legs": [leg.model_dump() for leg in live_result.leg_results],
            },
        )
        exits.append(
            {
                "market_slug": market_slug,
                "token_id": token_id,
                "status": getattr(second_chance_result, "status", live_result.status),
                "reference_price": reference_price,
                "target_price": second_chance_target if second_chance_result is not None else target_price,
                "first_target_price": target_price,
                "second_chance_attempted": second_chance_result is not None,
                "size": size,
            }
        )
    return exits
