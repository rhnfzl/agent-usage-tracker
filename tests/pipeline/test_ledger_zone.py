"""config.machine_zone_agrees: may a record counted in the machine's zone stand in the ledger zone."""

from __future__ import annotations

import functools
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent_usage_tracker import config

# Havana springs forward at its midnight, so 2026-03-08 has no 00:00; Detroit does at 02:00
START, END = (datetime(2026, 3, 7, tzinfo=timezone.utc), datetime(2026, 3, 9, tzinfo=timezone.utc))


@pytest.fixture
def machine(monkeypatch):
    def put(zone: str) -> None:
        monkeypatch.setattr(config, "_machine_offset",
                            lambda t: datetime.fromtimestamp(t, ZoneInfo(zone)).utcoffset())
        monkeypatch.setattr(config, "_machine_agrees_since_2024",
                            functools.cache(config._machine_agrees_since_2024.__wrapped__))
    return put


def test_a_zone_agrees_with_itself_across_a_missing_midnight(machine):
    machine("America/Havana")
    assert config.machine_zone_agrees(ZoneInfo("America/Havana"), START, END)


def test_zones_that_part_for_two_hours_disagree(machine):
    # both have a midnight at -05:00 on 7 and 9 March, but at 05:30Z Havana says 01:30, Detroit 00:30
    machine("America/Havana")
    assert not config.machine_zone_agrees(ZoneInfo("America/Detroit"), START, END)
    assert config.machine_zone_agrees(ZoneInfo("America/Detroit"), START, START.replace(hour=4))


def test_a_record_without_a_zone_holds_only_where_the_machine_agrees(machine):
    machine("Europe/Amsterdam")
    assert config.record_zone_ok({}, "Europe/Amsterdam")
    assert config.record_zone_ok({"ledger_timezone": "Asia/Tokyo"}, "Asia/Tokyo")
    assert not config.record_zone_ok({"ledger_timezone": "Asia/Tokyo"}, "Europe/Amsterdam")
    machine("UTC")
    assert not config.record_zone_ok({}, "Europe/Amsterdam")


def test_an_unknown_zone_name_is_the_operator_error(machine):
    machine("UTC")
    with pytest.raises(config.AutError, match="ledger_timezone 'Mars/Base' is unknown here"):
        config.record_zone_ok({}, "Mars/Base")
