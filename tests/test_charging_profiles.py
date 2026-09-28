"""Unit tests for charging_profiles.py: validation, installation rules and the composite-schedule
calculation (OCPP 1.6 s3.13, s5.7, s7.8-s7.14).

Pure and fast: no database, no network. Every expected number below is worked out by hand from
the spec's rules (in a comment where it is not obvious), not read back from the code under test.
"""

from datetime import UTC, datetime, timedelta

import pytest
from ocpp.v16.enums import ChargingProfilePurposeType as Purpose
from ocpp.v16.enums import ChargingRateUnitType as Unit

from charging_profiles import (
    DEFAULT_HARDWARE_LIMIT,
    MAX_COMPOSITE_SECONDS,
    ChargingProfileSet,
    installation_problem,
    parse_profile,
    purpose_connector_problem,
)

# A Monday, 08:00 UTC.
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
ANCHOR = "2026-01-01T00:00:00Z"  # a Thursday, midnight
HARDWARE_W = 22080.0  # 32 A * 230 V * 3 phases


def make_profile(
    profile_id=1, purpose="TxDefaultProfile", stack=0, kind="Absolute", unit="W",
    periods=((0, 11000),), duration=None, start=ANCHOR, recurrency=None, phases=None,
    valid_from=None, valid_to=None, transaction_id=None,
):
    schedule = {
        "chargingRateUnit": unit,
        "chargingSchedulePeriod": [
            {"startPeriod": s, "limit": lim, **({"numberPhases": phases} if phases else {})}
            for s, lim in periods
        ],
    }
    if duration is not None:
        schedule["duration"] = duration
    if start is not None and kind != "Relative":
        schedule["startSchedule"] = start
    profile = {
        "chargingProfileId": profile_id,
        "stackLevel": stack,
        "chargingProfilePurpose": purpose,
        "chargingProfileKind": kind,
        "chargingSchedule": schedule,
    }
    if recurrency:
        profile["recurrencyKind"] = recurrency
    if valid_from:
        profile["validFrom"] = valid_from
    if valid_to:
        profile["validTo"] = valid_to
    if transaction_id is not None:
        profile["transactionId"] = transaction_id
    return profile


def installed(*placed):
    """A ChargingProfileSet holding (connector_id, profile dict) pairs, received at T0."""
    profiles = ChargingProfileSet()
    for connector_id, profile in placed:
        profiles.install(connector_id, profile, T0)
    return profiles


def periods_of(schedule):
    return [(p["start_period"], p["limit"]) for p in schedule.periods]


def composite(profiles, connector_id=1, duration=7200, unit=Unit.watts, start=T0, **kwargs):
    kwargs.setdefault("connectors", [1])
    return profiles.composite(connector_id, start, duration, unit, **kwargs)


# --------------------------------------------------------------------------------------------
# parse_profile
# --------------------------------------------------------------------------------------------


def test_parse_accepts_camel_case_and_snake_case_alike():
    camel = parse_profile(make_profile())
    snake = parse_profile(camel.to_wire())
    assert camel == snake
    assert camel.to_wire()["charging_profile_purpose"] == "TxDefaultProfile"


def test_to_wire_keeps_only_what_was_given():
    wire = parse_profile(make_profile()).to_wire()
    assert "transaction_id" not in wire
    assert "valid_from" not in wire
    assert wire["charging_schedule"]["charging_schedule_period"] == [
        {"start_period": 0, "limit": 11000.0}
    ]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"periods": ((60, 10),)}, "startPeriod 0"),
        ({"periods": ((0, 10), (100, 8), (100, 6))}, "strictly increasing"),
        ({"periods": ((0, 10.55),)}, "one digit fraction"),
        ({"periods": ((0, -1),)}, "limit"),
        ({"kind": "Recurring"}, "recurrencyKind"),
        ({"kind": "Absolute", "start": None}, "startSchedule"),
        ({"kind": "Absolute", "recurrency": "Daily"}, "only applies to a Recurring"),
        ({"transaction_id": 5}, "only valid for a TxProfile"),
        ({"stack": -1}, "stack_level"),
        ({"unit": "kW"}, "charging_rate_unit"),
        ({"phases": 4}, "number_phases"),
        (
            {"valid_from": "2026-02-01T00:00:00Z", "valid_to": "2026-01-01T00:00:00Z"},
            "validTo must be after validFrom",
        ),
        ({"start": "2026-01-01T00:00:00"}, "timezone"),
    ],
)
def test_parse_rejects_invalid_profiles(overrides, message):
    with pytest.raises(ValueError, match=message):
        parse_profile(make_profile(**overrides))


