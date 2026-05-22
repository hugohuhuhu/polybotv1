from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models.core import BookLevel, LiveExecutionLegResult, LiveExecutionResult, OrderBookSnapshot
from app.models.runtime import TradingControls
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.post_fill_profit_take import (
    build_post_fill_profit_take_decision,
    execute_post_fill_profit_takes,
    profit_take_target_price,
)


def make_book(token_id: str = "token-yes", *, bid: float = 0.9, ask: float = 0.94, size: float = 10.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
    )


def controls(*, armed: bool = True, kill_switch: bool = False) -> TradingControls:
    return TradingControls(
        live_trading_enabled=armed,
        auto_execute_enabled=armed,
        kill_switch_enabled=kill_switch,
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
            "m-profit",
            "e-profit",
            "btc-updown-profit",
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
    order_id: str = "entry-profit",
) -> None:
    repository.connection.execute(
        """
        INSERT INTO live_trades (
            opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
            target_price, requested_size, order_id, status, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "near-close-entry-profit",
            1,
            "BUY",
            "token-yes",
            "btc-updown-profit",
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


def first_entry(repository: ScannerRepository) -> dict:
    entries = repository.near_close_filled_entries_without_profit_take(limit=5)
    assert len(entries) == 1
    return entries[0]


def test_profit_take_ladder_targets() -> None:
    settings = Settings()

    assert profit_take_target_price(settings, 0.86) == 0.95
    assert profit_take_target_price(settings, 0.88) == 0.955
    assert profit_take_target_price(settings, 0.90) == 0.965
    assert profit_take_target_price(settings, 0.92) == 0.97
    assert profit_take_target_price(settings, 0.94) == 0.985
    assert profit_take_target_price(settings, 0.95) is None


def test_profit_take_not_considered_before_entry_fill(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "profit-pending.db"))
    insert_market(repository)
    insert_entry(repository, status="SUBMITTED")

    assert repository.near_close_filled_entries_without_profit_take(limit=5) == []


def test_profit_take_builds_position_backed_sell_after_fill(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "profit-sell.db"))
    insert_market(repository)
    insert_entry(repository, price=0.9)

    decision = build_post_fill_profit_take_decision(
        entry=first_entry(repository),
        book=make_book(bid=0.91, ask=0.94),
        settings=Settings(
            ENABLE_LIVE_TRADING=True,
            NEAR_CLOSE_PROFIT_TAKE_LIVE_ENABLED=True,
            NEAR_CLOSE_PROFIT_TAKE_MIN_NET_PROFIT=0.1,
        ),
        controls=controls(),
        repository=repository,
    )

    assert decision.should_place is True
    assert decision.plan is not None
    assert decision.plan.legs[0].action == "SELL"
    assert decision.plan.legs[0].token_id == "token-yes"
    assert decision.plan.legs[0].target_price == 0.965
    assert decision.plan.legs[0].post_only is True


def test_profit_take_uses_fak_when_bid_already_reaches_target(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "profit-fak.db"))
    insert_market(repository)
    insert_entry(repository, price=0.86)

    decision = build_post_fill_profit_take_decision(
        entry=first_entry(repository),
        book=make_book(bid=0.951, ask=0.96),
        settings=Settings(
            ENABLE_LIVE_TRADING=True,
            NEAR_CLOSE_PROFIT_TAKE_LIVE_ENABLED=True,
            NEAR_CLOSE_PROFIT_TAKE_MIN_NET_PROFIT=0.1,
        ),
        controls=controls(),
        repository=repository,
    )

    assert decision.should_place is True
    assert decision.plan is not None
    assert decision.plan.legs[0].order_type == "FAK"
    assert decision.plan.legs[0].post_only is False


def test_profit_take_skips_when_entry_above_ladder(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "profit-high-entry.db"))
    insert_market(repository)
    insert_entry(repository, price=0.95)

    decision = build_post_fill_profit_take_decision(
        entry=first_entry(repository),
        book=make_book(),
        settings=Settings(),
        controls=controls(),
        repository=repository,
    )

    assert decision.reason == "entry_above_profit_take_ladder"
    assert decision.should_place is False


def test_profit_take_not_duplicated(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "profit-duplicate.db"))
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
            "profit-take:entry-dup",
            1,
            "SELL",
            "token-yes",
            "btc-updown-profit",
            "Up",
            0.965,
            5.0,
            "profit-order",
            "submitted",
            json.dumps({"strategy_variant": "near_close_profit_take", "profit_take_for_order_id": "entry-dup"}),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    assert repository.near_close_filled_entries_without_profit_take(limit=5) == []


def test_kill_switch_prevents_profit_take(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "profit-kill.db"))
    insert_market(repository)
    insert_entry(repository)

    decision = build_post_fill_profit_take_decision(
        entry=first_entry(repository),
        book=make_book(),
        settings=Settings(),
        controls=controls(kill_switch=True),
        repository=repository,
    )

    assert decision.reason == "kill_switch_enabled"
    assert decision.should_place is False


def test_profit_take_shadow_logs_without_order(tmp_path) -> None:
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
                        action="SELL",
                        token_id=plan.legs[0].token_id,
                        market_slug=plan.legs[0].market_slug,
                        outcome_label=plan.legs[0].outcome_label,
                        target_price=plan.legs[0].target_price,
                        requested_size=plan.legs[0].size,
                        order_id="profit-order",
                        status="submitted",
                    )
                ],
            )

    repository = ScannerRepository(connect_db(tmp_path / "profit-shadow.db"))
    insert_market(repository)
    insert_entry(repository)
    trader = FakeTrader()

    executed = asyncio.run(
        execute_post_fill_profit_takes(
            repository=repository,
            live_trader=trader,
            settings=Settings(ENABLE_LIVE_TRADING=False, NEAR_CLOSE_PROFIT_TAKE_LIVE_ENABLED=True),
            controls=controls(armed=False),
            watch_books={"token-yes": make_book()},
        )
    )

    events = repository.recent_execution_events(limit=3)
    assert executed == 0
    assert trader.called is False
    assert events[0]["status"] == "profit_take_shadow"
