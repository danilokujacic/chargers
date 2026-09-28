"""Charging profiles: validation, and the composite-schedule calculation (OCPP 1.6 s3.13,
s7.8-s7.14).

Terminology, for readers new to EV charging: a *charging profile* tells a charger how much power
(watts) or current (amps) it may deliver over time -- "at most 16 A, but nothing between 08:00 and
20:00". It wraps a *charging schedule*, a list of periods each with a limit. Several profiles can be
installed at once; the charger works out which one applies right now, and the *composite schedule*
is that answer laid out over a stretch of time.

This module holds the parts that are pure functions of data, with no I/O:

- parse_profile(): validate a profile (camelCase as in the spec, or snake_case as the ocpp
  library hands it over) into the snake_case dict the ocpp library sends.
- ChargingProfileSet: what a *charger* keeps -- installation with the s3.13.2 replacement rules,
  clearing, and the composite calculation. main.py (the Central System) does not compute
  composites itself: it asks the charger with GetCompositeSchedule, because the charger is the
  authority on its own local limits. This class exists because the simulator and the test
  doubles need a real, spec-following charger to talk to, and because it is importable by tests
  (simulate_charge_point.py is not: its last line runs main() at import time).

Time model, per s3.13 and s7.13: an *Absolute* profile counts from its startSchedule; a *Recurring*
one restarts from startSchedule every day or week; a *Relative* one counts from a
"situation-specific start point (such as the start of a Transaction)" -- here the start of the
transaction on that connector, or, for a ChargePointMaxProfile (which belongs to no transaction),
the moment the profile was installed.
"""

import math
from dataclasses import dataclass
from datetime import timedelta

from ocpp.charge_point import camel_to_snake_case
from ocpp.v16.enums import (
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingRateUnitType,
    RecurrencyKind,
)
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# OCPP 1.6 s7.12: watts and amps are related by "the set voltage for the area (hence, 230 or 110
# volt)" and the number of phases, "3 SHALL be assumed" when numberPhases is absent.
VOLTAGE = 230.0
DEFAULT_PHASES = 3

# A composite schedule longer than this is refused rather than computed: the answer would be a
# huge list nobody can use, and a runaway duration would tie up the charger.
MAX_COMPOSITE_SECONDS = 14 * 24 * 3600

RECURRENCE_SECONDS = {
    RecurrencyKind.daily: 24 * 3600,
    RecurrencyKind.weekly: 7 * 24 * 3600,
}

CPS_MAX = ChargingProfilePurposeType.charge_point_max_profile
TX_DEFAULT = ChargingProfilePurposeType.tx_default_profile
TX = ChargingProfilePurposeType.tx_profile


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SchedulePeriod(_Strict):
    start_period: int = Field(ge=0)
    limit: float = Field(ge=0)
    number_phases: int | None = Field(default=None, ge=1, le=3)

    @field_validator("limit")
    @classmethod
    def _one_decimal(cls, value):
        # s7.14: limit "accepts at most one digit fraction (e.g. 8.1)".
        if abs(value * 10 - round(value * 10)) > 1e-9:
            raise ValueError("limit accepts at most one digit fraction (e.g. 8.1)")
        return value


class ChargingSchedule(_Strict):
    duration: int | None = Field(default=None, ge=0)
    start_schedule: AwareDatetime | None = None
    charging_rate_unit: ChargingRateUnitType
    charging_schedule_period: list[SchedulePeriod] = Field(min_length=1)
    min_charging_rate: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _periods_in_order(self):
        periods = self.charging_schedule_period
        # s7.13: "The startSchedule of the first ChargingSchedulePeriod SHALL always be 0."
        if periods[0].start_period != 0:
            raise ValueError("the first chargingSchedulePeriod must have startPeriod 0")
        starts = [period.start_period for period in periods]
        if any(later <= earlier for earlier, later in zip(starts, starts[1:])):
            raise ValueError("startPeriod values must be strictly increasing")
        return self


