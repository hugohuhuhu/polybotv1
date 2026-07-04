from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.clients.crypto_price_client import CryptoPriceClient, CryptoPriceObservation, binance_symbol_for_asset
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
    last_trade_at = getattr(book, "last_trade_at", None)
    last_trade_age_sec = None
    if isinstance(last_trade_at, datetime):
        last_trade_age_sec = max((datetime.now(timezone.utc) - last_trade_at.astimezone(timezone.utc)).total_seconds(), 0.0)
    return {
        "observed_best_bid": best_bid,
        "observed_best_ask": best_ask,
        "observed_midpoint": getattr(book, "midpoint", None),
        "observed_spread": getattr(book, "spread", None),
        "observed_top_bid_size": book.depth_for_side("bid", best_bid) if best_bid is not None else None,
        "observed_top_ask_size": book.depth_for_side("ask", best_ask) if best_ask is not None else None,
        "observed_last_trade_price": getattr(book, "last_trade_price", None),
        "observed_last_trade_at": last_trade_at.isoformat() if isinstance(last_trade_at, datetime) else None,
        "observed_last_trade_age_sec": last_trade_age_sec,
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


def _execution_size_from_leg(leg: LiveExecutionLegResult) -> float | None:
    candidates = (
        leg.response.get("matched_size"),
        leg.response.get("matched_amount"),
        leg.response.get("matchedAmount"),
        leg.response.get("filled_size"),
        leg.response.get("size"),
    )
    for value in candidates:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            return numeric
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
            "reported_matched_size": _execution_size_from_leg(leg),
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


def _time_to_resolution_sec(market_slug: str) -> float | None:
    end_ts = ScannerRepository._parse_slug_end_timestamp(str(market_slug or ""))
    if end_ts is None:
        return None
    return max(end_ts - datetime.now(timezone.utc).timestamp(), 0.0)


def stop_exit_monitor_required(
    market_slug: str,
    settings: Settings,
    *,
    at: datetime | None = None,
) -> bool:
    end_ts = ScannerRepository._parse_slug_end_timestamp(str(market_slug or ""))
    if end_ts is None:
        return True
    checked_at = at or datetime.now(timezone.utc)
    grace_sec = max(float(settings.near_close_stop_exit_settlement_grace_sec), 0.0)
    return checked_at.timestamp() <= end_ts + grace_sec


def _direction_still_valid(details: dict[str, object]) -> bool | None:
    if "crypto_direction_broken" in details:
        return not bool(details.get("crypto_direction_broken"))
    reason = str(details.get("crypto_direction_guard_reason") or "")
    if reason == "direction_still_valid":
        return True
    if reason == "direction_broken":
        return False
    return None


async def _crypto_price_observation(
    *,
    settings: Settings,
    symbol: str,
    cache: dict[str, CryptoPriceObservation | None],
    client_holder: dict[str, CryptoPriceClient | None],
) -> CryptoPriceObservation | None:
    if symbol in cache:
        return cache[symbol]
    client = client_holder.get("client")
    if client is None:
        client = CryptoPriceClient(
            timeout=settings.crypto_price_timeout_sec,
            source=settings.crypto_price_source,
            rpc_url=settings.polygon_rpc_url,
            chainlink_feeds=settings.chainlink_price_feed_addresses,
            chainlink_stale_after_sec=settings.chainlink_price_stale_sec,
        )
        client_holder["client"] = client
    observations = await client.get_price_observations({symbol})
    observation = observations.get(symbol)
    cache[symbol] = observation
    return observation


async def _crypto_updown_direction_guard(
    *,
    settings: Settings,
    market_slug: str,
    outcome_label: str,
    entry_metadata: dict[str, Any],
    price_cache: dict[str, CryptoPriceObservation | None],
    client_holder: dict[str, CryptoPriceClient | None],
    time_to_resolution_sec: float | None = None,
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
            "crypto_stop_price_source": settings.crypto_price_source,
        }
    )
    if start_price is None or start_price <= 0 or outcome not in {"up", "down"}:
        details["crypto_direction_guard_reason"] = "missing_entry_direction_metadata"
        return True, details
    if symbol is None:
        details["crypto_direction_guard_reason"] = "missing_symbol"
        return False, details
    observation = await _crypto_price_observation(
        settings=settings,
        symbol=symbol,
        cache=price_cache,
        client_holder=client_holder,
    )
    spot = observation.price if observation is not None else None
    details["crypto_stop_spot_price"] = spot
    if spot is None:
        details["crypto_direction_guard_reason"] = "spot_unavailable"
        return False, details
    distance = abs(spot - start_price) / start_price
    price_age_sec = max(datetime.now(timezone.utc).timestamp() - observation.updated_at, 0.0)
    max_price_age_sec = max(float(settings.near_close_crypto_updown_stop_price_max_age_sec), 0.0)
    price_fresh = max_price_age_sec <= 0 or price_age_sec <= max_price_age_sec
    break_buffer = max(float(settings.near_close_crypto_updown_stop_direction_break_buffer), 0.0)
    final_seconds_zero_buffer = bool(
        settings.near_close_crypto_updown_stop_hard_override_enabled
        and time_to_resolution_sec is not None
        and time_to_resolution_sec <= max(
            float(settings.near_close_crypto_updown_stop_hard_override_max_seconds),
            0.0,
        )
    )
    effective_break_buffer = 0.0 if final_seconds_zero_buffer else break_buffer
    details.update(
        {
            "crypto_stop_start_distance": distance,
            "crypto_stop_price_updated_at": datetime.fromtimestamp(observation.updated_at, timezone.utc).isoformat(),
            "crypto_stop_price_age_sec": price_age_sec,
            "crypto_stop_price_max_age_sec": max_price_age_sec,
            "crypto_stop_price_fresh": price_fresh,
            "crypto_direction_break_buffer": break_buffer,
            "crypto_direction_effective_break_buffer": effective_break_buffer,
            "crypto_direction_final_seconds_zero_buffer": final_seconds_zero_buffer,
        }
    )
    if not price_fresh:
        details["crypto_direction_guard_reason"] = "spot_stale"
        return False, details
    if outcome == "up":
        direction_broken = spot < start_price * (1.0 - effective_break_buffer)
    else:
        direction_broken = spot > start_price * (1.0 + effective_break_buffer)
    details.update(
        {
            "crypto_direction_broken": direction_broken,
            "crypto_direction_guard_reason": "direction_broken" if direction_broken else "direction_still_valid",
        }
    )
    return direction_broken, details


