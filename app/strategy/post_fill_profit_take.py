from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan, OrderBookSnapshot
from app.models.runtime import TradingControls
from app.storage.repositories import ScannerRepository
from app.strategy.risk_manager import RiskManager
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter
from app.utils.time_utils import parse_datetime


@dataclass(slots=True)
class ProfitTakeDecision:
    should_place: bool
    shadow_only: bool
    reason: str
    plan: ExecutionPlan | None
    details: dict[str, Any]


def _minutes_until(raw_end_date: Any) -> float | None:
    end_date = parse_datetime(raw_end_date)
    if end_date is None:
        return None
    return (end_date - datetime.now(timezone.utc)).total_seconds() / 60.0


def parse_profit_take_ladder(raw_ladder: str) -> list[tuple[float, float]]:
    ladder: list[tuple[float, float]] = []
    for raw_item in str(raw_ladder or "").split(","):
        item = raw_item.strip()
        if not item or ":" not in item:
            continue
        raw_entry, raw_target = item.split(":", 1)
        try:
            entry = float(raw_entry)
            target = float(raw_target)
        except (TypeError, ValueError):
            continue
        if entry > 0 and target > 0:
            ladder.append((entry, target))
    return sorted(ladder, key=lambda pair: pair[0])


def profit_take_target_price(settings: Settings, entry_price: float) -> float | None:
    for max_entry, target in parse_profit_take_ladder(settings.near_close_profit_take_ladder):
        if entry_price <= max_entry:
            return target
    return None


def _position_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("market_slug") or ""),
        str(item.get("token_id") or ""),
        str(item.get("outcome_label") or ""),
    )


def _open_position_size(repository: ScannerRepository, entry: dict[str, Any]) -> float:
    key = _position_key(entry)
    for group in repository.live_trade_groups(limit=50):
        if _position_key(group) == key:
            return float(group.get("open_size") or 0.0)
    return 0.0


def _estimated_taker_fee(settings: Settings, *, price: float, size: float) -> float:
    return max(size, 0.0) * max(settings.near_close_profit_take_taker_fee_rate, 0.0) * price * (1.0 - price)