class ChargingProfile(_Strict):
    charging_profile_id: int
    transaction_id: int | None = None
    stack_level: int = Field(ge=0)
    charging_profile_purpose: ChargingProfilePurposeType
    charging_profile_kind: ChargingProfileKindType
    recurrency_kind: RecurrencyKind | None = None
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    charging_schedule: ChargingSchedule

    @model_validator(mode="after")
    def _consistent(self):
        kind = self.charging_profile_kind
        schedule = self.charging_schedule
        if kind == ChargingProfileKindType.recurring and self.recurrency_kind is None:
            raise ValueError("a Recurring profile needs recurrencyKind (Daily or Weekly)")
        if kind != ChargingProfileKindType.recurring and self.recurrency_kind is not None:
            raise ValueError("recurrencyKind only applies to a Recurring profile")
        if kind != ChargingProfileKindType.relative and schedule.start_schedule is None:
            raise ValueError(f"a {kind.value} profile needs chargingSchedule.startSchedule")
        # s7.8: transactionId "is only valid if ChargingProfilePurpose is set to TxProfile".
        if self.transaction_id is not None and self.charging_profile_purpose != TX:
            raise ValueError("transactionId is only valid for a TxProfile")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("validTo must be after validFrom")
        return self

    @property
    def purpose(self):
        return self.charging_profile_purpose

    @property
    def kind(self):
        return self.charging_profile_kind

    def to_wire(self):
        """The snake_case dict the ocpp library sends (it converts to camelCase itself)."""
        return self.model_dump(mode="json", exclude_none=True)


def parse_profile(data):
    """Validate a charging profile and return it as a ChargingProfile.

    Accepts camelCase (as printed in the spec, so an operator can paste an example) or
    snake_case (as the ocpp library delivers it to a charger's handler). Raises ValueError --
    pydantic's ValidationError is one -- with a message naming what is wrong.
    """
    if isinstance(data, ChargingProfile):
        return data
    if not isinstance(data, dict):
        raise ValueError("a charging profile must be a JSON object")
    try:
        return ChargingProfile.model_validate(camel_to_snake_case(data))
    except ValidationError as exc:
        # pydantic's own message is a multi-line dump; an operator wants one line per problem.
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: "
            f"{error['msg'].removeprefix('Value error, ')}".lstrip(": ")
            for error in exc.errors()
        ]
        raise ValueError("invalid charging profile: " + "; ".join(problems)) from None


def purpose_connector_problem(profile, connector_id):
    """Why `profile` cannot go on `connector_id` per s3.13.1, or None if it can.

    ChargePointMaxProfile "can only be set at Charge Point ConnectorId 0"; TxProfile "SHALL only
    be set at Charge Point ConnectorId >0". TxDefaultProfile is valid on either: 0 means every
    connector.
    """
    purpose = profile.purpose
    if purpose == CPS_MAX and connector_id != 0:
        return "a ChargePointMaxProfile can only be set on connector 0"
    if purpose == TX and connector_id == 0:
        return "a TxProfile can only be set on a connector above 0"
    return None


def installation_problem(
    profile, connector_id, *, known_connectors, max_stack_level, max_periods, allowed_units,
    open_transaction_id, installed_after, max_installed, transaction_pending=False,
):
    """Why a *charger* must refuse this profile (a Rejected conf), or None if it can install it.

    Covers what only the charger knows: which connectors exist, whether a transaction is running
    (s3.13.1: "If there is no transaction active on the connector specified in a charging
    profile of type TxProfile, then the Charge Point SHALL discard it and return an error
    status"), and its own advertised limits (the s3.13.5 configuration keys
    ChargeProfileMaxStackLevel, ChargingScheduleMaxPeriods, ChargingScheduleAllowedChargingRateUnit,
    MaxChargingProfilesInstalled, checked against `installed_after`: how many profiles would be
    installed once this one is, replacements accounted for). `transaction_pending` is for the
    profile that arrives inside a RemoteStartTransaction (s5.16.2): its transaction does not exist
    yet, so the "is one active" check does not apply.
    """
    problem = purpose_connector_problem(profile, connector_id)
    if problem:
        return problem
    if connector_id != 0 and connector_id not in known_connectors:
        return f"unknown connector {connector_id}"
    if profile.stack_level > max_stack_level:
        return f"stackLevel {profile.stack_level} is above ChargeProfileMaxStackLevel"
    schedule = profile.charging_schedule
    if len(schedule.charging_schedule_period) > max_periods:
        return "too many chargingSchedulePeriod entries (ChargingScheduleMaxPeriods)"
    if schedule.charging_rate_unit not in allowed_units:
        return f"chargingRateUnit {schedule.charging_rate_unit.value} is not supported"
    if profile.purpose == TX and not transaction_pending:
        if open_transaction_id is None:
            return "no transaction is active on this connector"
        if profile.transaction_id is not None and profile.transaction_id != open_transaction_id:
            return "transactionId does not match the transaction active on this connector"
    if installed_after > max_installed:
        return "too many charging profiles installed (MaxChargingProfilesInstalled)"
    return None