def test_parse_rejects_unknown_fields_and_non_objects():
    bad = make_profile()
    bad["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        parse_profile(bad)
    with pytest.raises(ValueError, match="JSON object"):
        parse_profile("not an object")


def test_relative_profile_needs_no_start_schedule():
    parse_profile(make_profile(kind="Relative", start=None))


# --------------------------------------------------------------------------------------------
# Where a purpose may go, and what a charger refuses
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "purpose, connector_id, problem",
    [
        ("ChargePointMaxProfile", 0, False),
        ("ChargePointMaxProfile", 1, True),  # s3.13.1: only at connector 0
        ("TxDefaultProfile", 0, False),
        ("TxDefaultProfile", 2, False),
        ("TxProfile", 1, False),
        ("TxProfile", 0, True),  # s3.13.1: only at connector > 0
    ],
)
def test_purpose_connector_rules(purpose, connector_id, problem):
    profile = parse_profile(make_profile(purpose=purpose))
    assert bool(purpose_connector_problem(profile, connector_id)) is problem


CHARGER = dict(
    known_connectors={1, 2}, max_stack_level=5, max_periods=3,
    allowed_units={Unit.amps, Unit.watts}, open_transaction_id=None, installed_after=1,
    max_installed=2,
)


def test_charger_accepts_an_ordinary_profile():
    assert installation_problem(parse_profile(make_profile()), 1, **CHARGER) is None


@pytest.mark.parametrize(
    "profile_kwargs, charger_kwargs, connector_id, message",
    [
        ({}, {}, 9, "unknown connector"),
        ({"stack": 6}, {}, 1, "ChargeProfileMaxStackLevel"),
        ({"periods": ((0, 1), (10, 2), (20, 3), (30, 4))}, {}, 1, "ChargingScheduleMaxPeriods"),
        ({"unit": "W"}, {"allowed_units": {Unit.amps}}, 1, "not supported"),
        ({}, {"installed_after": 3}, 1, "MaxChargingProfilesInstalled"),
        ({"purpose": "TxProfile"}, {}, 1, "no transaction is active"),
        (
            {"purpose": "TxProfile", "transaction_id": 7},
            {"open_transaction_id": 8},
            1,
            "does not match",
        ),
        ({"purpose": "ChargePointMaxProfile"}, {}, 1, "connector 0"),
    ],
)
def test_charger_refusals(profile_kwargs, charger_kwargs, connector_id, message):
    profile = parse_profile(make_profile(**profile_kwargs))
    problem = installation_problem(
        profile, connector_id, **{**CHARGER, **charger_kwargs}
    )
    assert problem is not None and message in problem


def test_tx_profile_is_fine_with_a_matching_or_absent_transaction_id():
    for transaction_id in (None, 8):
        profile = parse_profile(make_profile(purpose="TxProfile", transaction_id=transaction_id))
        assert installation_problem(
            profile, 1, **{**CHARGER, "open_transaction_id": 8}
        ) is None


def test_a_remote_start_tx_profile_needs_no_open_transaction_yet():
    profile = parse_profile(make_profile(purpose="TxProfile"))
    assert installation_problem(profile, 1, **CHARGER, transaction_pending=True) is None


# --------------------------------------------------------------------------------------------
# Installing and clearing (s3.13.2)
# --------------------------------------------------------------------------------------------


