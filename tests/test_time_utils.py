from __future__ import annotations

import pytest

from app.utils.time_utils import seconds_until_next_minute_second


def test_seconds_until_next_minute_second_targets_current_minute_when_before_second() -> None:
    assert seconds_until_next_minute_second(120.2, target_second=1.0) == pytest.approx(0.8)


def test_seconds_until_next_minute_second_rolls_to_next_minute_after_target() -> None:
    assert seconds_until_next_minute_second(121.1, target_second=1.0) == pytest.approx(59.9)


def test_seconds_until_next_minute_second_avoids_tight_loop_at_boundary() -> None:
    assert seconds_until_next_minute_second(121.0, target_second=1.0) == pytest.approx(60.0)