# --------------------------------------------------------------------------------------------
# Limits, and evaluating one profile at one instant
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Limit:
    """A limit normalised to watts, remembering how many phases it was expressed over, so it can
    be converted back to amps the way the profile itself would have."""

    watts: float
    phases: int = DEFAULT_PHASES


def to_limit(value, unit, phases):
    phases = phases or DEFAULT_PHASES
    watts = value if unit == ChargingRateUnitType.watts else value * VOLTAGE * phases
    return Limit(watts, phases)


def from_limit(limit, unit):
    """This limit as a plain number in `unit`, rounded to the one decimal the spec allows."""
    if unit == ChargingRateUnitType.watts:
        return round(limit.watts, 1)
    return round(limit.watts / (VOLTAGE * limit.phases), 1)


# What a charger can deliver with no profile at all: 32 A on three phases. A composite schedule
# always ends up with a number in it, and this is where "unconstrained" turns into one.
DEFAULT_HARDWARE_LIMIT = to_limit(32, ChargingRateUnitType.amps, 3)


def _schedule_length(profile):
    """How long one run of this profile's schedule covers, in seconds; None = indefinitely."""
    schedule = profile.charging_schedule
    duration = schedule.duration
    if profile.kind == ChargingProfileKindType.recurring:
        length = RECURRENCE_SECONDS[profile.recurrency_kind]
        # s5.16.4: periods or duration longer than the recurrence period do not run past it.
        return length if duration is None else min(duration, length)
    return duration


def _reference(profile, relative_start):
    if profile.kind == ChargingProfileKindType.relative:
        return relative_start
    return profile.charging_schedule.start_schedule


def _offset(profile, t, relative_start):
    """Seconds into this profile's schedule at time `t`, or None if the schedule does not cover
    `t` (before it starts, past its duration, or a Relative profile with no known start)."""
    reference = _reference(profile, relative_start)
    if reference is None:
        return None
    elapsed = (t - reference).total_seconds()
    if elapsed < 0:
        return None
    if profile.kind == ChargingProfileKindType.recurring:
        elapsed %= RECURRENCE_SECONDS[profile.recurrency_kind]
    length = _schedule_length(profile)
    if length is not None and elapsed >= length:
        return None
    return elapsed


def profile_limit_at(profile, t, relative_start=None):
    """The Limit this one profile imposes at time `t`, or None when it imposes nothing then
    (not yet valid, expired, or its schedule does not cover `t`).

    s5.16.4: "If duration is longer than the chargingSchedulePeriod, the Charge Point SHALL keep
    the value of the last chargingSchedulePeriod until duration has ended", and periods that run
    past the duration are simply never reached -- both fall out of picking the last period whose
    start is at or before the offset and refusing offsets past the duration.
    """
    if profile.valid_from is not None and t < profile.valid_from:
        return None
    if profile.valid_to is not None and t >= profile.valid_to:
        return None
    offset = _offset(profile, t, relative_start)
    if offset is None:
        return None
    schedule = profile.charging_schedule
    current = schedule.charging_schedule_period[0]
    for period in schedule.charging_schedule_period:
        if period.start_period <= offset:
            current = period
    return to_limit(current.limit, schedule.charging_rate_unit, current.number_phases)


def _profile_breakpoints(profile, lo, hi, relative_start):
    """Every instant strictly between `lo` and `hi` at which this profile's limit could change."""
    points = set()
    for moment in (profile.valid_from, profile.valid_to):
        if moment is not None:
            points.add(moment)
    reference = _reference(profile, relative_start)
    if reference is None:
        return {p for p in points if lo < p < hi}
    starts = [p.start_period for p in profile.charging_schedule.charging_schedule_period]
    length = _schedule_length(profile)
    if profile.kind == ChargingProfileKindType.recurring:
        cycle = RECURRENCE_SECONDS[profile.recurrency_kind]
        first = max(0, math.floor((lo - reference).total_seconds() / cycle))
        last = math.floor((hi - reference).total_seconds() / cycle)
        for k in range(first, last + 1):
            base = reference + timedelta(seconds=k * cycle)
            points.add(base)
            points.update(base + timedelta(seconds=s) for s in starts)
            points.add(base + timedelta(seconds=length))
    else:
        points.add(reference)
        points.update(reference + timedelta(seconds=s) for s in starts)
        if length is not None:
            points.add(reference + timedelta(seconds=length))
    return {p for p in points if lo < p < hi}


# --------------------------------------------------------------------------------------------
# What a charger holds
# --------------------------------------------------------------------------------------------


