from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models.core import BookLevel, LiveExecutionLegResult, LiveExecutionResult, OrderBookSnapshot
from app.models.runtime import TradingControls
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.post_fill_hedge import build_post_fill_hedge_decision, execute_post_fill_hedges


def make_book(token_id: str = "token-no", *, bid: float = 0.01, ask: float = 0.03, size: float = 10.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
    )


def insert_market(repository: ScannerRepository) -> None:
    repository.connection.execute(
        """
        INSERT INTO markets (
            market_id, event_id, slug, question, end_date, outcome_labels_json, token_ids_json,
            category, tags_json, active, closed, liquidity, volume, raw_json, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "m-hedge",
            "e-hedge",
            "btc-updown-hedge",
            "Bitcoin Up or Down",
            (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            json.dumps(["Up", "Down"]),
            json.dumps(["token-yes", "token-no"]),
            "crypto",
            "[]",
            True,
            False,
            1000.0,
            1000.0,
            "{}",
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def insert_entry(
    repository: ScannerRepository,
    *,
    status: str = "CONFIRMED",
    price: float = 0.9,
    order_id: str = "entry-1",
) -> None:
    repository.connection.execute(
        """
        INSERT INTO live_trades (
            opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
            target_price, requested_size, order_id, status, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "near-close-entry",
            1,
            "BUY",
            "token-yes",
            "btc-updown-hedge",
            "Up",
            price,
            5.0,
            order_id,
            status,
            json.dumps(
                {
                    "strategy_variant": "near_close_maker",
                    "minutes_to_resolution": 2.1,
                }
            ),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def controls(*, armed: bool = True, kill_switch: bool = False) -> TradingControls:
    return TradingControls(
        live_trading_enabled=armed,
        auto_execute_enabled=armed,
        kill_switch_enabled=kill_switch,
    )


def first_entry(repository: ScannerRepository) -> dict:
    entries = repository.near_close_filled_entries_without_hedge(limit=5)
    assert len(entries) == 1
    return entries[0]


def test_hedge_not_considered_before_entry_fill(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "hedge-pending.db"))
    insert_market(repository)
    insert_entry(repository, status="SUBMITTED")

    assert repository.near_close_filled_entries_without_hedge(limit=5) == []


def test_hedge_side_is_opposite_of_entry_side(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "hedge-opposite.db"))
    insert_market(repository)
    insert_entry(repository)

    decision = build_post_fill_hedge_decision(
        entry=first_entry(repository),
        book=make_book(),
        settings=Settings(ENABLE_LIVE_TRADING=True),
        controls=controls(),
        repository=repository,
    )

    assert decision.plan is not None
    assert decision.plan.legs[0].action == "BUY"
    assert decision.plan.legs[0].token_id == "token-no"
    assert decision.plan.legs[0].outcome_label == "Down"


def test_hedge_skipped_when_locked_profit_below_minimum(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "hedge-profit.db"))
    insert_market(repository)
    insert_entry(repository, price=0.95)

    decision = build_post_fill_hedge_decision(
        entry=first_entry(repository),
        book=make_book(),
        settings=Settings(NEAR_CLOSE_HEDGE_MIN_LOCKED_PROFIT=0.03),
        controls=controls(),
        repository=repository,
    )

    assert decision.reason == "locked_profit_below_minimum"
    assert decision.should_place is False


def test_hedge_skipped_when_best_ask_above_max_price(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "hedge-ask.db"))
    insert_market(repository)
    insert_entry(repository)

    decision = build_post_fill_hedge_decision(
        entry=first_entry(repository),
        book=make_book(ask=0.04),
        settings=Settings(NEAR_CLOSE_HEDGE_MAX_BEST_ASK=0.03),
        controls=controls(),
        repository=repository,
    )

    assert decision.reason == "best_ask_above_hedge_max"
    assert decision.should_place is False


def test_hedge_not_duplicated(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "hedge-duplicate.db"))
    insert_market(repository)
    insert_entry(repository, order_id="entry-dup")
    repository.connection.execute(
        """
        INSERT INTO live_trades (
            opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
            target_price, requested_size, order_id, status, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "hedge:entry-dup",
            1,
            "BUY",
            "token-no",
            "btc-updown-hedge",
            "Down",
            0.03,
            5.0,
            "hedge-order",
            "submitted",
            json.dumps({"hedge_for_order_id": "entry-dup"}),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    assert repository.near_close_filled_entries_without_hedge(limit=5) == []


def test_kill_switch_prevents_hedge(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "hedge-kill.db"))
    insert_market(repository)
    insert_entry(repository)

    decision = build_post_fill_hedge_decision(
        entry=first_entry(repository),
        book=make_book(),
        settings=Settings(),
        controls=controls(kill_switch=True),
        repository=repository,
    )

    assert decision.reason == "kill_switch_enabled"
    assert decision.should_place is False


def test_shadow_mode_logs_decision_without_order(tmp_path) -> None:
    class FakeTrader:
        def __init__(self) -> None:
            self.called = False

        async def execute(self, plan):
            self.called = True
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status="submitted",
                message="ok",
                order_type=plan.legs[0].order_type,
                leg_results=[
                    LiveExecutionLegResult(
                        leg_index=1,
                        action="BUY",
                        token_id=plan.legs[0].token_id,
                        market_slug=plan.legs[0].market_slug,
                        outcome_label=plan.legs[0].outcome_label,
                        target_price=plan.legs[0].target_price,
                        requested_size=plan.legs[0].size,
                        order_id="hedge-order",
                        status="submitted",
                    )
                ],
            )

    repository = ScannerRepository(connect_db(tmp_path / "hedge-shadow.db"))
    insert_market(repository)
    insert_entry(repository)
    trader = FakeTrader()

    executed = asyncio.run(
        execute_post_fill_hedges(
            repository=repository,
            live_trader=trader,
            settings=Settings(ENABLE_LIVE_TRADING=False, NEAR_CLOSE_POST_FILL_HEDGE_ENABLED=True),
            controls=controls(armed=False),
            watch_books={"token-no": make_book()},
        )
    )

    events = repository.recent_execution_events(limit=3)
    assert executed == 0
    assert trader.called is False
    assert events[0]["status"] == "hedge_shadow"


def test_hedge_skip_event_is_not_repeated_for_same_entry(tmp_path) -> None:
    class FakeTrader:
        async def execute(self, plan):  # pragma: no cover - should not be called
            raise AssertionError("hedge should not execute")

    repository = ScannerRepository(connect_db(tmp_path / "hedge-skip-once.db"))
    insert_market(repository)
    insert_entry(repository, order_id="entry-skip-once")

    for _ in range(2):
        executed = asyncio.run(
            execute_post_fill_hedges(
                repository=repository,
                live_trader=FakeTrader(),
                settings=Settings(ENABLE_LIVE_TRADING=True, NEAR_CLOSE_POST_FILL_HEDGE_ENABLED=True),
                controls=controls(),
                watch_books={},
            )
        )
        assert executed == 0

    events = [
        event
        for event in repository.recent_execution_events(limit=5)
        if event["status"] == "hedge_skipped"
    ]
    assert len(events) == 1
    assert events[0]["message"] == "missing_opposite_orderbook"
