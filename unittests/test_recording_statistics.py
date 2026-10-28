"""External statistics of recording sensors (issue #599).

Hours without consumption are not written, so a day after a day without
consumption has no stored row before it inside the window the hourly import
reads. The base sum for that day used to fall back to the latest stored row,
which already contains the day's values, and every hourly run added them
again (4.2 kWh -> 8.4 kWh -> 12.6 kWh).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.bosch.sensor.recording import RecordingSensor

UTC = dt_util.UTC
HISTORY = datetime(2026, 10, 15, 10, tzinfo=UTC)
TODAY = datetime(2026, 10, 17, tzinfo=UTC)
CONSUMPTION_HOUR = TODAY.replace(hour=13)


class FakeRecorder:
    """External statistics of one statistic_id, keyed by start timestamp."""

    def __init__(self, statistic_id: str) -> None:
        """Initialize the fake recorder."""
        self.statistic_id = statistic_id
        self.rows: dict[float, dict[str, float]] = {}

    def add(self, stats: list[Any]) -> None:
        """Upsert like async_add_external_statistics."""
        for stat in stats:
            start = dt_util.as_timestamp(stat["start"])
            self.rows[start] = {"start": start, "state": stat["state"], "sum": stat["sum"]}

    def _result(self, rows: list[dict[str, float]]) -> dict[str, list[dict[str, float]]]:
        return {self.statistic_id: rows} if rows else {}

    async def last(self) -> dict[str, list[dict[str, float]]]:
        """Like get_last_statistics with one row."""
        return self._result([self.rows[max(self.rows)]] if self.rows else [])

    async def period(
        self, start_time: datetime, end_time: datetime
    ) -> dict[str, list[dict[str, float]]]:
        """Like statistics_during_period, ascending."""
        start, end = dt_util.as_timestamp(start_time), dt_util.as_timestamp(end_time)
        return self._result(
            [self.rows[ts] for ts in sorted(self.rows) if start <= ts < end]
        )


def _gateway_day() -> list[dict[str, Any]]:
    """Today's recording rows as the gateway reports them, up to 13:00."""
    return [
        {"d": TODAY + timedelta(hours=hour), "value": 4.2 if hour == 13 else 0.0}
        for hour in range(14)
    ]


def _sensor(recorder: FakeRecorder) -> RecordingSensor:
    sensor = RecordingSensor.__new__(RecordingSensor)
    sensor._new_stats_api = True
    sensor._short_id = "actualdhwpower"
    sensor._statistic_import_lock = asyncio.Lock()
    rows = _gateway_day()
    sensor._bosch_object = SimpleNamespace(
        state=rows,
        fetch_range=AsyncMock(return_value=dict(enumerate(rows))),
    )
    sensor.get_last_stat = recorder.last
    sensor.get_stats_from_ha_db = recorder.period
    sensor.add_external_stats = recorder.add
    return sensor


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """Set dt_util.now; start_of_local_day follows it."""
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", UTC)
    current = {"now": TODAY}
    monkeypatch.setattr(dt_util, "now", lambda time_zone=None: current["now"])

    def set_now(value: datetime) -> None:
        current["now"] = value

    return set_now


async def test_hourly_runs_do_not_add_the_same_hour_again(clock) -> None:
    """The reported case: no rows yesterday, 4.2 kWh today at 13:00."""
    recorder = FakeRecorder("recording:actualdhwpowerexternal")
    recorder.add([{"start": HISTORY, "state": 98.3, "sum": 98.3}])
    sensor = _sensor(recorder)

    for hour in (14, 15, 16):
        clock(TODAY.replace(hour=hour, minute=6))
        await sensor._insert_statistics()

        row = recorder.rows[dt_util.as_timestamp(CONSUMPTION_HOUR)]
        assert row["state"] == pytest.approx(4.2)
        assert row["sum"] == pytest.approx(102.5), f"after the run at {hour}:06"


def test_base_row_before_the_day_is_used_when_present() -> None:
    sensor = RecordingSensor.__new__(RecordingSensor)
    sensor._short_id = "x"
    rows = [
        {"start": dt_util.as_timestamp(HISTORY), "state": 1.0, "sum": 10.0},
        {"start": dt_util.as_timestamp(CONSUMPTION_HOUR), "state": 4.2, "sum": 14.2},
    ]
    base = sensor.get_last_stats_before_date({sensor.statistic_id: rows}, TODAY)
    assert base["sum"] == pytest.approx(10.0)


def test_base_without_row_before_the_day_excludes_the_day() -> None:
    """The base is the sum before the earliest row of the day, not the latest row."""
    sensor = RecordingSensor.__new__(RecordingSensor)
    sensor._short_id = "x"
    start = dt_util.as_timestamp(CONSUMPTION_HOUR)
    rows = [
        {"start": start, "state": 4.2, "sum": 102.5},
        {"start": start + 3600, "state": 1.0, "sum": 103.5},
    ]
    base = sensor.get_last_stats_before_date({sensor.statistic_id: rows}, TODAY)
    assert base["sum"] == pytest.approx(98.3)
    assert base["start"] == pytest.approx(start - 3600)


def test_base_without_rows_is_zero() -> None:
    sensor = RecordingSensor.__new__(RecordingSensor)
    sensor._short_id = "x"
    base = sensor.get_last_stats_before_date({}, TODAY)
    assert base["sum"] == 0.0