def build_post_fill_profit_take_decision(
    *,
    entry: dict[str, Any],
    book: OrderBookSnapshot | None,
    settings: Settings,
    controls: TradingControls,
    repository: ScannerRepository,
) -> ProfitTakeDecision:
    entry_order_id = str(entry.get("order_id") or "").strip()
    token_id = str(entry.get("token_id") or "").strip()
    market_slug = str(entry.get("market_slug") or "")
    outcome_label = str(entry.get("outcome_label") or "Outcome")
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    entry_price = float(entry.get("target_price") or 0.0)
    entry_size = float(entry.get("requested_size") or 0.0)
    target_price = profit_take_target_price(settings, entry_price)
    minutes_to_close = _minutes_until(entry.get("end_date"))
    open_size = _open_position_size(repository, entry)
    order_type = str(settings.near_close_profit_take_order_type or "GTD").upper()

    details: dict[str, Any] = {
        "strategy_variant": "near_close_profit_take",
        "profit_take_role": "post_fill_limit_sell",
        "profit_take_for_order_id": entry_order_id,
        "entry_trade_id": entry.get("id"),
        "entry_market_id": entry.get("entry_market_id"),
        "entry_market_slug": market_slug,
        "entry_outcome": outcome_label,
        "entry_token_id": token_id,
        "entry_price": entry_price,
        "entry_fill_time": entry.get("created_at"),
        "entry_order_id": entry_order_id,
        "time_to_close_at_entry": response.get("minutes_to_resolution"),
        "time_to_close_at_profit_take": minutes_to_close,
        "profit_take_target_price": target_price,
        "open_size": open_size,
    }

    if not settings.near_close_profit_take_enabled:
        return ProfitTakeDecision(False, False, "profit_take_disabled", None, details)
    if not entry_order_id or not token_id or entry_price <= 0 or entry_size <= 0:
        return ProfitTakeDecision(False, False, "invalid_entry", None, details)
    if target_price is None:
        return ProfitTakeDecision(False, False, "entry_above_profit_take_ladder", None, details)
    if target_price <= entry_price:
        return ProfitTakeDecision(False, False, "target_not_above_entry", None, details)
    if repository.profit_take_order_exists_for_entry(entry_order_id):
        return ProfitTakeDecision(False, False, "duplicate_profit_take", None, details)
    if repository.near_close_stop_exit_order_exists_for_token(token_id):
        return ProfitTakeDecision(False, False, "stop_exit_exists", None, details)
    if bool(entry.get("closed")) or entry.get("active") is False or entry.get("active") == 0:
        return ProfitTakeDecision(False, False, "market_closed", None, details)
    if minutes_to_close is None:
        return ProfitTakeDecision(False, False, "missing_market_close_time", None, details)
    if minutes_to_close < settings.near_close_profit_take_min_minutes_to_end:
        return ProfitTakeDecision(False, False, "too_close_to_resolution", None, details)
    if settings.risk_kill_switch or controls.kill_switch_enabled:
        return ProfitTakeDecision(False, False, "kill_switch_enabled", None, details)
    if open_size <= 1e-9:
        return ProfitTakeDecision(False, False, "no_open_position", None, details)
    if book is None:
        return ProfitTakeDecision(False, False, "missing_orderbook", None, details)
    if book.best_bid is None or book.best_ask is None or book.spread is None:
        return ProfitTakeDecision(False, False, "incomplete_orderbook", None, details)
    details.update({"best_bid": book.best_bid, "best_ask": book.best_ask, "spread": book.spread})
    if book.spread > settings.near_close_profit_take_max_spread:
        return ProfitTakeDecision(False, False, "spread_above_max", None, details)

    size = min(entry_size, open_size, settings.live_max_order_size)
    direct_taker = book.best_bid >= target_price
    estimated_fee = _estimated_taker_fee(settings, price=target_price, size=size) if direct_taker else 0.0
    gross_profit = (target_price - entry_price) * size
    net_profit = gross_profit - estimated_fee
    details.update(
        {
            "profit_take_order_type": "FAK" if direct_taker else order_type,
            "profit_take_post_only": not direct_taker,
            "profit_take_size": size,
            "estimated_taker_fee": estimated_fee,
            "gross_profit_if_filled": gross_profit,
            "net_profit_if_filled": net_profit,
            "missed_redeem_profit_per_share": 1.0 - target_price,
        }
    )
    if net_profit < settings.near_close_profit_take_min_net_profit:
        return ProfitTakeDecision(False, False, "net_profit_below_minimum", None, details)
    if direct_taker:
        bid_depth = book.depth_for_side("bid", target_price)
        details["bid_depth_at_target"] = bid_depth
        if bid_depth < min(size, settings.near_close_profit_take_min_depth):
            return ProfitTakeDecision(False, False, "insufficient_bid_depth", None, details)
        order_type = "FAK"
        post_only = False
        expiration_sec = None
    else:
        bid_depth = book.depth_for_side("bid", book.best_bid)
        details["top_bid_depth"] = bid_depth
        if bid_depth < settings.near_close_profit_take_min_depth:
            return ProfitTakeDecision(False, False, "insufficient_bid_depth", None, details)
        post_only = True
        expiration_sec = settings.near_close_profit_take_gtd_seconds if order_type == "GTD" else None

    plan = ExecutionPlan(
        opportunity_id=f"profit-take:{entry_order_id}",
        summary=f"Post-fill profit take for {market_slug} {outcome_label} at {target_price:.4f}",
        legs=[
            ExecutionLeg(
                action="SELL",
                token_id=token_id,
                market_slug=market_slug,
                outcome_label=outcome_label,
                target_price=target_price,
                size=size,
                order_type=order_type,
                post_only=post_only,
                expiration_sec=expiration_sec,
                metadata=details,
            )
        ],
        max_slippage_bps=0.0,
        cancel_conditions=[
            "Entry fill must already be confirmed.",
            "SELL size must be backed by the open local position.",
            "Stop-exit may cancel this order if downside protection is triggered.",
        ],
        requires_manual_approval=True,
        live_trading_allowed=True,
        strategy_type="late_resolution",
        metadata=details,
    )
    risk = RiskManager(settings).assess(plan, repository, mode="live")
    details.update(
        {
            "risk_reason": risk.reason,
            "risk_estimated_notional": risk.estimated_notional,
            "risk_projected_daily_notional": risk.projected_daily_notional,
            "risk_projected_daily_orders": risk.projected_daily_orders,
        }
    )
    if not risk.allowed:
        return ProfitTakeDecision(False, False, "risk_blocked", None, details)

    shadow_only = (
        not settings.near_close_profit_take_live_enabled
        or not settings.enable_live_trading
        or not controls.armed
    )
    if shadow_only:
        return ProfitTakeDecision(
            False,
            settings.near_close_profit_take_shadow_enabled,
            "shadow_mode",
            plan,
            details,
        )
    return ProfitTakeDecision(True, False, "ready", plan, details)


