from __future__ import annotations

import asyncio
import json

from app.config import Settings
from app.main import _execute_near_close_taker_exits, _sync_live_fills_to_db
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
    assert manager.taker_exit_price(book=make_book(bid=0.51, ask=0.54)) == 0.5


def test_near_close_manager_requires_entry_relative_taker_exit() -> None:
    manager = NearCloseOrderManager(
        Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.025)
    )

    assert manager.taker_exit_required(book=make_book(bid=0.84, ask=0.86), entry_price=0.87) is True
    assert manager.taker_exit_required(book=make_book(bid=0.85, ask=0.87), entry_price=0.87) is False
    assert manager.taker_exit_price(book=make_book(bid=0.84, ask=0.86)) == 0.83


def test_near_close_taker_exit_uses_fak_to_take_available_liquidity(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.order_type = None
            self.target_price = None

        async def execute(self, plan):
            self.order_type = plan.legs[0].order_type
            self.target_price = plan.legs[0].target_price
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
    assert trader.target_price == 0.5


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


def test_near_close_taker_exit_assumes_submitted_order_may_be_filled(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.plan = None
            self.cancelled = []

        async def cancel_orders(self, order_ids):
            self.cancelled.extend(order_ids)
            return {"not_canceled": {order_ids[0]: "matched orders can't be canceled"}}

        async def execute(self, plan):
            self.plan = plan
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "assumed-stop-exit.db"))
    response = {"strategy_variant": "near_close_maker"}
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "submitted-stop",
                1,
                "BUY",
                "token-doge",
                "doge-updown-5m-test",
                "Up",
                0.89,
                5.0,
                "0xsubmitted",
                "SUBMITTED",
                json.dumps(response),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.025),
            watch_books={"token-doge": make_book(bid=0.84, ask=0.86)},
        )
    )

    assert trader.cancelled == ["0xsubmitted"]
    assert trader.plan is not None
    assert trader.plan.legs[0].action == "SELL"
    assert trader.plan.legs[0].order_type == "FAK"
    assert trader.plan.legs[0].metadata["assumed_fill_stop_exit"] is True
    assert trader.plan.legs[0].metadata["source_order_id"] == "0xsubmitted"


def test_near_close_taker_exit_uses_recent_stored_orderbook_when_watch_book_missing(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.plan = None
            self.cancelled = []

        async def cancel_orders(self, order_ids):
            self.cancelled.extend(order_ids)
            return {"not_canceled": {order_ids[0]: "matched orders can't be canceled"}}

        async def execute(self, plan):
            self.plan = plan
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "stored-book-stop-exit.db"))
    response = {"strategy_variant": "near_close_maker"}
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "submitted-stored-book-stop",
                1,
                "BUY",
                "token-btc",
                "btc-updown-15m-test",
                "Up",
                0.89,
                5.0,
                "0xsubmitted",
                "SUBMITTED",
                json.dumps(response),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id="token-btc",
                bids=[BookLevel(price=0.84, size=100)],
                asks=[BookLevel(price=0.86, size=100)],
            )
        ]
    )
    trader = FakeTrader()

    asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(
                NEAR_CLOSE_TAKER_EXIT_PRICE=0.52,
                NEAR_CLOSE_HARD_STOP_OFFSET=0.025,
                NEAR_CLOSE_STOP_EXIT_STALE_ORDERBOOK_MAX_AGE_SEC=60,
            ),
            watch_books={},
        )
    )

    assert trader.cancelled == ["0xsubmitted"]
    assert trader.plan is not None
    assert trader.plan.legs[0].target_price == 0.83
    assert trader.plan.legs[0].metadata["stop_orderbook_source"] == "stored_orderbook"


def test_near_close_taker_exit_cancels_unfilled_submitted_order_before_assumed_exit(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.plan = None

        async def cancel_orders(self, order_ids):
            return {"canceled": order_ids}

        async def execute(self, plan):
            self.plan = plan
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "assumed-stop-cancel.db"))
    response = {"strategy_variant": "near_close_maker"}
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "submitted-cancel",
                1,
                "BUY",
                "token-doge",
                "doge-updown-5m-test",
                "Up",
                0.89,
                5.0,
                "0xsubmitted",
                "SUBMITTED",
                json.dumps(response),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    exits = asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.025),
            watch_books={"token-doge": make_book(bid=0.84, ask=0.86)},
        )
    )

    assert trader.plan is None
    assert exits[0]["status"] == "entry_cancelled_before_assumed_stop_exit"
    row = repository.connection.fetchone("SELECT status FROM live_trades WHERE order_id = ?", ("0xsubmitted",))
    assert row["status"].upper() == "QUALIFICATION_CANCELLED"


