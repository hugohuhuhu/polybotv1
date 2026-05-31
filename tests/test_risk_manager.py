from __future__ import annotations

import time
from datetime import datetime, timezone

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan, LiveExecutionLegResult, LiveExecutionResult
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.risk_manager import RiskManager


def make_plan(*, size: float = 50.0, price: float = 0.49) -> ExecutionPlan:
    return ExecutionPlan(
        opportunity_id="risk-opp",
        summary="Risk test plan",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id="yes",
                market_slug="risk-market",
                outcome_label="Yes",
                target_price=price,
                size=size,
            ),
            ExecutionLeg(
                action="BUY",
                token_id="no",
                market_slug="risk-market",
                outcome_label="No",
                target_price=price,
                size=size,
            ),
        ],
        max_slippage_bps=10.0,
        cancel_conditions=[],
        requires_manual_approval=True,
        live_trading_allowed=True,
    )


def make_near_close_plan(
    *,
    size: float = 5.0,
    price: float = 0.97,
    minutes_to_resolution: float | None = None,
    time_to_resolution_sec: float | None = None,
    variant: str | None = None,
) -> ExecutionPlan:
    metadata = {"strategy_variant": "near_close_maker"}
    if time_to_resolution_sec is not None:
        metadata["time_to_resolution_sec"] = time_to_resolution_sec
    if minutes_to_resolution is not None:
        metadata["minutes_to_resolution"] = minutes_to_resolution
    if variant is not None:
        metadata["near_close_variant"] = variant
    return ExecutionPlan(
        opportunity_id="near-close",
        summary="near close",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id="yes",
                market_slug="risk-market",
                outcome_label="Yes",
                target_price=price,
                size=size,
                order_type="GTD",
                post_only=True,
            )
        ],
        max_slippage_bps=0.0,
        cancel_conditions=[],
        live_trading_allowed=True,
        strategy_type="late_resolution",
        metadata=metadata,
    )


def test_risk_manager_blocks_plan_over_max_notional(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk.db"))
    manager = RiskManager(Settings(MAX_NOTIONAL_PER_PLAN=40.0))

    decision = manager.assess(make_plan(size=50.0, price=0.49), repository, mode="paper")

    assert decision.allowed is False
    assert "MAX_NOTIONAL_PER_PLAN" in decision.reason


def test_risk_manager_blocks_live_after_daily_budget(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-live.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="existing-live",
            status="submitted",
            message="ok",
            order_type="FOK",
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="risk-market",
                    outcome_label="Yes",
                    target_price=0.50,
                    requested_size=100.0,
                    order_id="live-1",
                    status="submitted",
                    response={"ok": True},
                )
            ],
        )
    )
    manager = RiskManager(Settings(MAX_DAILY_LIVE_NOTIONAL=120.0, MAX_NOTIONAL_PER_PLAN=1000.0))

    decision = manager.assess(make_plan(size=80.0, price=0.45), repository, mode="live")

    assert decision.allowed is False
    assert "MAX_DAILY_LIVE_NOTIONAL" in decision.reason


def test_risk_manager_allows_live_when_daily_notional_cap_disabled(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-live-no-notional-cap.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="existing-live",
            status="submitted",
            message="ok",
            order_type="FOK",
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="risk-market",
                    outcome_label="Yes",
                    target_price=0.50,
                    requested_size=100.0,
                    order_id="live-1",
                    status="submitted",
                    response={"ok": True},
                )
            ],
        )
    )
    manager = RiskManager(Settings(MAX_DAILY_LIVE_NOTIONAL=0.0, MAX_NOTIONAL_PER_PLAN=1000.0))

    decision = manager.assess(make_plan(size=80.0, price=0.45), repository, mode="live")

    assert decision.allowed is True
    assert decision.projected_daily_notional == 122.0


def test_risk_manager_does_not_block_live_on_daily_order_count(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-live-orders.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="existing-live",
            status="submitted",
            message="ok",
            order_type="FOK",
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="risk-market",
                    outcome_label="Yes",
                    target_price=0.50,
                    requested_size=1.0,
                    order_id="live-1",
                    status="submitted",
                    response={"ok": True},
                )
            ],
        )
    )
    manager = RiskManager(
        Settings(MAX_DAILY_LIVE_ORDERS=0, MAX_DAILY_LIVE_NOTIONAL=1000.0, MAX_NOTIONAL_PER_PLAN=1000.0)
    )

    decision = manager.assess(make_plan(size=1.0, price=0.45), repository, mode="live")

    assert decision.allowed is True


def test_risk_manager_blocks_near_close_live_until_paper_gate(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close.db"))
    plan = make_near_close_plan(size=1.0, price=0.97)
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=100,
            MAX_NOTIONAL_PER_PLAN=10,
        )
    )

    decision = manager.assess(plan, repository, mode="live")

    assert decision.allowed is False
    assert "paper signals" in decision.reason


