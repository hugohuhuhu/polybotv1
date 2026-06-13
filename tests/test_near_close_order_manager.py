from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.config import Settings
from app.main import _cancel_unqualified_near_close_orders, _sync_live_fills_to_db
from app.models.core import BookLevel, LiveExecutionLegResult, LiveExecutionResult, OrderBookSnapshot
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.near_close_order_manager import NearCloseOrderManager
from app.strategy.near_close_stop_exit import execute_near_close_taker_exits


_execute_near_close_taker_exits = execute_near_close_taker_exits


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

    assert "entry_before_window" in reasons
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

    assert "entry_before_window" in reasons
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


def test_cancel_unqualified_order_records_not_open_reason(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.cancelled = []

        async def cancel_orders(self, order_ids):
            self.cancelled.extend(order_ids)
            return {"canceled": order_ids}

    class FakeCycle:
        opportunities = []

        def __init__(self, token_id: str) -> None:
            self.books = {
                token_id: OrderBookSnapshot(
                    token_id=token_id,
                    bids=[BookLevel(price=0.9, size=100)],
                    asks=[BookLevel(price=0.96, size=100)],
                )
            }

    repository = ScannerRepository(connect_db(tmp_path / "qualification-cancel-reason.db"))
    token_id = "token-btc"
    start_epoch = int(datetime.now(timezone.utc).timestamp()) - 290
    market_slug = f"btc-updown-5m-{start_epoch}"
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cancel-reason",
                1,
                "BUY",
                token_id,
                market_slug,
                "Up",
                0.9,
                5.0,
                "0xcancelreason",
                "SUBMITTED",
                json.dumps({"strategy_variant": "near_close_maker", "near_close_variant": "crypto_updown"}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    trader = FakeTrader()
    asyncio.run(
        _cancel_unqualified_near_close_orders(
            cycle=FakeCycle(token_id),
            repository=repository,
            live_trader=trader,
            settings=Settings(
                NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
                NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
                NEAR_CLOSE_EXISTING_ORDER_HARD_CANCEL_SECONDS=12,
            ),
        )
    )

    row = repository.connection.fetchone(
        "SELECT status, response_json FROM live_trades WHERE order_id = ?",
        ("0xcancelreason",),
    )
    response = json.loads(row["response_json"])
    assert row["status"].upper() == "QUALIFICATION_CANCELLED"
    assert trader.cancelled == ["0xcancelreason"]
    assert response["not_open_reason"] == "existing_order_hard_cancel"
    assert "existing_order_hard_cancel" in response["not_open_reasons"]
    assert "entry_after_window" in response["not_open_reasons"]
    assert response["cancel_reason_context"]["trigger"] == "qualification_cancel"
    assert 0 <= response["cancel_reason_context"]["time_to_resolution_sec"] <= 12
    assert response["cancel_reason_context"]["existing_order_hard_cancel_seconds"] == 12
    assert response["cancel_reason_context"]["cancel_reason_checked_at"]

    order = repository.recent_live_orders(limit=1)[0]
    assert order["not_open_reason"] == "existing_order_hard_cancel"
    assert "existing_order_hard_cancel" in order["not_open_reasons"]
    assert "entry_after_window" in order["not_open_reasons"]


def test_cancel_unqualified_order_keeps_time_only_failure_until_hard_cutoff(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.cancelled = []

        async def cancel_orders(self, order_ids):
            self.cancelled.extend(order_ids)
            return {"canceled": order_ids}

    class FakeCycle:
        opportunities = []

        def __init__(self, token_id: str) -> None:
            self.books = {
                token_id: OrderBookSnapshot(
                    token_id=token_id,
                    bids=[BookLevel(price=0.88, size=100)],
                    asks=[BookLevel(price=0.89, size=100)],
                )
            }

    repository = ScannerRepository(connect_db(tmp_path / "qualification-hold-until-cutoff.db"))
    token_id = "token-sol"
    start_epoch = int(datetime.now(timezone.utc).timestamp()) - 280
    market_slug = f"sol-updown-5m-{start_epoch}"
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hold-until-cutoff",
                1,
                "BUY",
                token_id,
                market_slug,
                "Up",
                0.87,
                5.0,
                "0xholduntilcutoff",
                "SUBMITTED",
                json.dumps({"strategy_variant": "near_close_maker", "near_close_variant": "crypto_updown"}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    trader = FakeTrader()

    asyncio.run(
        _cancel_unqualified_near_close_orders(
            cycle=FakeCycle(token_id),
            repository=repository,
            live_trader=trader,
            settings=Settings(
                NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
                NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
                NEAR_CLOSE_EXISTING_ORDER_HARD_CANCEL_SECONDS=12,
            ),
        )
    )

    row = repository.connection.fetchone(
        "SELECT status FROM live_trades WHERE order_id = ?",
        ("0xholduntilcutoff",),
    )
    assert trader.cancelled == []
    assert row["status"].upper() == "SUBMITTED"


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


def test_near_close_taker_exit_sends_panic_fak_even_when_spread_is_wide(tmp_path) -> None:
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

    repository = ScannerRepository(connect_db(tmp_path / "stop-wide-spread.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wide-spread-stop",
                1,
                "BUY",
                "token-bnb",
                "bnb-updown-15m-test",
                "Down",
                0.89,
                5.0,
                "0xopen",
                "CONFIRMED",
                json.dumps({"strategy_variant": "near_close_maker"}),
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
                NEAR_CLOSE_STOP_EXIT_MAX_SPREAD=0.08,
                NEAR_CLOSE_CRYPTO_UPDOWN_STOP_REQUIRES_DIRECTION_BREAK=False,
            ),
            watch_books={"token-bnb": make_book(bid=0.69, ask=0.96)},
        )
    )

    assert trader.plan is not None
    assert trader.plan.legs[0].order_type == "FAK"
    assert trader.plan.legs[0].metadata["panic_exit_wide_spread"] is True
    assert trader.plan.legs[0].metadata["max_stop_exit_spread"] == 0.08
    assert exits[0]["status"] == "submitted"
    assert exits[0]["panic_exit_wide_spread"] is True
    assert exits[0]["observed_spread"] == 0.27


def test_near_close_taker_exit_skips_when_crypto_direction_still_valid(tmp_path, monkeypatch) -> None:
    class FakePriceClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get_prices(self, symbols):
            return {"BTCUSDT": 99.0}

        async def close(self) -> None:
            return None

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

    monkeypatch.setattr("app.strategy.near_close_stop_exit.CryptoPriceClient", FakePriceClient)
    repository = ScannerRepository(connect_db(tmp_path / "stop-direction-valid.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "direction-valid-stop",
                1,
                "BUY",
                "token-btc",
                "btc-updown-15m-test",
                "Down",
                0.92,
                5.0,
                "0xopen",
                "CONFIRMED",
                json.dumps(
                    {
                        "strategy_variant": "near_close_maker",
                        "crypto_start_price": 100.0,
                        "crypto_winning_outcome": "Down",
                    }
                ),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    exits = asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.2),
            watch_books={"token-btc": make_book(bid=0.71, ask=0.72)},
        )
    )

    assert trader.plan is None
    assert exits[0]["status"] == "stop_exit_skipped_crypto_direction_intact"
    assert exits[0]["crypto_stop_spot_price"] == 99.0
    assert exits[0]["crypto_direction_broken"] is False


def test_near_close_taker_exit_skips_down_proxy_tie_at_start(tmp_path, monkeypatch) -> None:
    class FakePriceClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get_prices(self, symbols):
            return {"SOLUSDT": 100.0}

        async def close(self) -> None:
            return None

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

    monkeypatch.setattr("app.strategy.near_close_stop_exit.CryptoPriceClient", FakePriceClient)
    repository = ScannerRepository(connect_db(tmp_path / "stop-direction-proxy-tie.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "direction-proxy-tie-stop",
                1,
                "BUY",
                "token-sol",
                "sol-updown-15m-test",
                "Down",
                0.94,
                5.0,
                "0xopen",
                "CONFIRMED",
                json.dumps(
                    {
                        "strategy_variant": "near_close_maker",
                        "crypto_start_price": 100.0,
                        "crypto_winning_outcome": "Down",
                    }
                ),
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
                NEAR_CLOSE_HARD_STOP_OFFSET=0.2,
                NEAR_CLOSE_CRYPTO_UPDOWN_STOP_HARD_OVERRIDE_ENABLED=False,
            ),
            watch_books={"token-sol": make_book(bid=0.37, ask=0.38)},
        )
    )

    assert trader.plan is None
    assert exits[0]["status"] == "stop_exit_skipped_crypto_direction_intact"
    assert exits[0]["crypto_stop_spot_price"] == 100.0
    assert exits[0]["crypto_direction_break_buffer"] == 0.00075
    assert exits[0]["crypto_direction_broken"] is False


def test_near_close_taker_exit_hard_override_bypasses_intact_crypto_direction(tmp_path, monkeypatch) -> None:
    class FakePriceClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get_prices(self, symbols):
            return {"BTCUSDT": 99.0}

        async def close(self) -> None:
            return None

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

    monkeypatch.setattr("app.strategy.near_close_stop_exit.CryptoPriceClient", FakePriceClient)
    repository = ScannerRepository(connect_db(tmp_path / "stop-direction-hard-override.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "direction-hard-override-stop",
                1,
                "BUY",
                "token-btc",
                "btc-updown-15m-test",
                "Down",
                0.94,
                5.0,
                "0xopen",
                "CONFIRMED",
                json.dumps(
                    {
                        "strategy_variant": "near_close_maker",
                        "crypto_start_price": 100.0,
                        "crypto_winning_outcome": "Down",
                    }
                ),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    exits = asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.2),
            watch_books={"token-btc": make_book(bid=0.37, ask=0.38)},
        )
    )

    assert trader.plan is not None
    assert exits[0]["status"] == "submitted"
    assert trader.plan.legs[0].order_type == "FAK"
    assert trader.plan.legs[0].metadata["crypto_direction_broken"] is False
    assert trader.plan.legs[0].metadata["crypto_direction_hard_override_active"] is True
    assert "best_bid_collapse" in trader.plan.legs[0].metadata["crypto_direction_hard_override_reasons"]


