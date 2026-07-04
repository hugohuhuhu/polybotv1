from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.clients.websocket_client import OrderBookState
from app.main import _merge_timestamped_last_trade_observations
from app.models.core import BookLevel, OrderBookSnapshot


def test_last_trade_event_preserves_exchange_timestamp_across_book_updates() -> None:
    state = OrderBookState()
    timestamp_ms = 1_750_428_146_322

    asyncio.run(
        state.handle_message(
            {
                "event_type": "last_trade_price",
                "asset_id": "token-yes",
                "market": "condition-1",
                "price": "0.456",
                "timestamp": str(timestamp_ms),
            }
        )
    )
    asyncio.run(
        state.handle_message(
            {
                "event_type": "book",
                "asset_id": "token-yes",
                "market": "condition-1",
                "bids": [{"price": "0.45", "size": "10"}],
                "asks": [{"price": "0.46", "size": "12"}],
            }
        )
    )

    snapshot = state.books["token-yes"]
    assert snapshot.last_trade_price == 0.456
    assert snapshot.last_trade_at == datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)


def test_last_trade_without_exchange_timestamp_is_not_treated_as_fresh() -> None:
    state = OrderBookState()

    asyncio.run(
        state.handle_message(
            {
                "event_type": "last_trade_price",
                "asset_id": "token-yes",
                "price": "0.42",
            }
        )
    )

    assert state.books["token-yes"].last_trade_price == 0.42
    assert state.books["token-yes"].last_trade_at is None


def test_fast_monitor_rest_book_receives_timestamped_websocket_last_trade() -> None:
    last_trade_at = datetime.now(timezone.utc)
    rest_book = OrderBookSnapshot(
        token_id="token-yes",
        bids=[BookLevel(price=0.48, size=10)],
        asks=[BookLevel(price=0.49, size=10)],
        last_trade_price=0.9,
    )

    _merge_timestamped_last_trade_observations(
        {"token-yes": rest_book},
        {"token-yes": (0.47, last_trade_at)},
    )

    assert rest_book.last_trade_price == 0.47
    assert rest_book.last_trade_at == last_trade_at