def _crypto_updown_stop_neutral_override(
    *,
    settings: Settings,
    market_slug: str,
    time_to_resolution_sec: float | None,
    direction_details: dict[str, object],
    orderbook_telemetry: dict[str, object],
    size: float,
) -> tuple[bool, dict[str, object]]:
    enabled = bool(settings.near_close_crypto_updown_stop_neutral_override_enabled)
    min_seconds = max(float(settings.near_close_crypto_updown_stop_neutral_override_min_seconds), 0.0)
    max_seconds = max(float(settings.near_close_crypto_updown_stop_neutral_override_max_seconds), 0.0)
    max_distance = max(float(settings.near_close_crypto_updown_stop_neutral_override_max_distance), 0.0)
    max_bid = max(float(settings.near_close_crypto_updown_stop_neutral_override_max_bid), 0.0)
    max_midpoint = max(float(settings.near_close_crypto_updown_stop_neutral_override_max_midpoint), 0.0)
    max_spread = max(float(settings.near_close_crypto_updown_stop_neutral_override_max_spread), 0.0)
    best_bid = _float_or_none(orderbook_telemetry.get("observed_best_bid"))
    midpoint = _float_or_none(orderbook_telemetry.get("observed_midpoint"))
    spread = _float_or_none(orderbook_telemetry.get("observed_spread"))
    bid_depth = _float_or_none(orderbook_telemetry.get("observed_top_bid_size"))
    start_distance = _float_or_none(direction_details.get("crypto_stop_start_distance"))
    guard_reason = str(direction_details.get("crypto_direction_guard_reason") or "")
    within_time_window = bool(
        enabled
        and "updown" in str(market_slug).lower()
        and max_seconds > min_seconds
        and time_to_resolution_sec is not None
        and min_seconds < time_to_resolution_sec <= max_seconds
    )
    chainlink_neutral = bool(start_distance is not None and start_distance <= max_distance)
    chainlink_unavailable = guard_reason in {"spot_stale", "spot_unavailable"}
    bid_collapsed = bool(max_bid > 0 and best_bid is not None and best_bid <= max_bid)
    midpoint_collapsed = bool(max_midpoint > 0 and midpoint is not None and midpoint <= max_midpoint)
    spread_tradeable = bool(max_spread > 0 and spread is not None and spread <= max_spread)
    depth_sufficient = bool(bid_depth is not None and bid_depth + 1e-9 >= max(size, 0.0))
    active = bool(
        within_time_window
        and (chainlink_neutral or chainlink_unavailable)
        and bid_collapsed
        and midpoint_collapsed
        and spread_tradeable
        and depth_sufficient
    )
    reasons: list[str] = []
    if within_time_window:
        reasons.append("neutral_window")
    if chainlink_neutral:
        reasons.append("chainlink_near_start")
    elif chainlink_unavailable:
        reasons.append("chainlink_stale_or_unavailable")
    if bid_collapsed:
        reasons.append("best_bid_collapse")
    if midpoint_collapsed:
        reasons.append("midpoint_collapse")
    if spread_tradeable:
        reasons.append("spread_tradeable")
    if depth_sufficient:
        reasons.append("bid_depth_sufficient")
    return active, {
        "crypto_direction_neutral_override_enabled": enabled,
        "crypto_direction_neutral_override_active": active,
        "crypto_direction_neutral_override_reasons": reasons,
        "crypto_direction_neutral_override_time_to_resolution_sec": time_to_resolution_sec,
        "crypto_direction_neutral_override_min_seconds": min_seconds,
        "crypto_direction_neutral_override_max_seconds": max_seconds,
        "crypto_direction_neutral_override_max_distance": max_distance,
        "crypto_direction_neutral_override_start_distance": start_distance,
        "crypto_direction_neutral_override_max_bid": max_bid,
        "crypto_direction_neutral_override_max_midpoint": max_midpoint,
        "crypto_direction_neutral_override_max_spread": max_spread,
        "crypto_direction_neutral_override_best_bid": best_bid,
        "crypto_direction_neutral_override_midpoint": midpoint,
        "crypto_direction_neutral_override_spread": spread,
        "crypto_direction_neutral_override_bid_depth": bid_depth,
        "crypto_direction_neutral_override_required_depth": max(size, 0.0),
    }