def test_watch_fill_sync_updates_submitted_maker_order_before_stop_exit(tmp_path, monkeypatch) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.settings = None

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "type": "TRADE",
                    "proxyWallet": "0xabc",
                    "asset": "token-doge",
                    "side": "BUY",
                    "size": "5",
                    "price": "0.89",
                    "timestamp": "1779154661",
                    "transactionHash": "0xfill",
                    "outcome": "Up",
                }
            ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    repository = ScannerRepository(connect_db(tmp_path / "fill-sync.db"))
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    response = {"strategy_variant": "near_close_maker"}
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "submitted-fill-sync",
                1,
                "BUY",
                "token-doge",
                "doge-updown-5m-test",
                "Up",
                0.89,
                5.0,
                "0xsubmitted",
                "SUBMITTED",
                json.dumps(response),
                "2026-05-14T00:58:16+00:00",
            ),
        )

    inserted = asyncio.run(
        _sync_live_fills_to_db(
            repository=repository,
            live_trader=FakeTrader(),
            settings=Settings(POLYMARKET_PRIVATE_KEY="0x1", POLYMARKET_FUNDER_ADDRESS="0xabc"),
        )
    )

    row = repository.connection.fetchone(
        "SELECT status, target_price, requested_size FROM live_trades WHERE order_id = ?",
        ("0xsubmitted",),
    )
    assert inserted == 1
    assert row["status"] == "CONFIRMED"
    assert row["target_price"] == 0.89
    assert row["requested_size"] == 5.0


def test_near_close_taker_exit_uses_entry_relative_stop_before_deep_crash(tmp_path) -> None:
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

    repository = ScannerRepository(connect_db(tmp_path / "relative-stop-exit.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "relative-stop",
                1,
                "BUY",
                "token-doge",
                "doge-updown-5m-test",
                "Down",
                0.87,
                5.0,
                "0xrelative",
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
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.025),
            watch_books={"token-doge": make_book(bid=0.84, ask=0.86)},
        )
    )

    assert trader.plan is not None
    assert trader.plan.legs[0].target_price == 0.83
    assert trader.plan.legs[0].metadata["stop_reference_price"] == 0.84
    assert round(trader.plan.legs[0].metadata["stop_entry_price"], 6) == 0.87


def test_near_close_taker_exit_uses_second_chance_floor_when_first_fak_has_no_match(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.plans = []

        async def execute(self, plan):
            self.plans.append(plan)
            if len(self.plans) == 1:
                return LiveExecutionResult(
                    opportunity_id=plan.opportunity_id,
                    status="failed",
                    message="no orders found to match with FAK order",
                    order_type=plan.legs[0].order_type,
                    leg_results=[],
                )
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "second-chance-stop-exit.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "second-chance-stop",
                1,
                "BUY",
                "token-doge",
                "doge-updown-5m-test",
                "Down",
                0.91,
                5.0,
                "0xopen",
                "CONFIRMED",
                "{}",
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    exits = asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(
                NEAR_CLOSE_TAKER_EXIT_PRICE=0.52,
                NEAR_CLOSE_HARD_STOP_OFFSET=0.025,
                NEAR_CLOSE_EMERGENCY_SLIPPAGE=0.03,
                NEAR_CLOSE_SECOND_CHANCE_EXIT_PRICE=0.01,
            ),
            watch_books={"token-doge": make_book(bid=0.87, ask=0.9)},
        )
    )

    assert len(trader.plans) == 2
    assert trader.plans[0].legs[0].target_price == 0.84
    assert trader.plans[1].legs[0].target_price == 0.01
    assert trader.plans[1].legs[0].metadata["stop_exit_stage"] == "second_chance"
    assert exits[0]["second_chance_attempted"] is True
    assert exits[0]["status"] == "submitted"


def test_near_close_taker_exit_cancels_active_profit_take_before_stop(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.cancelled = []
            self.plan = None

        async def cancel_orders(self, order_ids):
            self.cancelled.extend(order_ids)
            return {"canceled": order_ids}

        async def execute(self, plan):
            self.plan = plan
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[],
            )

    repository = ScannerRepository(connect_db(tmp_path / "stop-cancels-profit.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "entry-before-profit",
                1,
                "BUY",
                "token-doge",
                "doge-updown-5m-test",
                "Down",
                0.91,
                5.0,
                "0xentry",
                "CONFIRMED",
                json.dumps({"strategy_variant": "near_close_maker"}),
                "2026-05-14T00:58:16+00:00",
            ),
        )
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "profit-take:0xentry",
                1,
                "SELL",
                "token-doge",
                "doge-updown-5m-test",
                "Down",
                0.97,
                5.0,
                "0xprofit",
                "SUBMITTED",
                json.dumps({"strategy_variant": "near_close_profit_take", "profit_take_for_order_id": "0xentry"}),
                "2026-05-14T00:58:20+00:00",
            ),
        )
    trader = FakeTrader()

    asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(
                NEAR_CLOSE_TAKER_EXIT_PRICE=0.52,
                NEAR_CLOSE_HARD_STOP_OFFSET=0.025,
                NEAR_CLOSE_EMERGENCY_SLIPPAGE=0.03,
            ),
            watch_books={"token-doge": make_book(bid=0.87, ask=0.9)},
        )
    )

    row = repository.connection.fetchone("SELECT status FROM live_trades WHERE order_id = ?", ("0xprofit",))
    assert trader.cancelled == ["0xprofit"]
    assert trader.plan is not None
    assert trader.plan.legs[0].action == "SELL"
    assert row["status"] == "stop_exit_cancelled_profit_take"