def test_risk_manager_blocks_near_close_live_before_two_minute_window(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-time.db"))
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=0,
            NEAR_CLOSE_ENTRY_MAX_SECONDS=120,
            MAX_NOTIONAL_PER_PLAN=10,
        )
    )

    decision = manager.assess(
        make_near_close_plan(size=1.0, price=0.97, time_to_resolution_sec=121),
        repository,
        mode="live",
    )

    assert decision.allowed is False
    assert "seconds to resolution" in decision.reason


def test_risk_manager_allows_near_close_live_inside_final_30_seconds(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-final-30.db"))
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=0,
            NEAR_CLOSE_ENTRY_MAX_SECONDS=120,
            NEAR_CLOSE_ENTRY_MIN_SECONDS=0,
            NEAR_CLOSE_FINAL_SECONDS_ALLOW_ENTRY=True,
            MAX_NOTIONAL_PER_PLAN=10,
        )
    )

    decision = manager.assess(
        make_near_close_plan(size=1.0, price=0.97, time_to_resolution_sec=15),
        repository,
        mode="live",
    )

    assert decision.allowed is True


def test_risk_manager_blocks_final_30_seconds_when_disabled(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-final-30-disabled.db"))
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=0,
            NEAR_CLOSE_ENTRY_MAX_SECONDS=120,
            NEAR_CLOSE_ENTRY_MIN_SECONDS=0,
            NEAR_CLOSE_FINAL_SECONDS_ALLOW_ENTRY=False,
            MAX_NOTIONAL_PER_PLAN=10,
        )
    )

    decision = manager.assess(
        make_near_close_plan(size=1.0, price=0.97, time_to_resolution_sec=15),
        repository,
        mode="live",
    )

    assert decision.allowed is False
    assert "final_seconds_entry_disabled" in decision.reason


def test_risk_manager_blocks_near_close_above_same_position_size(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-size.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="existing-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="risk-market",
                    outcome_label="Yes",
                    target_price=0.97,
                    requested_size=6.0,
                    order_id="near-close-open",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=0,
            NEAR_CLOSE_MAX_TOTAL_EXPOSURE=100.0,
            NEAR_CLOSE_MAX_POSITION_SIZE=10.0,
            NEAR_CLOSE_ORDER_SIZE=10.0,
            MAX_NOTIONAL_PER_PLAN=100.0,
        )
    )

    decision = manager.assess(make_near_close_plan(size=5.0, price=0.97), repository, mode="live")

    assert decision.allowed is False
    assert "same-position" in decision.reason


def test_risk_manager_counts_pending_near_close_size_per_position(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-pending.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="pending-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="risk-market",
                    outcome_label="Yes",
                    target_price=0.97,
                    requested_size=6.0,
                    order_id="near-close-pending",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_maker",
                        "expiration": int(time.time()) + 600,
                    },
                )
            ],
        )
    )
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=0,
            NEAR_CLOSE_MAX_TOTAL_EXPOSURE=100.0,
            NEAR_CLOSE_MAX_POSITION_SIZE=10.0,
            NEAR_CLOSE_ORDER_SIZE=10.0,
            MAX_NOTIONAL_PER_PLAN=100.0,
        )
    )

    decision = manager.assess(make_near_close_plan(size=5.0, price=0.97), repository, mode="live")

    assert decision.allowed is False
    assert "same-position" in decision.reason


def test_risk_manager_uses_weekend_order_size_limit_for_crypto_updown(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-weekend-order.db"))
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
            NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
            NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER=0.5,
            NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE=5.0,
            MAX_NOTIONAL_PER_PLAN=100.0,
        )
    )

    decision = manager.assess(
        make_near_close_plan(size=5.0, price=0.95, variant="crypto_updown"),
        repository,
        mode="paper",
    )

    assert decision.allowed is False
    assert "2.50 pUSD" in decision.reason


def test_risk_manager_uses_weekend_total_exposure_limit(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "risk-near-close-weekend-exposure.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="existing-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="risk-market",
                    outcome_label="Yes",
                    target_price=0.97,
                    requested_size=6.0,
                    order_id="near-close-open",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )
    manager = RiskManager(
        Settings(
            NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
            NEAR_CLOSE_MIN_PAPER_SIGNALS_FOR_LIVE=0,
            NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
            NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
            NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER=1.0,
            NEAR_CLOSE_WEEKEND_EXPOSURE_MULTIPLIER=0.5,
            NEAR_CLOSE_MAX_TOTAL_EXPOSURE=15.0,
            NEAR_CLOSE_MAX_POSITION_SIZE=20.0,
            NEAR_CLOSE_ORDER_SIZE=10.0,
            MAX_NOTIONAL_PER_PLAN=100.0,
        )
    )

    decision = manager.assess(make_near_close_plan(size=2.0, price=0.97), repository, mode="live")

    assert decision.allowed is False
    assert "total exposure" in decision.reason
