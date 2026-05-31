from __future__ import annotations

from typing import Any

from app.clients.crypto_price_client import CryptoPriceClient, binance_symbol_for_asset
from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan, LiveExecutionLegResult
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


def _unique_order_ids(order_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(order_id for order_id in order_ids if order_id))


def _book_telemetry(book: Any | None) -> dict[str, object]:
    if book is None:
        return {}
    best_bid = getattr(book, "best_bid", None)
    best_ask = getattr(book, "best_ask", None)
    return {
        "observed_best_bid": best_bid,
        "observed_best_ask": best_ask,
        "observed_midpoint": getattr(book, "midpoint", None),
        "observed_spread": getattr(book, "spread", None),
        "observed_top_bid_size": book.depth_for_side("bid", best_bid) if best_bid is not None else None,
        "observed_top_ask_size": book.depth_for_side("ask", best_ask) if best_ask is not None else None,
    }


def _execution_price_from_leg(leg: LiveExecutionLegResult) -> float | None:
    candidates = (
        leg.response.get("average_price"),
        leg.response.get("avg_price"),
        leg.response.get("price"),
        leg.response.get("matched_price"),
        leg.response.get("target_price"),
        leg.target_price,
    )
    for value in candidates:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _execution_telemetry(legs: list[LiveExecutionLegResult]) -> list[dict[str, object]]:
    return [
        {
            "action": leg.action,
            "order_id": leg.order_id,
            "status": leg.status,
            "target_price": leg.target_price,
            "requested_size": leg.requested_size,
            "reported_execution_price": _execution_price_from_leg(leg),
            "response": leg.response,
        }
        for leg in legs
    ]


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _crypto_updown_symbol(market_slug: str, entry_metadata: dict[str, Any]) -> str | None:
    raw_symbol = str(
        entry_metadata.get("crypto_symbol")
        or entry_metadata.get("near_close_crypto_symbol")
        or ""
    ).strip()
    if raw_symbol:
        return raw_symbol.upper()
    asset = str(market_slug or "").split("-", 1)[0].strip()
    if not asset:
        return None
    symbol = binance_symbol_for_asset(asset)
    return symbol.upper() if symbol else None


async def _crypto_price(
    *,
    settings: Settings,
    symbol: str,
    cache: dict[str, float | None],
    client_holder: dict[str, CryptoPriceClient | None],
) -> float | None:
    if symbol in cache:
        return cache[symbol]
    client = client_holder.get("client")
    if client is None:
        client = CryptoPriceClient(timeout=settings.crypto_price_timeout_sec)
        client_holder["client"] = client
    prices = await client.get_prices({symbol})
    price = prices.get(symbol)
    cache[symbol] = price
    return price


async def _crypto_updown_direction_guard(
    *,
    settings: Settings,
    market_slug: str,
    outcome_label: str,
    entry_metadata: dict[str, Any],
    price_cache: dict[str, float | None],
    client_holder: dict[str, CryptoPriceClient | None],
) -> tuple[bool, dict[str, object]]:
    details: dict[str, object] = {
        "crypto_direction_guard_enabled": settings.near_close_crypto_updown_stop_requires_direction_break,
    }
    if not settings.near_close_crypto_updown_stop_requires_direction_break:
        return True, details
    if "updown" not in str(market_slug).lower():
        return True, details
    start_price = _float_or_none(entry_metadata.get("crypto_start_price"))
    outcome = str(outcome_label or entry_metadata.get("crypto_winning_outcome") or "").strip().lower()
    symbol = _crypto_updown_symbol(market_slug, entry_metadata)
    details.update(
        {
            "crypto_stop_symbol": symbol,
            "crypto_stop_outcome": outcome or None,
            "crypto_stop_start_price": start_price,
        }
    )
    if start_price is None or start_price <= 0 or outcome not in {"up", "down"}:
        details["crypto_direction_guard_reason"] = "missing_entry_direction_metadata"
        return True, details
    if symbol is None:
        details["crypto_direction_guard_reason"] = "missing_symbol"
        return False, details
    spot = await _crypto_price(settings=settings, symbol=symbol, cache=price_cache, client_holder=client_holder)
    details["crypto_stop_spot_price"] = spot
    if spot is None:
        details["crypto_direction_guard_reason"] = "spot_unavailable"
        return False, details
    distance = abs(spot - start_price) / start_price
    break_buffer = max(float(settings.near_close_crypto_updown_stop_direction_break_buffer), 0.0)
    if outcome == "up":
        direction_broken = spot < start_price * (1.0 - break_buffer)
    else:
        direction_broken = spot > start_price * (1.0 + break_buffer)
    details.update(
        {
            "crypto_stop_start_distance": distance,
            "crypto_direction_break_buffer": break_buffer,
            "crypto_direction_broken": direction_broken,
            "crypto_direction_guard_reason": "direction_broken" if direction_broken else "direction_still_valid",
        }
    )
    return direction_broken, details