def test_same_profile_id_replaces_wherever_it_was():
    profiles = installed((1, make_profile(profile_id=5, stack=0)))
    replaced = profiles.install(2, make_profile(profile_id=5, stack=3), T0)
    assert len(replaced) == 1
    assert [(e.connector_id, e.profile.stack_level) for e in profiles.entries()] == [(2, 3)]


def test_same_connector_purpose_and_stack_level_replaces():
    profiles = installed((1, make_profile(profile_id=1, stack=2)))
    profiles.install(1, make_profile(profile_id=2, stack=2, periods=((0, 5000),)), T0)
    assert [e.profile.charging_profile_id for e in profiles.entries()] == [2]


def test_same_stack_level_on_another_connector_or_purpose_coexists():
    profiles = installed(
        (0, make_profile(profile_id=1, stack=0)),  # TxDefault on connector 0 (all connectors)
        (1, make_profile(profile_id=2, stack=0)),  # TxDefault on connector 1 only
        (1, make_profile(profile_id=3, stack=0, purpose="TxProfile")),
    )
    assert len(profiles) == 3


def test_count_after_install_accounts_for_replacement():
    profiles = installed((1, make_profile(profile_id=1)), (1, make_profile(profile_id=2, stack=1)))
    assert profiles.count_after_install(1, parse_profile(make_profile(profile_id=1))) == 2
    assert profiles.count_after_install(1, parse_profile(make_profile(profile_id=9, stack=9))) == 3


def test_clear_by_id_purpose_stack_connector_and_everything():
    def fresh():
        return installed(
            (0, make_profile(profile_id=1, purpose="ChargePointMaxProfile", stack=0)),
            (0, make_profile(profile_id=2, stack=0)),
            (1, make_profile(profile_id=3, stack=1)),
            (1, make_profile(profile_id=4, stack=2, purpose="TxProfile")),
        )

    assert fresh().clear(profile_id=3) == 1
    assert fresh().clear(purpose=Purpose.tx_default_profile) == 2
    assert fresh().clear(stack_level=0) == 2
    assert fresh().clear(connector_id=1) == 2
    # Criteria are read together: connector 1 AND TxDefaultProfile is just profile 3.
    assert fresh().clear(connector_id=1, purpose=Purpose.tx_default_profile) == 1
    assert fresh().clear(profile_id=3, connector_id=0) == 0
    assert fresh().clear() == 4
    assert fresh().clear(profile_id=99) == 0


def test_a_tx_profile_ends_with_its_transaction_but_defaults_stay():
    profiles = installed(
        (1, make_profile(profile_id=1, purpose="TxProfile")),
        (1, make_profile(profile_id=2)),
        (2, make_profile(profile_id=3, purpose="TxProfile")),
    )
    assert profiles.drop_tx_profiles(1) == 1
    assert sorted(e.profile.charging_profile_id for e in profiles.entries()) == [2, 3]


# --------------------------------------------------------------------------------------------
# The composite schedule
# --------------------------------------------------------------------------------------------


def test_the_specs_own_example_a_daily_6kw_cap_between_0800_and_2000():
    """s3.13.7: 11 kW, 6 kW from 08:00, 11 kW again from 20:00, repeating daily."""
    daily = make_profile(
        profile_id=100, kind="Recurring", recurrency="Daily", duration=86400,
        start="2013-01-01T00:00:00Z", periods=((0, 11000), (28800, 6000), (72000, 11000)),
        phases=3,
    )
    profiles = installed((0, daily))
    start = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
    schedule = composite(profiles, duration=16 * 3600, start=start)
    # From 06:00: 11 kW until 08:00 (+7200 s), 6 kW until 20:00 (+50400 s), then 11 kW.
    assert periods_of(schedule) == [(0, 11000.0), (7200, 6000.0), (50400, 11000.0)]


def test_no_profiles_at_all_means_the_hardware_limit():
    schedule = composite(ChargingProfileSet())
    assert periods_of(schedule) == [(0, HARDWARE_W)]
    assert DEFAULT_HARDWARE_LIMIT.watts == HARDWARE_W


