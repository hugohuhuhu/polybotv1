from __future__ import annotations

import asyncio
import json

from app.config import Settings
from app.main import _execute_near_close_taker_exits
from app.models.core import BookLevel, LiveExecutionResult, OrderBookSnapshot
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.near_close_order_manager import NearCloseOrderManager


def make_book(*, bid: float, ask: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="yes",
        bids=[BookLevel(price=bid, size=100)],
        asks=[BookLevel(price=ask, size=100)],
    )


def test_near_close_manager_cancels_when_order_would_cross_or_is_too_late() -> None:
    manager = NearCloseOrderManager(Settings())
    reasons = manager.entry_cancel_reasons(
        book=make_book(bid=0.98, ask=0.985),
        minutes_to_end=2.5,
        entry_price=0.986,
    )

    assert "too_close_to_end" in reasons
    assert "would_cross_post_only" in reasons


def test_near_close_manager_hard_stop_and_worst_price() -> None:
    manager = NearCloseOrderManager(Settings())
    book = make_book(bid=0.94, ask=0.96)

    assert manager.hard_stop_required(book=book, entry_price=0.97) is True
    assert round(manager.emergency_worst_price(book=book, entry_price=0.97) or 0.0, 6) == 0.93


def test_near_close_manager_uses_crypto_cancel_thresholds() -> None:
    manager = NearCloseOrderManager(Settings())
    reasons = manager.entry_cancel_reasons(
        book=make_book(bid=0.981, ask=0.986),
        minutes_to_end=4.5,
        entry_price=0.982,
        variant="crypto",
        crypto_strike_distance=0.01,
    )

    assert "too_close_to_end" in reasons
    assert "crypto_strike_too_close" in reasons


def test_near_close_manager_requires_taker_exit_at_or_below_threshold() -> None:
    manager = NearCloseOrderManager(Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52))

    assert manager.taker_exit_required(book=make_book(bid=0.52, ask=0.55)) is True
    assert manager.taker_exit_required(book=make_book(bid=0.53, ask=0.56)) is False
    assert manager.taker_exit_price(book=make_book(bid=0.51, ask=0.54)) == 0.51


def test_near_close_taker_exit_uses_fak_to_take_available_liquidity(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.order_type = None

        async def execute(self, plan):
            self.order_type = plan.legs[0].order_type
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "stop-exit.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "open-stop",
                1,
                "BUY",
                "token-up",
                "sol-updown-15m-test",
                "Up",
                0.83,
                5.0,
                "0xopen",
                "CONFIRMED",
                "{}",
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52),
            watch_books={"token-up": make_book(bid=0.51, ask=0.54)},
        )
    )

    assert trader.order_type == "FAK"


def test_near_close_taker_exit_includes_matched_cancel_unconfirmed_order(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.plan = None

        async def execute(self, plan):
            self.plan = plan
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "pending-stop-exit.db"))
    order_id = "0xmatched"
    response = {
        "strategy_variant": "near_close_maker",
        "expiration": 1999999999,
    }
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pending-stop",
                1,
                "BUY",
                "token-down",
                "sol-updown-5m-test",
                "Down",
                0.96,
                5.0,
                order_id,
                "SUBMITTED",
                json.dumps(response),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    repository.mark_live_orders_cancelled(
        [order_id],
        status="cancel_unconfirmed",
        cancel_response={"not_canceled": {order_id: "matched orders can't be canceled"}},
    )
    trader = FakeTrader()

    asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52),
            watch_books={"token-down": make_book(bid=0.49, ask=0.78)},
        )
    )

    assert trader.plan is not None
    assert trader.plan.legs[0].action == "SELL"
    assert trader.plan.legs[0].order_type == "FAK"
    assert trader.plan.legs[0].size == 5.0