@dataclass
class Installed:
    connector_id: int
    profile: ChargingProfile
    received_at: object  # datetime


@dataclass(frozen=True)
class CompositeSchedule:
    start: object  # datetime the periods are relative to
    duration: int
    unit: ChargingRateUnitType
    periods: list

    def to_wire(self):
        """The `chargingSchedule` field of GetCompositeSchedule.conf, snake_case."""
        return {
            "duration": self.duration,
            "start_schedule": self.start.isoformat(),
            "charging_rate_unit": self.unit.value,
            "charging_schedule_period": self.periods,
        }


class ChargingProfileSet:
    """The charging profiles installed on one charger, and the rules for combining them.

    `hardware` is what the charger can deliver per connector with no profile at all.
    """

    def __init__(self, hardware=DEFAULT_HARDWARE_LIMIT):
        self.hardware = hardware
        self._entries = []

    def __len__(self):
        return len(self._entries)

    def entries(self, connector_id=None):
        return [
            e for e in self._entries if connector_id is None or e.connector_id == connector_id
        ]

    def _conflicts(self, connector_id, profile):
        """Entries `profile` replaces when installed (s3.13.2, s5.16.3): the same
        chargingProfileId, or the same stackLevel and purpose on the same connector.

        Same *connector* because s3.13.1 has a TxDefaultProfile set on connector 0 (every
        connector) and one set on connector N coexist, the latter overriding it for N only.
        """
        return [
            e for e in self._entries
            if e.profile.charging_profile_id == profile.charging_profile_id
            or (
                e.connector_id == connector_id
                and e.profile.purpose == profile.purpose
                and e.profile.stack_level == profile.stack_level
            )
        ]

    def count_after_install(self, connector_id, profile):
        """How many profiles would be installed once `profile` is (replacements accounted for);
        compare with MaxChargingProfilesInstalled."""
        return len(self._entries) - len(self._conflicts(connector_id, profile)) + 1

    def install(self, connector_id, profile, received_at):
        """Install a profile, replacing whatever it conflicts with. Returns the replaced ones."""
        profile = parse_profile(profile)
        replaced = self._conflicts(connector_id, profile)
        self._entries = [e for e in self._entries if e not in replaced]
        self._entries.append(Installed(connector_id, profile, received_at))
        return [e.profile for e in replaced]

    def clear(self, profile_id=None, connector_id=None, purpose=None, stack_level=None):
        """Remove every profile matching *all* the criteria given (none given = everything).
        Returns how many were removed; zero is what a charger answers Unknown to."""
        def matches(e):
            return (
                (profile_id is None or e.profile.charging_profile_id == profile_id)
                and (connector_id is None or e.connector_id == connector_id)
                and (purpose is None or e.profile.purpose == purpose)
                and (stack_level is None or e.profile.stack_level == stack_level)
            )

        kept = [e for e in self._entries if not matches(e)]
        removed = len(self._entries) - len(kept)
        self._entries = kept
        return removed

    def drop_tx_profiles(self, connector_id):
        """s3.13.1: a TxProfile "SHALL cease to be valid when the transaction terminates"."""
        return self.clear(connector_id=connector_id, purpose=TX)

    # --- evaluation -------------------------------------------------------------------------

    def _relative_start(self, entry, connector_id, tx_starts, plan_start):
        if entry.profile.purpose == CPS_MAX:
            return entry.received_at
        # A transaction-related profile counts from the start of the transaction on the connector
        # it is being evaluated for; with none running, as if one started at the moment of
        # planning (there is nothing more honest to say about a transaction that has not begun).
        return tx_starts.get(connector_id, plan_start)

    def _prevailing(self, entries, t, connector_id, tx_starts, plan_start):
        """s3.13.2: "the prevailing charging profile SHALL be the charging profile with the
        highest stackLevel among the profiles that are valid at that point in time". A higher one
        whose schedule does not cover `t` (duration over) falls through to the next."""
        for entry in sorted(entries, key=lambda e: e.profile.stack_level, reverse=True):
            limit = profile_limit_at(
                entry.profile, t, self._relative_start(entry, connector_id, tx_starts, plan_start)
            )
            if limit is not None:
                return limit
        return None

    def _of(self, purpose, connector_id):
        return [
            e for e in self._entries
            if e.profile.purpose == purpose and e.connector_id == connector_id
        ]

    def _tx_limit(self, connector_id, t, tx_starts, plan_start):
        """The TxProfile if it says anything now, else the TxDefaultProfile -- the connector's
        own before the connector-0 one that applies to all (s3.13.1)."""
        for group in (
            self._of(TX, connector_id),
            self._of(TX_DEFAULT, connector_id),
            self._of(TX_DEFAULT, 0),
        ):
            limit = self._prevailing(group, t, connector_id, tx_starts, plan_start)
            if limit is not None:
                return limit
        return None

    def _cap(self, t, tx_starts, plan_start):
        return self._prevailing(self._of(CPS_MAX, 0), t, 0, tx_starts, plan_start)

    def _connector_uncapped(self, connector_id, t, tx_starts, plan_start):
        limit = self._tx_limit(connector_id, t, tx_starts, plan_start)
        if limit is None:
            return self.hardware
        return min(limit, self.hardware, key=lambda x: x.watts)

    def limit_at(self, connector_id, t, tx_starts=None, connectors=(), plan_start=None):
        """The Limit governing `connector_id` at `t`, combining every purpose (s3.13.3: "taking
        the minimum value for each time interval"). Connector 0 is the whole charger: the most
        it could draw, the sum of its connectors' limits held down by the ChargePointMaxProfile.
        """
        tx_starts = tx_starts or {}
        plan_start = plan_start or t
        cap = self._cap(t, tx_starts, plan_start)
        if connector_id == 0:
            watts = sum(
                self._connector_uncapped(c, t, tx_starts, plan_start).watts for c in connectors
            ) or self.hardware.watts
            total = Limit(watts, DEFAULT_PHASES)
            return total if cap is None else min(total, cap, key=lambda x: x.watts)
        limit = self._connector_uncapped(connector_id, t, tx_starts, plan_start)
        return limit if cap is None else min(limit, cap, key=lambda x: x.watts)

    def _targets(self, connector_id, connectors):
        """(entry, connector it is evaluated for) pairs that can affect `connector_id`'s limit;
        connector 0, the whole charger, is affected by every connector's profiles."""
        evaluated = list(connectors) if connector_id == 0 else [connector_id]
        pairs = []
        for entry in self._entries:
            purpose = entry.profile.purpose
            if purpose == CPS_MAX:
                pairs.append((entry, 0))
            elif purpose == TX:
                if entry.connector_id in evaluated:
                    pairs.append((entry, entry.connector_id))
            else:
                pairs.extend((entry, c) for c in evaluated if entry.connector_id in (0, c))
        return pairs

    def _breakpoints(self, connector_id, lo, hi, tx_starts, connectors, plan_start):
        points = set()
        for entry, c in self._targets(connector_id, connectors):
            points |= _profile_breakpoints(
                entry.profile, lo, hi, self._relative_start(entry, c, tx_starts, plan_start)
            )
        return points

    def composite(
        self, connector_id, start, duration, unit, *, tx_starts=None, connectors=()
    ):
        """The composite schedule for `connector_id` from `start` for `duration` seconds
        (s5.7, GetCompositeSchedule): every installed profile merged, expressed in `unit`, with
        neighbouring periods of equal limit merged into one.

        Raises ValueError for a duration that is not positive or exceeds MAX_COMPOSITE_SECONDS.
        """
        if duration <= 0 or duration > MAX_COMPOSITE_SECONDS:
            raise ValueError(f"duration must be between 1 and {MAX_COMPOSITE_SECONDS} seconds")
        tx_starts = tx_starts or {}
        end = start + timedelta(seconds=duration)
        points = sorted(
            {start}
            | self._breakpoints(connector_id, start, end, tx_starts, list(connectors), start)
        )
        periods = []
        for point in points:
            limit = self.limit_at(
                connector_id, point, tx_starts, connectors, plan_start=start
            )
            entry = {
                "start_period": round((point - start).total_seconds()),
                "limit": from_limit(limit, unit),
                "number_phases": limit.phases,
            }
            if periods and periods[-1]["limit"] == entry["limit"] and (
                periods[-1]["number_phases"] == entry["number_phases"]
            ):
                continue
            periods.append(entry)
        return CompositeSchedule(start, duration, unit, periods)

    def next_change_after(self, t, tx_starts=None, connectors=()):
        """The next instant after `t` at which any connector's limit could change, looking a week
        and a day ahead (the longest recurrence); None when nothing is scheduled to change."""
        tx_starts = tx_starts or {}
        horizon = t + timedelta(seconds=RECURRENCE_SECONDS[RecurrencyKind.weekly] + 86400)
        points = set()
        for c in (0, *connectors):
            points |= self._breakpoints(c, t, horizon, tx_starts, list(connectors), t)
        return min(points) if points else None