def test_a_limit_is_reported_in_the_unit_asked_for():
    profiles = installed((1, make_profile(unit="A", periods=((0, 16),), phases=3)))
    # 16 A * 230 V * 3 phases = 11040 W; one phase would be 3680 W.
    assert periods_of(composite(profiles, unit=Unit.watts)) == [(0, 11040.0)]
    assert periods_of(composite(profiles, unit=Unit.amps)) == [(0, 16.0)]
    single = installed((1, make_profile(unit="A", periods=((0, 16),), phases=1)))
    assert periods_of(composite(single, unit=Unit.watts)) == [(0, 3680.0)]


def test_missing_number_phases_means_three():
    profiles = installed((1, make_profile(unit="A", periods=((0, 10),))))
    assert periods_of(composite(profiles, unit=Unit.watts)) == [(0, 6900.0)]  # 10*230*3


def test_an_absolute_schedule_ends_after_its_duration_and_falls_back():
    profiles = installed(
        (1, make_profile(periods=((0, 5000),), start="2026-01-05T08:00:00Z", duration=1800))
    )
    schedule = composite(profiles, duration=3600)
    assert periods_of(schedule) == [(0, 5000.0), (1800, HARDWARE_W)]


def test_an_absolute_schedule_that_has_not_started_yet_does_not_apply():
    profiles = installed((1, make_profile(periods=((0, 5000),), start="2026-01-05T08:30:00Z")))
    assert periods_of(composite(profiles, duration=3600)) == [(0, HARDWARE_W), (1800, 5000.0)]


def test_duration_longer_than_the_periods_keeps_the_last_value():
    """s5.16.4: "the Charge Point SHALL keep the value of the last chargingSchedulePeriod until
    duration has ended"."""
    profiles = installed(
        (
            1,
            make_profile(
                periods=((0, 9000), (600, 4000)), start="2026-01-05T08:00:00Z", duration=3000
            ),
        )
    )
    assert periods_of(composite(profiles, duration=3600)) == [
        (0, 9000.0), (600, 4000.0), (3000, HARDWARE_W),
    ]


def test_periods_beyond_the_duration_are_never_reached():
    profiles = installed(
        (
            1,
            make_profile(
                periods=((0, 9000), (2000, 1000)), start="2026-01-05T08:00:00Z", duration=1000
            ),
        )
    )
    assert periods_of(composite(profiles, duration=3600)) == [(0, 9000.0), (1000, HARDWARE_W)]


def test_higher_stack_level_prevails_and_the_lower_shows_through_when_it_ends():
    profiles = installed(
        (1, make_profile(profile_id=1, stack=0, periods=((0, 10000),))),
        (
            1,
            make_profile(
                profile_id=2, stack=1, periods=((0, 3000),), start="2026-01-05T08:00:00Z",
                duration=1200,
            ),
        ),
    )
    assert periods_of(composite(profiles, duration=3600)) == [(0, 3000.0), (1200, 10000.0)]


def test_a_stack_without_a_duration_never_falls_back():
    """s3.13.2: "If you use Stacking without a duration, on the highest stack level, the Charge
    Point will never fall back to a lower stack level profile."""
    profiles = installed(
        (1, make_profile(profile_id=1, stack=0, periods=((0, 10000),))),
        (1, make_profile(profile_id=2, stack=1, periods=((0, 3000),))),
    )
    assert periods_of(composite(profiles, duration=3600)) == [(0, 3000.0)]


def test_valid_from_and_valid_to_bound_a_profile():
    profiles = installed(
        (
            1,
            make_profile(
                periods=((0, 4000),), valid_from="2026-01-05T08:10:00Z",
                valid_to="2026-01-05T08:40:00Z",
            ),
        )
    )
    assert periods_of(composite(profiles, duration=3600)) == [
        (0, HARDWARE_W), (600, 4000.0), (2400, HARDWARE_W),
    ]