def _record_stop_exit_skip(
    *,
    repository: ScannerRepository,
    exits: list[dict[str, object]],
    opportunity_id: str,
    status: str,
    message: str,
    market_slug: str,
    token_id: str,
    reference_price: float | None,
    target_price: float | None,
    entry_price: float,
    size: float,
    details: dict[str, object],
) -> None:
    payload = {
        "panic_exit": True,
        "market_slug": market_slug,
        "token_id": token_id,
        "reference_price": reference_price,
        "target_price": target_price,
        "entry_price": entry_price,
        "size": size,
        **details,
    }
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=opportunity_id,
        status=status,
        message=message,
        details=payload,
    )
    exits.append(
        {
            "market_slug": market_slug,
            "token_id": token_id,
            "status": status,
            "reference_price": reference_price,
            "target_price": target_price,
            "size": size,
            **details,
        }
    )


async def _cancel_orders_for_panic_exit(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    opportunity_id: str,
    order_ids: list[str],
    status: str,
    message: str,
) -> tuple[list[str], list[str], object | None]:
    cleaned = _unique_order_ids(order_ids)
    if not cleaned:
        return [], [], None
    try:
        response = await live_trader.cancel_orders(cleaned)
    except Exception as exc:
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=opportunity_id,
            status=f"{status}_failed_continuing",
            message=str(exc),
            details={"order_ids": cleaned},
        )
        return [], cleaned, {"error": str(exc)}
    canceled_ids, uncertain_ids = _split_cancel_response(cleaned, response)
    if canceled_ids:
        repository.mark_live_orders_cancelled(canceled_ids, status=status, cancel_response=response)
    if uncertain_ids:
        repository.mark_live_orders_cancelled(uncertain_ids, status="cancel_unconfirmed", cancel_response=response)
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=opportunity_id,
        status=status,
        message=message,
        details={
            "order_ids": cleaned,
            "canceled_order_ids": canceled_ids,
            "unconfirmed_order_ids": uncertain_ids,
            "cancel_response": response,
        },
    )
    return canceled_ids, uncertain_ids, response