async def execute_post_fill_profit_takes(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
    controls: TradingControls,
    watch_books: dict[str, OrderBookSnapshot],
    source: str = "watch",
) -> int:
    executed = 0
    for entry in repository.near_close_filled_entries_without_profit_take(limit=25):
        token_id = str(entry.get("token_id") or "")
        decision = build_post_fill_profit_take_decision(
            entry=entry,
            book=watch_books.get(token_id),
            settings=settings,
            controls=controls,
            repository=repository,
        )
        if decision.shadow_only:
            repository.save_execution_event(
                source=source,
                mode="shadow",
                opportunity_id=decision.plan.opportunity_id if decision.plan else None,
                status="profit_take_shadow",
                message="Post-fill profit-take decision simulated; no live order placed.",
                details=decision.details,
            )
            continue
        if not decision.should_place or decision.plan is None:
            repository.save_execution_event_once(
                source=source,
                mode="live",
                opportunity_id=decision.plan.opportunity_id if decision.plan else None,
                status="profit_take_skipped",
                message=decision.reason,
                details=decision.details,
            )
            continue

        claim_key = f"profit-take:{decision.details.get('profit_take_for_order_id')}"
        claimed = repository.claim_execution(
            claim_key=claim_key,
            opportunity_id=decision.plan.opportunity_id,
            source=source,
            mode="live",
            message="Post-fill profit-take claimed by watch loop.",
        )
        if not claimed:
            repository.save_execution_event(
                source=source,
                mode="live",
                opportunity_id=decision.plan.opportunity_id,
                status="duplicate_profit_take_claim",
                message="Post-fill profit-take was already claimed.",
                details=decision.details,
                claim_key=claim_key,
            )
            continue
        try:
            live_result = await live_trader.execute(decision.plan)
        except Exception as exc:
            repository.update_execution_claim(claim_key=claim_key, status="failed", message=str(exc))
            repository.save_execution_event(
                source=source,
                mode="live",
                opportunity_id=decision.plan.opportunity_id,
                status="profit_take_failed",
                message=str(exc),
                details=decision.details,
                claim_key=claim_key,
            )
            continue

        repository.update_execution_claim(
            claim_key=claim_key,
            status=live_result.status,
            message=live_result.message,
        )
        repository.save_live_execution(live_result)
        repository.save_execution_event(
            source=source,
            mode="live",
            opportunity_id=decision.plan.opportunity_id,
            status="profit_take_submitted",
            message=live_result.message,
            details={
                **decision.details,
                "profit_take_fill_status": live_result.status,
                "profit_take_order_id": live_result.leg_results[0].order_id if live_result.leg_results else None,
                "legs": [leg.model_dump() for leg in live_result.leg_results],
            },
            claim_key=claim_key,
        )
        executed += 1
    return executed
