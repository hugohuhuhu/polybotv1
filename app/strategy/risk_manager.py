from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.models.core import ExecutionPlan
from app.storage.repositories import ScannerRepository


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    estimated_notional: float
    projected_daily_notional: float
    projected_daily_orders: int


class RiskManager:
    """Apply lightweight daily and per-plan risk limits before execution."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def estimate_plan_notional(plan: ExecutionPlan) -> float:
        return sum(
            abs(float(leg.target_price) * float(leg.size))
            for leg in plan.legs
            if leg.action.upper() in {"BUY", "SELL"}
        )

    def assess(self, plan: ExecutionPlan, repository: ScannerRepository, *, mode: str) -> RiskDecision:
        estimated_notional = self.estimate_plan_notional(plan)
        leg_count = sum(1 for leg in plan.legs if leg.action.upper() in {"BUY", "SELL"})
        near_close = plan.strategy_type == "late_resolution" and plan.metadata.get("strategy_variant") == "near_close_maker"

        if self.settings.risk_kill_switch:
            return RiskDecision(
                allowed=False,
                reason="Risk kill switch is enabled.",
                estimated_notional=estimated_notional,
                projected_daily_notional=estimated_notional,
                projected_daily_orders=leg_count,
            )

        if estimated_notional <= 0:
            return RiskDecision(
                allowed=False,
                reason="Execution plan has no positive notional.",
                estimated_notional=estimated_notional,
                projected_daily_notional=estimated_notional,
                projected_daily_orders=leg_count,
            )

        if estimated_notional > self.settings.max_notional_per_plan:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Plan notional {estimated_notional:.2f} exceeds MAX_NOTIONAL_PER_PLAN "
                    f"{self.settings.max_notional_per_plan:.2f}."
                ),
                estimated_notional=estimated_notional,
                projected_daily_notional=estimated_notional,
                projected_daily_orders=leg_count,
            )

        if near_close:
            near_close_decision = self._assess_near_close(
                plan,
                repository,
                mode=mode,
                estimated_notional=estimated_notional,
                leg_count=leg_count,
            )
            if near_close_decision is not None:
                return near_close_decision

        if mode == "paper":
            summary = repository.paper_risk_summary()
            projected_notional = float(summary["paper_notional_today"]) + estimated_notional
            projected_orders = int(summary["paper_trades_today"]) + 1
            if projected_notional > self.settings.max_daily_paper_notional:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Projected paper notional {projected_notional:.2f} exceeds "
                        f"MAX_DAILY_PAPER_NOTIONAL {self.settings.max_daily_paper_notional:.2f}."
                    ),
                    estimated_notional=estimated_notional,
                    projected_daily_notional=projected_notional,
                    projected_daily_orders=projected_orders,
                )
            if projected_orders > self.settings.max_daily_paper_trades:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Projected paper trade count {projected_orders} exceeds "
                        f"MAX_DAILY_PAPER_TRADES {self.settings.max_daily_paper_trades}."
                    ),
                    estimated_notional=estimated_notional,
                    projected_daily_notional=projected_notional,
                    projected_daily_orders=projected_orders,
                )
            return RiskDecision(
                allowed=True,
                reason="Paper execution is within configured risk limits.",
                estimated_notional=estimated_notional,
                projected_daily_notional=projected_notional,
                projected_daily_orders=projected_orders,
            )

        summary = repository.live_risk_summary()
        projected_notional = float(summary["live_notional_today"]) + estimated_notional
        projected_orders = int(summary["live_orders_today"]) + leg_count
        if projected_notional > self.settings.max_daily_live_notional:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Projected live notional {projected_notional:.2f} exceeds "
                    f"MAX_DAILY_LIVE_NOTIONAL {self.settings.max_daily_live_notional:.2f}."
                ),
                estimated_notional=estimated_notional,
                projected_daily_notional=projected_notional,
                projected_daily_orders=projected_orders,
            )
        if projected_orders > self.settings.max_daily_live_orders:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Projected live order count {projected_orders} exceeds "
                    f"MAX_DAILY_LIVE_ORDERS {self.settings.max_daily_live_orders}."
                ),
                estimated_notional=estimated_notional,
                projected_daily_notional=projected_notional,
                projected_daily_orders=projected_orders,
            )
        return RiskDecision(
            allowed=True,
            reason="Live execution is within configured risk limits.",
            estimated_notional=estimated_notional,
            projected_daily_notional=projected_notional,
            projected_daily_orders=projected_orders,
        )

    def _assess_near_close(
        self,
        plan: ExecutionPlan,
        repository: ScannerRepository,
        *,
        mode: str,
        estimated_notional: float,
        leg_count: int,
    ) -> RiskDecision | None:
        if estimated_notional > self.settings.near_close_order_size:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Near-close order notional {estimated_notional:.2f} exceeds "
                    f"{self.settings.near_close_order_size:.2f} pUSD."
                ),
                estimated_notional=estimated_notional,
                projected_daily_notional=estimated_notional,
                projected_daily_orders=leg_count,
            )
        if mode != "live":
            return None
        if not self.settings.near_close_maker_live_enabled:
            return RiskDecision(
                allowed=False,
                reason="Near-close maker live trading is disabled; paper observation only.",
                estimated_notional=estimated_notional,
                projected_daily_notional=estimated_notional,
                projected_daily_orders=leg_count,
            )
        signal_count = repository.near_close_signal_count()
        if signal_count < self.settings.near_close_min_paper_signals_for_live:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Near-close maker requires {self.settings.near_close_min_paper_signals_for_live} "
                    f"paper signals before live; currently {signal_count}."
                ),
                estimated_notional=estimated_notional,
                projected_daily_notional=estimated_notional,
                projected_daily_orders=leg_count,
            )
        exposure = repository.near_close_live_exposure()
        market_slug = plan.legs[0].market_slug if plan.legs else ""
        market_exposure = float(exposure["by_market"].get(market_slug, 0.0)) + estimated_notional
        total_exposure = float(exposure["total"]) + estimated_notional
        if market_exposure > self.settings.near_close_max_market_exposure:
            return RiskDecision(
                allowed=False,
                reason="Near-close maker same-market exposure limit reached.",
                estimated_notional=estimated_notional,
                projected_daily_notional=market_exposure,
                projected_daily_orders=leg_count,
            )
        if total_exposure > self.settings.near_close_max_total_exposure:
            return RiskDecision(
                allowed=False,
                reason="Near-close maker total exposure limit reached.",
                estimated_notional=estimated_notional,
                projected_daily_notional=total_exposure,
                projected_daily_orders=leg_count,
            )
        return None