def test_near_close_taker_exit_allows_when_crypto_direction_breaks(tmp_path, monkeypatch) -> None:
    class FakePriceClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get_prices(self, symbols):
            return {"BTCUSDT": 100.5}

        async def close(self) -> None:
            return None

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

    monkeypatch.setattr("app.strategy.near_close_stop_exit.CryptoPriceClient", FakePriceClient)
    repository = ScannerRepository(connect_db(tmp_path / "stop-direction-broken.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "direction-broken-stop",
                1,
                "BUY",
                "token-btc",
                "btc-updown-15m-test",
                "Down",
                0.92,
                5.0,
                "0xopen",
                "CONFIRMED",
                json.dumps(
                    {
                        "strategy_variant": "near_close_maker",
                        "crypto_start_price": 100.0,
                        "crypto_winning_outcome": "Down",
                    }
                ),
                "2026-05-14T00:58:16+00:00",
            ),
        )
    trader = FakeTrader()

    asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=trader,
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.2),
            watch_books={"token-btc": make_book(bid=0.71, ask=0.72)},
        )
    )

    assert trader.plan is not None
    assert trader.plan.legs[0].order_type == "FAK"
    assert trader.plan.legs[0].metadata["crypto_direction_broken"] is True


def test_near_close_taker_exit_records_observed_bid_and_execution_telemetry(tmp_path) -> None:
    class FakeTrader:
        async def execute(self, plan):
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[
                    LiveExecutionLegResult(
                        leg_index=1,
                        action="SELL",
                        token_id=plan.legs[0].token_id,
                        market_slug=plan.legs[0].market_slug,
                        outcome_label=plan.legs[0].outcome_label,
                        target_price=plan.legs[0].target_price,
                        requested_size=plan.legs[0].size,
                        order_id="0xexit",
                        status="submitted",
                        response={"price": "0.50"},
                    )
                ],
            )

    repository = ScannerRepository(connect_db(tmp_path / "stop-exit-telemetry.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "open-telemetry",
                1,
                "BUY",
                "token-up",
                "btc-updown-15m-test",
                "Up",
                0.83,
                5.0,
                "0xopen",
                "CONFIRMED",
                "{}",
                "2026-05-14T00:58:16+00:00",
            ),
        )

    exits = asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=FakeTrader(),
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52),
            watch_books={"token-up": make_book(bid=0.51, ask=0.54)},
        )
    )
    event = repository.connection.fetchone(
        "SELECT details_json FROM execution_audit_log WHERE opportunity_id = ? ORDER BY id DESC LIMIT 1",
        ("stop-exit:btc-updown-15m-test:token-up",),
    )
    details = json.loads(event["details_json"])

    assert exits[0]["observed_best_bid"] == 0.51
    assert details["observed_best_bid"] == 0.51
    assert details["execution"][0]["reported_execution_price"] == 0.5
    assert details["second_chance_attempted"] is False


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
        cancel_reason_by_order={
            order_id: {
                "not_open_reason": "would_cross_post_only",
                "not_open_reasons": ["would_cross_post_only"],
            }
        },
    )
    order = repository.recent_live_orders(limit=1)[0]
    assert order["cancel_attempt_matched"] is True
    assert order["not_open_reason"] is None
    assert order["not_open_reasons"] == []
    trader = FakeTrader()

    asyncio.run(
            _execute_near_close_taker_exits(
                repository=repository,
                live_trader=trader,
                settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52),
                watch_books={"token-down": make_book(bid=0.49, ask=0.50)},
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


def test_near_close_taker_exit_does_not_attempt_second_chance_when_first_fak_has_no_match(tmp_path) -> None:
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
                NEAR_CLOSE_SECOND_CHANCE_EXIT_ENABLED=False,
            ),
            watch_books={"token-doge": make_book(bid=0.87, ask=0.9)},
        )
    )

    assert len(trader.plans) == 1
    assert trader.plans[0].legs[0].target_price == 0.84
    assert exits[0]["second_chance_attempted"] is False
    assert exits[0]["status"] == "failed"