async def execute_near_close_taker_exits(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
    watch_books: dict[str, Any],
) -> list[dict[str, object]]:
    manager = NearCloseOrderManager(settings)
    exits: list[dict[str, object]] = []
    price_cache: dict[str, float | None] = {}
    client_holder: dict[str, CryptoPriceClient | None] = {"client": None}
    try:
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
            entry_price = float(group.get("open_cost_basis") or 0.0) / size if size > 1e-9 else 0.0
            reference_price = manager.taker_exit_reference_price(book=book) if book is not None else None
            if book is None or not manager.taker_exit_required(book=book, entry_price=entry_price):
                continue
            target_price = manager.taker_exit_price(book=book)
            if target_price is None or target_price <= 0:
                continue

            opportunity_id = f"stop-exit:{market_slug}:{token_id}"
            orderbook_telemetry = _book_telemetry(book)
            spread = _float_or_none(getattr(book, "spread", None))
            max_stop_spread = max(float(settings.near_close_stop_exit_max_spread), 0.0)
            orderbook_telemetry.update(
                {
                    "max_stop_exit_spread": max_stop_spread,
                    "panic_exit_wide_spread": bool(spread is not None and max_stop_spread > 0 and spread > max_stop_spread),
                }
            )

            outcome_label = str(group.get("outcome_label") or "Outcome")
            entry_metadata = repository.near_close_entry_metadata_for_position(
                market_slug=market_slug,
                token_id=token_id,
                outcome_label=outcome_label,
            )
            direction_allows_exit, direction_details = await _crypto_updown_direction_guard(
                settings=settings,
                market_slug=market_slug,
                outcome_label=outcome_label,
                entry_metadata=entry_metadata,
                price_cache=price_cache,
                client_holder=client_holder,
            )
            if not direction_allows_exit:
                _record_stop_exit_skip(
                    repository=repository,
                    exits=exits,
                    opportunity_id=opportunity_id,
                    status="stop_exit_skipped_crypto_direction_intact",
                    message="Skipped panic FAK stop-exit because the underlying crypto direction has not broken.",
                    market_slug=market_slug,
                    token_id=token_id,
                    reference_price=reference_price,
                    target_price=target_price,
                    entry_price=entry_price,
                    size=size,
                    details={
                        "stop_orderbook_source": book_source,
                        **direction_details,
                        **orderbook_telemetry,
                    },
                )
                continue

            assumed_fill = bool(group.get("assumed_fill"))
            source_order_id = str(group.get("source_order_id") or "").strip()
            if assumed_fill and not settings.near_close_assume_submitted_filled_stop_exit:
                continue

            active_maker_order_ids = [
                str(order.get("order_id") or "").strip()
                for order in repository.near_close_active_orders_for_market(
                    market_slug=market_slug,
                    token_id=token_id,
                )
                if str(order.get("order_id") or "").strip()
            ]
            source_cancel_response = None
            source_canceled_ids: list[str] = []
            source_uncertain_ids: list[str] = []
            if assumed_fill and source_order_id:
                source_canceled_ids, source_uncertain_ids, source_cancel_response = await _cancel_orders_for_panic_exit(
                    repository=repository,
                    live_trader=live_trader,
                    opportunity_id=opportunity_id,
                    order_ids=[source_order_id],
                    status="qualification_cancelled",
                    message="Cancelled assumed-fill maker entry before panic stop-exit.",
                )
                if source_order_id in source_canceled_ids and source_order_id not in source_uncertain_ids:
                    exits.append(
                        {
                            "market_slug": market_slug,
                            "token_id": token_id,
                            "status": "entry_cancelled_before_assumed_stop_exit",
                            "reference_price": reference_price,
                            "target_price": target_price,
                            "size": size,
                            "second_chance_attempted": False,
                        }
                    )
                    continue

            maker_cancel_ids = [order_id for order_id in active_maker_order_ids if order_id != source_order_id]
            maker_canceled_ids, maker_uncertain_ids, maker_cancel_response = await _cancel_orders_for_panic_exit(
                repository=repository,
                live_trader=live_trader,
                opportunity_id=opportunity_id,
                order_ids=maker_cancel_ids,
                status="panic_exit_cancelled_maker",
                message="Cancelled active near-close maker orders before panic taker exit.",
            )

            profit_take_order_ids = [
                str(order.get("order_id") or "").strip()
                for order in repository.near_close_active_profit_take_orders_for_position(
                    token_id=token_id,
                    market_slug=market_slug,
                )
                if str(order.get("order_id") or "").strip()
            ]
            profit_take_canceled_ids, profit_take_uncertain_ids, profit_take_cancel_response = await _cancel_orders_for_panic_exit(
                repository=repository,
                live_trader=live_trader,
                opportunity_id=opportunity_id,
                order_ids=profit_take_order_ids,
                status="stop_exit_cancelled_profit_take",
                message="Cancelled active profit-taking SELL before panic taker exit.",
            )

            plan = ExecutionPlan(
                opportunity_id=opportunity_id,
                summary=f"Panic taker stop exit on {market_slug} at {target_price:.4f}",
                legs=[
                    ExecutionLeg(
                        action="SELL",
                        token_id=token_id,
                        market_slug=market_slug,
                        outcome_label=outcome_label,
                        target_price=target_price,
                        size=size,
                        order_type="FAK",
                        post_only=False,
                        metadata={
                            "strategy_variant": "near_close_stop_exit",
                            "panic_exit": True,
                            "stop_trigger_price": settings.near_close_taker_exit_price,
                            "stop_reference_price": reference_price,
                            "stop_entry_price": entry_price,
                            "stop_limit_price": target_price,
                            "stop_slippage": settings.near_close_emergency_slippage,
                            "stop_orderbook_source": book_source,
                            **direction_details,
                            "assumed_fill_stop_exit": assumed_fill,
                            "source_order_id": source_order_id or None,
                            "source_cancel_response": source_cancel_response,
                            "source_canceled_order_ids": source_canceled_ids,
                            "source_uncertain_order_ids": source_uncertain_ids,
                            "maker_cancelled_order_ids": maker_canceled_ids,
                            "maker_uncertain_order_ids": maker_uncertain_ids,
                            "maker_cancel_response": maker_cancel_response,
                            "profit_take_cancelled_order_ids": profit_take_canceled_ids,
                            "profit_take_uncertain_order_ids": profit_take_uncertain_ids,
                            "profit_take_cancel_response": profit_take_cancel_response,
                            **orderbook_telemetry,
                        },
                    )
                ],
                max_slippage_bps=10.0,
                cancel_conditions=["Panic exit: cancel maker orders, then take immediately available liquidity."],
                requires_manual_approval=False,
                live_trading_allowed=True,
                strategy_type="near_close_stop_exit",
                metadata={"market_slug": market_slug, "token_id": token_id, "panic_exit": True},
            )
            live_result = await live_trader.execute(plan)
            repository.save_live_execution(live_result)
            execution_telemetry = _execution_telemetry(live_result.leg_results)
            repository.save_execution_event(
                source="watch",
                mode="live",
                opportunity_id=plan.opportunity_id,
                status=live_result.status,
                message=live_result.message,
                details={
                    "panic_exit": True,
                    "stop_trigger_price": settings.near_close_taker_exit_price,
                    "reference_price": reference_price,
                    "entry_price": entry_price,
                    "target_price": target_price,
                    "slippage": settings.near_close_emergency_slippage,
                    "orderbook_source": book_source,
                    **direction_details,
                    "assumed_fill_stop_exit": assumed_fill,
                    "source_order_id": source_order_id or None,
                    "source_cancel_response": source_cancel_response,
                    "source_canceled_order_ids": source_canceled_ids,
                    "source_uncertain_order_ids": source_uncertain_ids,
                    "maker_cancelled_order_ids": maker_canceled_ids,
                    "maker_uncertain_order_ids": maker_uncertain_ids,
                    "maker_cancel_response": maker_cancel_response,
                    "profit_take_cancelled_order_ids": profit_take_canceled_ids,
                    "profit_take_uncertain_order_ids": profit_take_uncertain_ids,
                    "profit_take_cancel_response": profit_take_cancel_response,
                    "second_chance_enabled": False,
                    "second_chance_attempted": False,
                    "market_slug": market_slug,
                    "token_id": token_id,
                    **orderbook_telemetry,
                    "execution": execution_telemetry,
                    "legs": [leg.model_dump() for leg in live_result.leg_results],
                },
            )
            exits.append(
                {
                    "market_slug": market_slug,
                    "token_id": token_id,
                    "status": live_result.status,
                    "reference_price": reference_price,
                    "target_price": target_price,
                    "first_target_price": target_price,
                    "second_chance_attempted": False,
                    "size": size,
                    **orderbook_telemetry,
                    "execution": execution_telemetry,
                }
            )
    finally:
        client = client_holder.get("client")
        if client is not None:
            await client.close()
    return exits
