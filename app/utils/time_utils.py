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
