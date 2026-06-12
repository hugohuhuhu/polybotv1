from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import Settings
from app.main import (
    _monitored_markets_expired,
    _watch_delay_sec_for_near_close_pacing,
    _watch_initial_scan_delay_sec,
)


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


def test_watch_delay_uses_fast_pacing_for_cached_crypto_updown_prewarm() -> None:
    now = datetime(2026, 5, 28, 2, 20, tzinfo=timezone.utc)
    settings = Settings(
        SCAN_INTERVAL_SEC=8,
        NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY=True,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_PREWARM_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_FAST_SCAN_SEC=2,
    )

    assert _watch_delay_sec_for_near_close_pacing(
        settings,
        [SimpleNamespace(end_date=now + timedelta(seconds=75))],
        now=now,
    ) == 2.0


def test_watch_delay_wakes_at_entry_window_boundary_when_closer_than_fast_delay() -> None:
    now = datetime(2026, 5, 28, 2, 20, tzinfo=timezone.utc)
    settings = Settings(
        SCAN_INTERVAL_SEC=8,
        NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY=True,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_PREWARM_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_FAST_SCAN_SEC=2,
    )

    assert _watch_delay_sec_for_near_close_pacing(
        settings,
        [SimpleNamespace(end_date=now + timedelta(seconds=61))],
        now=now,
    ) == 1.0


def test_initial_watch_scan_waits_for_utc_bucket_prewarm() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY=True,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_PREWARM_SECONDS=60,
    )

    assert _watch_initial_scan_delay_sec(
        settings,
        now=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
    ) == 180.0
    assert _watch_initial_scan_delay_sec(
        settings,
        now=datetime(2026, 5, 28, 12, 2, 59, tzinfo=timezone.utc),
    ) == 1.0
    assert _watch_initial_scan_delay_sec(
        settings,
        now=datetime(2026, 5, 28, 12, 3, tzinfo=timezone.utc),
    ) == 0.0


def test_initial_watch_scan_skips_final_seconds_and_targets_next_bucket() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY=True,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_PREWARM_SECONDS=60,
    )

    assert _watch_initial_scan_delay_sec(
        settings,
        now=datetime(2026, 5, 28, 12, 4, 45, tzinfo=timezone.utc),
    ) == 195.0


def test_empty_watch_pool_scans_only_during_bucket_prewarm_and_entry_window() -> None:
    settings = Settings(
        SCAN_INTERVAL_SEC=8,
        NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY=True,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=30,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=60,
        NEAR_CLOSE_CRYPTO_UPDOWN_PREWARM_SECONDS=60,
    )

    assert _watch_delay_sec_for_near_close_pacing(
        settings,
        [],
        now=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
    ) == 180.0
    assert _watch_delay_sec_for_near_close_pacing(
        settings,
        [],
        now=datetime(2026, 5, 28, 12, 3, tzinfo=timezone.utc),
    ) == 8.0
    assert _watch_delay_sec_for_near_close_pacing(
        settings,
        [],
        now=datetime(2026, 5, 28, 12, 3, 59, tzinfo=timezone.utc),
    ) == 1.0
    assert _watch_delay_sec_for_near_close_pacing(
        settings,
        [],
        now=datetime(2026, 5, 28, 12, 4, 31, tzinfo=timezone.utc),
    ) == 209.0
