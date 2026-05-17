from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def minutes_to(target: datetime | None, now: datetime | None = None) -> float | None:
    if target is None:
        return None
    current = now or datetime.now(timezone.utc)
    delta = target - current.astimezone(timezone.utc)
    return delta.total_seconds() / 60


def is_older_than(target: datetime, threshold_sec: int, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    return (current.astimezone(timezone.utc) - target).total_seconds() >= threshold_sec


def seconds_until_next_minute_second(now_timestamp: float, *, target_second: float = 1.0) -> float:
    """Return seconds until the next minute boundary at the requested second."""

    if target_second < 0 or target_second >= 60:
        raise ValueError("target_second must be in [0, 60).")
    seconds_into_minute = now_timestamp % 60
    delay = target_second - seconds_into_minute
    if delay <= 0.05:
        delay += 60
    return delay


def humanize_seconds(value: float) -> str:
    delta = timedelta(seconds=max(value, 0))
    total = int(delta.total_seconds())
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小時 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"
