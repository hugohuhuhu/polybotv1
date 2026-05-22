from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan, OrderBookSnapshot
from app.models.runtime import TradingControls
from app.storage.repositories import ScannerRepository
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter
from app.strategy.risk_manager import RiskManager
from app.utils.time_utils import parse_datetime


@dataclass(slots=True)
class HedgeDecision:
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


def _hedge_target_price(settings: Settings, entry_price: float) -> float:
    if not settings.near_close_hedge_dynamic_pricing_enabled:
        return float(settings.near_close_hedge_default_price)
    if entry_price <= settings.near_close_hedge_low_entry_threshold:
        return float(settings.near_close_hedge_low_entry_price)
    if entry_price <= settings.near_close_hedge_mid_entry_threshold:
        return float(settings.near_close_hedge_mid_entry_price)
    return float(settings.near_close_hedge_default_price)


def build_post_fill_hedge_decision(
    *,
    entry: dict[str, Any],
    book: OrderBookSnapshot | None,
    settings: Settings,
    controls: TradingControls,
    repository: ScannerRepository,
) -> HedgeDecision:
    entry_order_id = str(entry.get("order_id") or "").strip()
    entry_token_id = str(entry.get("token_id") or "").strip()
    entry_outcome = str(entry.get("outcome_label") or "")
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    outcomes = entry.get("market_outcomes") or []
    opposite = next((item for item in outcomes if str(item.get("token_id") or "") != entry_token_id), None)
    entry_price = float(entry.get("target_price") or 0.0)
    entry_size = float(entry.get("requested_size") or 0.0)
    target_price = min(_hedge_target_price(settings, entry_price), settings.near_close_hedge_max_best_ask)
    locked_profit = 1.0 - entry_price - target_price
    minutes_to_close_at_hedge = _minutes_until(entry.get("end_date"))
    time_to_close_at_entry = response.get("minutes_to_resolution")

    details: dict[str, Any] = {
        "strategy_variant": "near_close_post_fill_hedge",
        "hedge_role": "post_fill_profit_lock",
        "hedge_for_order_id": entry_order_id,
        "entry_trade_id": entry.get("id"),
        "entry_market_id": entry.get("entry_market_id"),
        "entry_market_slug": entry.get("market_slug"),
        "entry_outcome": entry_outcome,
        "entry_token_id": entry_token_id,
        "entry_price": entry_price,
        "entry_fill_time": entry.get("created_at"),
        "hedge_target_price": target_price,
        "locked_profit_if_filled": locked_profit,
        "time_to_close_at_entry": time_to_close_at_entry,
        "time_to_close_at_hedge": minutes_to_close_at_hedge,
    }

    if not entry_order_id or entry_price <= 0 or entry_size <= 0:
        return HedgeDecision(False, False, "invalid_entry", None, details)
    if repository.hedge_order_exists_for_entry(entry_order_id):
        return HedgeDecision(False, False, "duplicate_hedge", None, details)
    if not opposite:
        return HedgeDecision(False, False, "missing_opposite_outcome", None, details)

    hedge_token_id = str(opposite.get("token_id") or "")
    hedge_outcome = str(opposite.get("outcome_label") or "Opposite")
    details.update({"hedge_token_id": hedge_token_id, "hedge_outcome": hedge_outcome})

    if bool(entry.get("closed")) or entry.get("active") is False or entry.get("active") == 0:
        return HedgeDecision(False, False, "market_closed", None, details)
    if minutes_to_close_at_hedge is None:
        return HedgeDecision(False, False, "missing_market_close_time", None, details)
    if minutes_to_close_at_hedge < settings.near_close_hedge_min_minutes_to_end:
        return HedgeDecision(False, False, "too_close_to_resolution", None, details)
    if locked_profit < settings.near_close_hedge_min_locked_profit:
        return HedgeDecision(False, False, "locked_profit_below_minimum", None, details)
    if settings.risk_kill_switch or controls.kill_switch_enabled:
        return HedgeDecision(False, False, "kill_switch_enabled", None, details)
    if book is None:
        return HedgeDecision(False, False, "missing_opposite_orderbook", None, details)
    if book.best_ask is None or book.best_bid is None or book.spread is None:
        return HedgeDecision(False, False, "incomplete_opposite_orderbook", None, details)
    if book.best_ask > settings.near_close_hedge_max_best_ask:
        details["best_ask"] = book.best_ask
        return HedgeDecision(False, False, "best_ask_above_hedge_max", None, details)
    if book.best_ask > target_price:
        details["best_ask"] = book.best_ask
        return HedgeDecision(False, False, "best_ask_above_target", None, details)
    if book.spread > settings.near_close_hedge_max_spread:
        details["spread"] = book.spread
        return HedgeDecision(False, False, "spread_above_max", None, details)
    ask_depth = book.depth_for_side("ask", target_price)
    if ask_depth < min(entry_size, settings.live_max_order_size, settings.near_close_hedge_min_depth):
        details["ask_depth"] = ask_depth
        return HedgeDecision(False, False, "insufficient_opposite_liquidity", None, details)

    size = min(entry_size, settings.live_max_order_size)
    plan = ExecutionPlan(
        opportunity_id=f"hedge:{entry_order_id}",
        summary=f"Post-fill hedge for {entry.get('market_slug')} {entry_outcome}",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id=hedge_token_id,
                market_slug=str(entry.get("market_slug") or ""),
                outcome_label=hedge_outcome,
                target_price=target_price,
                size=size,
                order_type=settings.near_close_hedge_order_type,
                post_only=False,
                metadata=details,
            )
        ],
        max_slippage_bps=0.0,
        cancel_conditions=[
            "Entry fill must already be confirmed.",
            "Opposite ask must remain at or below the configured hedge target.",
            "Kill switch and daily risk limits must remain clear.",
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
        return HedgeDecision(False, False, "risk_blocked", None, details)

    shadow_only = (
        not settings.near_close_post_fill_hedge_enabled
        or not settings.enable_live_trading
        or not controls.armed
    )
    if shadow_only:
        return HedgeDecision(False, settings.near_close_post_fill_hedge_shadow_enabled, "shadow_mode", plan, details)
    return HedgeDecision(True, False, "ready", plan, details)


async def execute_post_fill_hedges(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
    controls: TradingControls,
    watch_books: dict[str, OrderBookSnapshot],
    source: str = "watch",
) -> int:
    executed = 0
    for entry in repository.near_close_filled_entries_without_hedge(limit=25):
        opposite_token_id = ""
        entry_token_id = str(entry.get("token_id") or "")
        for outcome in entry.get("market_outcomes") or []:
            token_id = str(outcome.get("token_id") or "")
            if token_id and token_id != entry_token_id:
                opposite_token_id = token_id
                break
        decision = build_post_fill_hedge_decision(
            entry=entry,
            book=watch_books.get(opposite_token_id),
            settings=settings,
            controls=controls,
            repository=repository,
        )
        if decision.shadow_only:
            repository.save_execution_event(
                source=source,
                mode="shadow",
                opportunity_id=decision.plan.opportunity_id if decision.plan else None,
                status="hedge_shadow",
                message="Post-fill hedge decision simulated; no live order placed.",
                details=decision.details,
            )
            continue
        if not decision.should_place or decision.plan is None:
            repository.save_execution_event(
                source=source,
                mode="live",
                opportunity_id=decision.plan.opportunity_id if decision.plan else None,
                status="hedge_skipped",
                message=decision.reason,
                details=decision.details,
            )
            continue

        claim_key = f"hedge:{decision.details.get('hedge_for_order_id')}"
        claimed = repository.claim_execution(
            claim_key=claim_key,
            opportunity_id=decision.plan.opportunity_id,
            source=source,
            mode="live",
            message="Post-fill hedge claimed by watch loop.",
        )
        if not claimed:
            repository.save_execution_event(
                source=source,
                mode="live",
                opportunity_id=decision.plan.opportunity_id,
                status="duplicate_hedge_claim",
                message="Post-fill hedge was already claimed.",
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
                status="hedge_failed",
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
            status="hedge_submitted",
            message=live_result.message,
            details={
                **decision.details,
                "hedge_fill_status": live_result.status,
                "hedge_order_id": live_result.leg_results[0].order_id if live_result.leg_results else None,
                "legs": [leg.model_dump() for leg in live_result.leg_results],
            },
            claim_key=claim_key,
        )
        executed += 1
    return executed
