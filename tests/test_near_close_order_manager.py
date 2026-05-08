from __future__ import annotations

from app.config import Settings
from app.models.core import BookLevel, OrderBookSnapshot
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