def test_a_tx_profile_overrides_the_default_and_the_default_returns_without_it():
    default = make_profile(profile_id=1, periods=((0, 10000),))
    tx = make_profile(profile_id=2, purpose="TxProfile", periods=((0, 2000),))
    profiles = installed((0, default), (1, tx))
    assert periods_of(composite(profiles)) == [(0, 2000.0)]
    profiles.drop_tx_profiles(1)
    assert periods_of(composite(profiles)) == [(0, 10000.0)]


def test_a_connector_specific_default_overrides_the_all_connectors_default_for_that_connector():
    profiles = installed(
        (0, make_profile(profile_id=1, periods=((0, 10000),))),
        (2, make_profile(profile_id=2, periods=((0, 4000),))),
    )
    assert periods_of(composite(profiles, connector_id=1, connectors=[1, 2])) == [(0, 10000.0)]
    assert periods_of(composite(profiles, connector_id=2, connectors=[1, 2])) == [(0, 4000.0)]


def test_charge_point_max_profile_caps_whichever_is_lower():
    cap = make_profile(profile_id=1, purpose="ChargePointMaxProfile", periods=((0, 6000),))
    profiles = installed((0, cap), (1, make_profile(profile_id=2, periods=((0, 11000),))))
    assert periods_of(composite(profiles)) == [(0, 6000.0)]  # the cap is lower
    profiles = installed((0, cap), (1, make_profile(profile_id=2, periods=((0, 3000),))))
    assert periods_of(composite(profiles)) == [(0, 3000.0)]  # the connector's own is lower


def test_merging_takes_the_minimum_in_each_interval():
    """s3.13.3: "calculated by taking the minimum value for each time interval", with intervals
    that need not line up between purposes."""
    cap = make_profile(
        profile_id=1, purpose="ChargePointMaxProfile", periods=((0, 8000), (900, 2000)),
        start="2026-01-05T08:00:00Z",
    )
    tx = make_profile(
        profile_id=2, periods=((0, 5000), (600, 9000), (1500, 1000)), start="2026-01-05T08:00:00Z",
    )
    profiles = installed((0, cap), (1, tx))
    # 0-600 min(8000,5000)=5000; 600-900 min(8000,9000)=8000; 900-1500 min(2000,9000)=2000;
    # 1500-1800 min(2000,1000)=1000.
    assert periods_of(composite(profiles, duration=1800)) == [
        (0, 5000.0), (600, 8000.0), (900, 2000.0), (1500, 1000.0),
    ]


def test_a_relative_profile_counts_from_the_start_of_the_transaction():
    profiles = installed((1, make_profile(kind="Relative", periods=((0, 9000), (3600, 3000)))))
    started = T0 - timedelta(minutes=30)  # transaction began at 07:30
    schedule = composite(profiles, duration=7200, tx_starts={1: started})
    # The drop happens 3600 s after 07:30, i.e. 08:30 -- 1800 s after T0.
    assert periods_of(schedule) == [(0, 9000.0), (1800, 3000.0)]


def test_a_relative_profile_with_no_transaction_counts_from_the_moment_of_planning():
    profiles = installed((1, make_profile(kind="Relative", periods=((0, 9000), (600, 3000)))))
    assert periods_of(composite(profiles)) == [(0, 9000.0), (600, 3000.0)]


def test_a_relative_charge_point_max_profile_counts_from_when_it_was_installed():
    cap = make_profile(
        profile_id=1, purpose="ChargePointMaxProfile", kind="Relative",
        periods=((0, 4000), (1000, 8000)),
    )
    profiles = ChargingProfileSet()
    profiles.install(0, cap, T0 - timedelta(seconds=400))
    assert periods_of(composite(profiles, duration=3600)) == [(0, 4000.0), (600, 8000.0)]