def _crypto_updown_stop_hard_override(
    *,
    settings: Settings,
    market_slug: str,
    time_to_resolution_sec: float | None,
    orderbook_telemetry: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    enabled = bool(settings.near_close_crypto_updown_stop_hard_override_enabled)
    max_seconds = max(float(settings.near_close_crypto_updown_stop_hard_override_max_seconds), 0.0)
    max_bid = max(float(settings.near_close_crypto_updown_stop_hard_override_max_bid), 0.0)
    max_midpoint = max(float(settings.near_close_crypto_updown_stop_hard_override_max_midpoint), 0.0)
    last_trade_max_age_sec = max(
        float(settings.near_close_crypto_updown_stop_hard_override_last_trade_max_age_sec),
        0.0,
    )
    best_bid = _float_or_none(orderbook_telemetry.get("observed_best_bid"))
    midpoint = _float_or_none(orderbook_telemetry.get("observed_midpoint"))
    last_trade_price = _float_or_none(orderbook_telemetry.get("observed_last_trade_price"))
    last_trade_age_sec = _float_or_none(orderbook_telemetry.get("observed_last_trade_age_sec"))
    within_time_window = bool(
        enabled
        and max_seconds > 0
        and time_to_resolution_sec is not None
        and time_to_resolution_sec <= max_seconds
    )
    bid_collapsed = bool(max_bid > 0 and best_bid is not None and best_bid <= max_bid)
    midpoint_collapsed = bool(max_midpoint > 0 and midpoint is not None and midpoint <= max_midpoint)
    last_trade_threshold = max(max_bid, max_midpoint)
    last_trade_fresh = bool(
        last_trade_age_sec is not None
        and (last_trade_max_age_sec <= 0 or last_trade_age_sec <= last_trade_max_age_sec)
    )
    last_trade_collapsed = bool(
        last_trade_threshold > 0
        and last_trade_price is not None
        and last_trade_price <= last_trade_threshold
    )
    reasons: list[str] = []
    if within_time_window and "updown" in str(market_slug).lower():
        reasons.append("final_seconds")
        if bid_collapsed:
            reasons.append("best_bid_collapse")
        if midpoint_collapsed:
            reasons.append("midpoint_collapse")
        if last_trade_fresh:
            reasons.append("last_trade_fresh")
        if last_trade_collapsed:
            reasons.append("last_trade_collapse")
    active = bool(
        within_time_window
        and "updown" in str(market_slug).lower()
        and (bid_collapsed or midpoint_collapsed)
        and last_trade_fresh
        and last_trade_collapsed
    )
    return active, {
        "crypto_direction_hard_override_enabled": enabled,
        "crypto_direction_hard_override_active": active,
        "crypto_direction_hard_override_reasons": reasons,
        "crypto_direction_hard_override_time_to_resolution_sec": time_to_resolution_sec,
        "crypto_direction_hard_override_max_seconds": max_seconds,
        "crypto_direction_hard_override_within_time_window": within_time_window,
        "crypto_direction_hard_override_max_bid": max_bid,
        "crypto_direction_hard_override_max_midpoint": max_midpoint,
        "crypto_direction_hard_override_best_bid": best_bid,
        "crypto_direction_hard_override_midpoint": midpoint,
        "crypto_direction_hard_override_last_trade_price": last_trade_price,
        "crypto_direction_hard_override_last_trade_age_sec": last_trade_age_sec,
        "crypto_direction_hard_override_last_trade_max_age_sec": last_trade_max_age_sec,
        "crypto_direction_hard_override_last_trade_threshold": last_trade_threshold,
        "crypto_direction_hard_override_last_trade_fresh": last_trade_fresh,
        "crypto_direction_hard_override_last_trade_collapsed": last_trade_collapsed,
    }


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


def _record_stop_exit_check(
    *,
    repository: ScannerRepository,
    opportunity_id: str,
    trade_autopsy_id: str,
    market_slug: str,
    token_id: str,
    outcome_label: str,
    entry_price: float,
    size: float,
    book_source: str,
    reference_price: float | None,
    target_price: float | None,
    stop_required: bool,
    stop_reason: str,
    direction_details: dict[str, object],
    orderbook_telemetry: dict[str, object],
) -> None:
    payload = {
        "trade_autopsy_id": trade_autopsy_id,
        "event_type": "stop_check",
        "check_timestamp": datetime.now(timezone.utc).isoformat(),
        "market_slug": market_slug,
        "token_id": token_id,
        "outcome": outcome_label,
        "entry_price": entry_price,
        "size": size,
        "time_to_resolution_sec": _time_to_resolution_sec(market_slug),
        "stop_orderbook_source": book_source,
        "reference_price": reference_price,
        "target_price": target_price,
        "stop_required": stop_required,
        "stop_reason": stop_reason,
        "expected_winning_outcome": direction_details.get("crypto_stop_outcome") or outcome_label,
        "direction_still_valid": _direction_still_valid(direction_details),
        "crypto_spot": direction_details.get("crypto_stop_spot_price"),
        "crypto_start_price": direction_details.get("crypto_stop_start_price"),
        "current_start_distance": direction_details.get("crypto_stop_start_distance"),
        **direction_details,
        **orderbook_telemetry,
    }
    repository.save_execution_event(
        source="trade-autopsy",
        mode="live",
        opportunity_id=opportunity_id,
        status="trade_autopsy_stop_check",
        message=f"Near-close stop/exit check: {stop_reason}.",
        details=payload,
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
    price_cache: dict[str, CryptoPriceObservation | None] = {}
    client_holder: dict[str, CryptoPriceClient | None] = {"client": None}
    try:
        for group in repository.near_close_stop_exit_groups(limit=50):
            if float(group.get("open_size") or 0.0) <= 1e-9:
                continue
            token_id = str(group.get("token_id") or "")
            market_slug = str(group.get("market_slug") or "")
            if "updown" not in market_slug:
                continue
            if not stop_exit_monitor_required(market_slug, settings):
                continue
            book = watch_books.get(token_id)
            book_source = "watch"
            if book is None:
                book = _fallback_stop_exit_book(repository=repository, settings=settings, token_id=token_id)
                book_source = "stored_orderbook" if book is not None else "missing"
            size = float(group.get("open_size") or 0.0)
            entry_price = float(group.get("open_cost_basis") or 0.0) / size if size > 1e-9 else 0.0
            opportunity_id = f"stop-exit:{market_slug}:{token_id}"
            outcome_label = str(group.get("outcome_label") or "Outcome")
            entry_metadata = repository.near_close_entry_metadata_for_position(
                market_slug=market_slug,
                token_id=token_id,
                outcome_label=outcome_label,
            )
            trade_autopsy_id = str(entry_metadata.get("trade_autopsy_id") or "").strip()
            if not trade_autopsy_id:
                trade_autopsy_id = ScannerRepository.make_trade_autopsy_id(
                    market_slug=market_slug,
                    token_id=token_id,
                    outcome_label=outcome_label,
                    order_id=str(group.get("source_order_id") or ""),
                    opportunity_id=str(group.get("opportunity_id") or ""),
                    created_at=str(group.get("latest_at") or ""),
                )
            time_to_resolution_sec = _time_to_resolution_sec(market_slug)
            direction_allows_exit, direction_details = await _crypto_updown_direction_guard(
                settings=settings,
                market_slug=market_slug,
                outcome_label=outcome_label,
                entry_metadata=entry_metadata,
                price_cache=price_cache,
                client_holder=client_holder,
                time_to_resolution_sec=time_to_resolution_sec,
            )
            reference_price = manager.taker_exit_reference_price(book=book) if book is not None else None
            orderbook_telemetry = _book_telemetry(book)
            spread = _float_or_none(getattr(book, "spread", None)) if book is not None else None
            max_stop_spread = max(float(settings.near_close_stop_exit_max_spread), 0.0)
            orderbook_telemetry.update(
                {
                    "max_stop_exit_spread": max_stop_spread,
                    "panic_exit_wide_spread": bool(spread is not None and max_stop_spread > 0 and spread > max_stop_spread),
                }
            )
            hard_override_allows_exit, hard_override_details = _crypto_updown_stop_hard_override(
                settings=settings,
                market_slug=market_slug,
                time_to_resolution_sec=time_to_resolution_sec,
                orderbook_telemetry=orderbook_telemetry,
            )
            neutral_override_allows_exit, neutral_override_details = _crypto_updown_stop_neutral_override(
                settings=settings,
                market_slug=market_slug,
                time_to_resolution_sec=time_to_resolution_sec,
                direction_details=direction_details,
                orderbook_telemetry=orderbook_telemetry,
                size=size,
            )
            effective_direction_allows_exit = (
                direction_allows_exit or neutral_override_allows_exit or hard_override_allows_exit
            )
            stop_details = {**direction_details, **neutral_override_details, **hard_override_details}
            if book is None:
                _record_stop_exit_check(
                    repository=repository,
                    opportunity_id=opportunity_id,
                    trade_autopsy_id=trade_autopsy_id,
                    market_slug=market_slug,
                    token_id=token_id,
                    outcome_label=outcome_label,
                    entry_price=entry_price,
                    size=size,
                    book_source=book_source,
                    reference_price=reference_price,
                    target_price=None,
                    stop_required=False,
                    stop_reason="missing_orderbook",
                    direction_details=stop_details,
                    orderbook_telemetry=orderbook_telemetry,
                )
                continue
            stop_required = manager.taker_exit_required(book=book, entry_price=entry_price)
            target_price = manager.taker_exit_price(book=book)
            if not stop_required:
                _record_stop_exit_check(
                    repository=repository,
                    opportunity_id=opportunity_id,
                    trade_autopsy_id=trade_autopsy_id,
                    market_slug=market_slug,
                    token_id=token_id,
                    outcome_label=outcome_label,
                    entry_price=entry_price,
                    size=size,
                    book_source=book_source,
                    reference_price=reference_price,
                    target_price=target_price,
                    stop_required=False,
                    stop_reason="stop_not_triggered",
                    direction_details=stop_details,
                    orderbook_telemetry=orderbook_telemetry,
                )
                continue
            if target_price is None or target_price <= 0:
                _record_stop_exit_check(
                    repository=repository,
                    opportunity_id=opportunity_id,
                    trade_autopsy_id=trade_autopsy_id,
                    market_slug=market_slug,
                    token_id=token_id,
                    outcome_label=outcome_label,
                    entry_price=entry_price,
                    size=size,
                    book_source=book_source,
                    reference_price=reference_price,
                    target_price=target_price,
                    stop_required=True,
                    stop_reason="invalid_exit_target",
                    direction_details=stop_details,
                    orderbook_telemetry=orderbook_telemetry,
                )
                continue
            _record_stop_exit_check(
                repository=repository,
                opportunity_id=opportunity_id,
                trade_autopsy_id=trade_autopsy_id,
                market_slug=market_slug,
                token_id=token_id,
                outcome_label=outcome_label,
                entry_price=entry_price,
                size=size,
                book_source=book_source,
                reference_price=reference_price,
                target_price=target_price,
                stop_required=True,
                stop_reason="stop_triggered",
                direction_details=stop_details,
                orderbook_telemetry=orderbook_telemetry,
            )
            if hard_override_allows_exit and not direction_allows_exit:
                repository.save_execution_event(
                    source="watch",
                    mode="live",
                    opportunity_id=opportunity_id,
                    status="stop_exit_crypto_direction_hard_override",
                    message="Panic FAK stop-exit bypassed crypto direction guard because hard risk override fired.",
                    details={
                        "market_slug": market_slug,
                        "token_id": token_id,
                        "reference_price": reference_price,
                        "target_price": target_price,
                        "entry_price": entry_price,
                        "size": size,
                        "trade_autopsy_id": trade_autopsy_id,
                        "stop_orderbook_source": book_source,
                        **stop_details,
                        **orderbook_telemetry,
                    },
                )
            if neutral_override_allows_exit and not direction_allows_exit:
                repository.save_execution_event(
                    source="watch",
                    mode="live",
                    opportunity_id=opportunity_id,
                    status="stop_exit_crypto_direction_neutral_override",
                    message="Panic FAK stop-exit used the near-tie Chainlink and liquid-book risk override.",
                    details={
                        "market_slug": market_slug,
                        "token_id": token_id,
                        "reference_price": reference_price,
                        "target_price": target_price,
                        "entry_price": entry_price,
                        "size": size,
                        "trade_autopsy_id": trade_autopsy_id,
                        "stop_orderbook_source": book_source,
                        **stop_details,
                        **orderbook_telemetry,
                    },
                )
            if not effective_direction_allows_exit:
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
                        "trade_autopsy_id": trade_autopsy_id,
                        "stop_orderbook_source": book_source,
                        **stop_details,
                        **orderbook_telemetry,
                    },
                )
                continue

            assumed_fill = bool(group.get("assumed_fill"))
            source_order_id = str(group.get("source_order_id") or "").strip()
            if assumed_fill and not settings.near_close_assume_submitted_filled_stop_exit:
                _record_stop_exit_check(
                    repository=repository,
                    opportunity_id=opportunity_id,
                    trade_autopsy_id=trade_autopsy_id,
                    market_slug=market_slug,
                    token_id=token_id,
                    outcome_label=outcome_label,
                    entry_price=entry_price,
                    size=size,
                    book_source=book_source,
                    reference_price=reference_price,
                    target_price=target_price,
                    stop_required=True,
                    stop_reason="assumed_fill_stop_exit_disabled",
                    direction_details=stop_details,
                    orderbook_telemetry=orderbook_telemetry,
                )
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
                            "trade_autopsy_id": trade_autopsy_id,
                            "panic_exit": True,
                            "exit_trigger_reason": "stop_triggered",
                            "attempted_exit_price": target_price,
                            "observed_bid_before_exit": orderbook_telemetry.get("observed_best_bid"),
                            "stop_trigger_price": settings.near_close_taker_exit_price,
                            "stop_reference_price": reference_price,
                            "stop_entry_price": entry_price,
                            "stop_limit_price": target_price,
                            "stop_slippage": settings.near_close_emergency_slippage,
                            "stop_orderbook_source": book_source,
                            **stop_details,
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
            first_execution = execution_telemetry[0] if execution_telemetry else {}
            actual_matched_size = _float_or_none(first_execution.get("reported_matched_size"))
            actual_matched_price = _float_or_none(first_execution.get("reported_execution_price"))
            remaining_position = (max(size - actual_matched_size, 0.0) if actual_matched_size is not None else None)
            exit_autopsy_details = {
                "trade_autopsy_id": trade_autopsy_id,
                "event_type": "panic_exit_attempt",
                "trigger_reason": "stop_triggered",
                "market_slug": market_slug,
                "token_id": token_id,
                "outcome": outcome_label,
                "time_to_resolution_sec": _time_to_resolution_sec(market_slug),
                "observed_bid_before_exit": orderbook_telemetry.get("observed_best_bid"),
                "attempted_exit_price": target_price,
                "order_type": live_result.order_type,
                "order_id": first_execution.get("order_id"),
                "exit_status": live_result.status,
                "response": first_execution.get("response") if first_execution else None,
                "actual_matched_size": actual_matched_size,
                "actual_matched_price": actual_matched_price,
                "remaining_position": remaining_position,
                "reference_price": reference_price,
                "entry_price": entry_price,
                **stop_details,
                **orderbook_telemetry,
            }
            repository.save_execution_event(
                source="trade-autopsy",
                mode="live",
                opportunity_id=plan.opportunity_id,
                status="trade_autopsy_exit",
                message="Panic stop-exit attempt autopsy snapshot recorded.",
                details=exit_autopsy_details,
            )
            repository.save_execution_event(
                source="watch",
                mode="live",
                opportunity_id=plan.opportunity_id,
                status=live_result.status,
                message=live_result.message,
                details={
                    "trade_autopsy_id": trade_autopsy_id,
                    "panic_exit": True,
                    "stop_trigger_price": settings.near_close_taker_exit_price,
                    "reference_price": reference_price,
                    "entry_price": entry_price,
                    "target_price": target_price,
                    "slippage": settings.near_close_emergency_slippage,
                    "orderbook_source": book_source,
                    **stop_details,
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
                    "trade_autopsy_id": trade_autopsy_id,
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