def test_near_close_stop_check_records_autopsy_when_not_triggered(tmp_path) -> None:
    class FakeTrader:
        async def execute(self, _plan):
            raise AssertionError("stop exit should not execute")

    repository = ScannerRepository(connect_db(tmp_path / "stop-check-autopsy.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="stop-check-entry",
            status="submitted",
            message="ok",
            order_type="GTD",
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="token-doge",
                    market_slug="doge-updown-5m-test",
                    outcome_label="Down",
                    target_price=0.87,
                    requested_size=5.0,
                    order_id="0xstop-check",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker", "crypto_winning_outcome": "Down"},
                )
            ],
        )
    )

    exits = asyncio.run(
        _execute_near_close_taker_exits(
            repository=repository,
            live_trader=FakeTrader(),
            settings=Settings(NEAR_CLOSE_TAKER_EXIT_PRICE=0.52, NEAR_CLOSE_HARD_STOP_OFFSET=0.025),
            watch_books={"token-doge": make_book(bid=0.86, ask=0.88)},
        )
    )
    events = repository.trade_autopsy_events(limit=10)
    stop_event = next(event for event in events if event["status"] == "trade_autopsy_stop_check")

    assert exits == []
    assert stop_event["details"]["stop_reason"] == "stop_not_triggered"
    assert stop_event["details"]["observed_best_bid"] == 0.86
    assert stop_event["details"]["trade_autopsy_id"].startswith("ta_")


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
