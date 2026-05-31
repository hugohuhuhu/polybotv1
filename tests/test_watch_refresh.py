from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.main import _monitored_markets_expired


def test_monitored_markets_expired_when_all_cached_markets_are_past_end() -> None:
    now = datetime(2026, 5, 28, 2, 20, tzinfo=timezone.utc)
    markets = [
        SimpleNamespace(end_date=now - timedelta(seconds=20)),
        SimpleNamespace(end_date=now - timedelta(minutes=1)),
    ]

    assert _monitored_markets_expired(markets, now=now) is True


def test_monitored_markets_not_expired_when_any_cached_market_is_still_open() -> None:
    now = datetime(2026, 5, 28, 2, 20, tzinfo=timezone.utc)
    markets = [
        SimpleNamespace(end_date=now - timedelta(seconds=20)),
        SimpleNamespace(end_date=now + timedelta(minutes=1)),
    ]

    assert _monitored_markets_expired(markets, now=now) is False


def test_monitored_markets_not_expired_without_reliable_end_dates() -> None:
    now = datetime(2026, 5, 28, 2, 20, tzinfo=timezone.utc)

    assert _monitored_markets_expired([], now=now) is False
    assert _monitored_markets_expired([SimpleNamespace(end_date=None)], now=now) is False