def test_a_weekly_recurrence_restarts_every_seven_days():
    weekly = make_profile(
        kind="Recurring", recurrency="Weekly", start="2026-01-01T00:00:00Z",  # a Thursday
        periods=((0, 12000), (86400, 3000)), duration=2 * 86400,
    )
    profiles = installed((1, weekly))
    # Monday 2026-01-05 is 4 days into the cycle: past the 2-day schedule, so nothing applies. The
    # next cycle starts Thursday 2026-01-08 00:00 -- 2 d 18 h = 237600 s after Monday 06:00 -- and
    # its second period (a day in) starts 86400 s later.
    start = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    schedule = composite(profiles, duration=4 * 86400, start=start)
    assert periods_of(schedule) == [(0, HARDWARE_W), (237600, 12000.0), (324000, 3000.0)]


def test_a_recurring_duration_shorter_than_the_cycle_falls_back_each_day():
    daily = make_profile(
        kind="Recurring", recurrency="Daily", start=ANCHOR, periods=((0, 5000),), duration=3600,
    )
    profiles = installed((1, daily))
    start = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
    schedule = composite(profiles, duration=7200, start=start)
    # 23:30-00:00 falls outside the hour after midnight; 00:00-01:00 is inside; then out again.
    assert periods_of(schedule) == [(0, HARDWARE_W), (1800, 5000.0), (5400, HARDWARE_W)]


def test_neighbouring_periods_with_equal_limits_are_merged():
    profiles = installed((1, make_profile(periods=((0, 5000), (600, 5000), (1200, 5000)))))
    assert periods_of(composite(profiles, duration=3600)) == [(0, 5000.0)]


def test_a_zero_limit_is_a_zero_limit():
    profiles = installed(
        (1, make_profile(periods=((0, 0), (600, 8000)), start="2026-01-05T08:00:00Z"))
    )
    assert periods_of(composite(profiles, duration=1200)) == [(0, 0.0), (600, 8000.0)]
    assert profiles.limit_at(1, T0, connectors=[1]).watts == 0


def test_connector_zero_is_the_whole_chargers_expected_draw():
    profiles = installed((0, make_profile(profile_id=1, periods=((0, 6000),))))
    # Two connectors at 6 kW each would be 12 kW ...
    assert periods_of(composite(profiles, connector_id=0, connectors=[1, 2])) == [(0, 12000.0)]
    # ... but a ChargePointMaxProfile of 9 kW holds the total down.
    profiles.install(
        0, make_profile(profile_id=2, purpose="ChargePointMaxProfile", periods=((0, 9000),)), T0
    )
    assert periods_of(composite(profiles, connector_id=0, connectors=[1, 2])) == [(0, 9000.0)]


def test_connector_zero_with_nothing_installed_is_every_connectors_hardware_limit():
    schedule = composite(ChargingProfileSet(), connector_id=0, connectors=[1, 2])
    assert periods_of(schedule) == [(0, 2 * HARDWARE_W)]


@pytest.mark.parametrize("duration", [0, -5, MAX_COMPOSITE_SECONDS + 1])
def test_composite_refuses_silly_durations(duration):
    with pytest.raises(ValueError, match="duration"):
        composite(ChargingProfileSet(), duration=duration)


def test_the_wire_form_of_a_composite_schedule():
    profiles = installed((1, make_profile(periods=((0, 5000),))))
    wire = composite(profiles, duration=600, unit=Unit.watts).to_wire()
    assert wire == {
        "duration": 600,
        "start_schedule": T0.isoformat(),
        "charging_rate_unit": "W",
        "charging_schedule_period": [{"start_period": 0, "limit": 5000.0, "number_phases": 3}],
    }


def test_next_change_after_finds_the_next_boundary():
    profiles = installed(
        (
            1,
            make_profile(
                periods=((0, 5000), (900, 1000)), start="2026-01-05T08:00:00Z", duration=1800
            ),
        )
    )
    assert profiles.next_change_after(T0, connectors=[1]) == T0 + timedelta(seconds=900)
    assert profiles.next_change_after(T0 + timedelta(seconds=900), connectors=[1]) == (
        T0 + timedelta(seconds=1800)
    )
    assert profiles.next_change_after(T0 + timedelta(seconds=1800), connectors=[1]) is None
    assert ChargingProfileSet().next_change_after(T0) is None
