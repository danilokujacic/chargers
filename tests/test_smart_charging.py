"""Tests for Smart Charging (instructions/06-remaining-flows.md section I; OCPP 1.6 s3.13, s5.5,
s5.7, s5.16): the Central System limits how much power or current a charger may deliver, checks
what it has set, and asks the charger what limits actually apply.

Everything is real -- MongoDB, the Central System in-process, a real OCPP-J WebSocket -- apart from
the charger, a test double (SmartChargingChargePoint) that behaves the way s3.13 says a charger
must: it installs, replaces and clears profiles by the s3.13.2 rules, refuses what it must
refuse, answers GetCompositeSchedule, suspends a transaction whose limit drops to zero and
resumes it when the limit lifts, and drops a TxProfile when its transaction ends. The maths it
relies on is tested on its own, against hand-worked numbers, in test_charging_profiles.py.
"""

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pytest
import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearChargingProfileStatus,
    GetCompositeScheduleStatus,
    RemoteStartStopStatus,
)
from websockets.typing import Subprotocol

import main
from charging_profiles import (
    ChargingProfileSet,
    installation_problem,
    parse_profile,
)
from commissioning import PendingChargerError
from models import ChargePoint, ConnectorStatus, InstalledChargingProfile, Transaction
from ocpp_client_auth import basic_auth_header

Purpose = ChargingProfilePurposeType


def profile(
    profile_id=1, purpose="TxDefaultProfile", stack=0, limit=11000.0, unit="W", periods=None,
    kind="Relative", **extra,
):
    """A ChargingProfile, camelCase as an operator would write it. Relative by default: counts
    from the start of the transaction, so a test never depends on the wall clock."""
    body = {
        "chargingProfileId": profile_id,
        "stackLevel": stack,
        "chargingProfilePurpose": purpose,
        "chargingProfileKind": kind,
        "chargingSchedule": {
            "chargingRateUnit": unit,
            "chargingSchedulePeriod": periods or [{"startPeriod": 0, "limit": limit}],
        },
    }
    body.update(extra)
    return body


class SmartChargingChargePoint(cp):
    """A charger with two connectors that implements Smart Charging per OCPP 1.6 s3.13."""

    def __init__(self, id, connection, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.profiles = ChargingProfileSet()
        self.connectors = {1, 2}
        self.open_transactions = {}
        self.transaction_started_at = {}
        self.suspended_by_profile = set()
        self.force_profile_status = None
        self.received = []
        self.limits_applied = asyncio.Event()
        self.remote_started = asyncio.Event()

    def received_actions(self):
        return [entry[0] for entry in self.received]

    # --- OCPP handlers ---------------------------------------------------------------------

    @on("SetChargingProfile")
    def on_set_charging_profile(self, connector_id, cs_charging_profiles, **kwargs):
        self.received.append(("SetChargingProfile", connector_id, cs_charging_profiles))
        if self.force_profile_status is not None:
            return call_result.SetChargingProfile(status=self.force_profile_status)
        try:
            parsed = parse_profile(cs_charging_profiles)
        except ValueError:
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        problem = installation_problem(
            parsed,
            connector_id,
            known_connectors=self.connectors,
            max_stack_level=5,
            max_periods=10,
            allowed_units={ChargingRateUnitType.amps, ChargingRateUnitType.watts},
            open_transaction_id=self.open_transactions.get(connector_id),
            installed_after=self.profiles.count_after_install(connector_id, parsed),
            max_installed=10,
        )
        if problem:
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        self.profiles.install(connector_id, parsed, datetime.now(UTC))
        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    @after("SetChargingProfile")
    async def after_set_charging_profile(self, **kwargs):
        await self.apply_limits()

    @on("ClearChargingProfile")
    def on_clear_charging_profile(self, **kwargs):
        self.received.append(("ClearChargingProfile", kwargs))
        removed = self.profiles.clear(
            profile_id=kwargs.get("id"),
            connector_id=kwargs.get("connector_id"),
            purpose=kwargs.get("charging_profile_purpose"),
            stack_level=kwargs.get("stack_level"),
        )
        return call_result.ClearChargingProfile(
            status=ClearChargingProfileStatus.accepted if removed
            else ClearChargingProfileStatus.unknown
        )

    @after("ClearChargingProfile")
    async def after_clear_charging_profile(self, **kwargs):
        await self.apply_limits()

    @on("GetCompositeSchedule")
    def on_get_composite_schedule(self, connector_id, duration, charging_rate_unit=None, **kwargs):
        self.received.append(("GetCompositeSchedule", connector_id, duration, charging_rate_unit))
        if connector_id != 0 and connector_id not in self.connectors:
            return call_result.GetCompositeSchedule(status=GetCompositeScheduleStatus.rejected)
        start = datetime.now(UTC)
        schedule = self.profiles.composite(
            connector_id, start, duration,
            ChargingRateUnitType(charging_rate_unit or ChargingRateUnitType.watts),
            tx_starts=self.transaction_started_at, connectors=sorted(self.connectors),
        )
        return call_result.GetCompositeSchedule(
            status=GetCompositeScheduleStatus.accepted,
            connector_id=connector_id,
            schedule_start=start.isoformat(),
            charging_schedule=schedule.to_wire(),
        )

    @on("RemoteStartTransaction")
    def on_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        self.received.append(("RemoteStartTransaction", id_tag, connector_id, kwargs))
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @after("RemoteStartTransaction")
    async def after_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        connector_id = connector_id or 1
        transaction_id = await self.start_transaction(connector_id, id_tag)
        if kwargs.get("charging_profile"):
            self.profiles.install(
                connector_id,
                parse_profile(kwargs["charging_profile"]).model_copy(
                    update={"transaction_id": transaction_id}
                ),
                datetime.now(UTC),
            )
        self.remote_started.set()

    # --- what the charger itself does -------------------------------------------------------

    async def status(self, connector_id, status):
        await self.call(
            call.StatusNotification(
                connector_id=connector_id, error_code=ChargePointErrorCode.no_error,
                status=status,
            ),
            suppress=False,
        )

    async def start_transaction(self, connector_id, id_tag):
        await self.status(connector_id, ChargePointStatus.preparing)
        started = await self.call(
            call.StartTransaction(
                connector_id=connector_id, id_tag=id_tag, meter_start=0,
                timestamp=datetime.now(UTC).isoformat(),
            ),
            suppress=False,
        )
        self.open_transactions[connector_id] = started.transaction_id
        self.transaction_started_at[connector_id] = datetime.now(UTC)
        await self.status(connector_id, ChargePointStatus.charging)
        await self.apply_limits()
        return started.transaction_id

    async def stop_transaction(self, connector_id):
        transaction_id = self.open_transactions.pop(connector_id)
        await self.call(
            call.StopTransaction(
                transaction_id=transaction_id, meter_stop=0,
                timestamp=datetime.now(UTC).isoformat(),
            ),
            suppress=False,
        )
        self.transaction_started_at.pop(connector_id, None)
        self.suspended_by_profile.discard(connector_id)
        # s3.13.1: a TxProfile ceases to be valid when its transaction terminates.
        self.profiles.drop_tx_profiles(connector_id)
        await self.status(connector_id, ChargePointStatus.finishing)

    async def apply_limits(self):
        """Charging -> SuspendedEVSE when the limit drops to zero (s4.9 C5), and back when it
        lifts (E3) -- but only for a connector this charger suspended itself."""
        moment = datetime.now(UTC)
        for connector_id in list(self.open_transactions):
            limit = self.profiles.limit_at(
                connector_id, moment, self.transaction_started_at, sorted(self.connectors)
            )
            if limit.watts <= 0 and connector_id not in self.suspended_by_profile:
                self.suspended_by_profile.add(connector_id)
                await self.status(connector_id, ChargePointStatus.suspended_evse)
            elif limit.watts > 0 and connector_id in self.suspended_by_profile:
                self.suspended_by_profile.discard(connector_id)
                await self.status(connector_id, ChargePointStatus.charging)
        self.limits_applied.set()


async def connect(server, identity, key):
    return websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": basic_auth_header(identity, key)},
    )


@pytest.fixture
async def charger(server, registered_charge_point):
    identity, key = registered_charge_point
    async with await connect(server, identity, key) as ws:
        client = SmartChargingChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            # See test_remote_start_stop.py: one real round trip guarantees the registry entry.
            await client.call(call.Heartbeat(), suppress=False)
            yield client
        finally:
            listener.cancel()


@pytest.fixture
async def charging(charger, accepted_id_tag):
    """The charger with a transaction open on connector 1."""
    charger.transaction_id = await charger.start_transaction(1, accepted_id_tag)
    return charger


async def records(charger, connector_id=None):
    return await main.get_installed_charging_profiles(charger.id, connector_id)


async def connector_status(identity, connector_id=1):
    record = await ConnectorStatus.find_one(
        ConnectorStatus.charge_point_identity == identity,
        ConnectorStatus.connector_id == connector_id,
    )
    return record.status


async def wait_limits_applied(charger):
    await asyncio.wait_for(charger.limits_applied.wait(), timeout=5)
    charger.limits_applied.clear()


# --------------------------------------------------------------------------------------------
# SetChargingProfile
# --------------------------------------------------------------------------------------------


async def test_accepted_profile_is_recorded_and_reaches_the_charger_intact(charger):
    response = await main.send_set_charging_profile(charger.id, 0, profile(profile_id=7, stack=2))
    assert response.status == ChargingProfileStatus.accepted

    [record] = await records(charger)
    assert (record.connector_id, record.charging_profile_id, record.stack_level) == (0, 7, 2)
    assert record.purpose == Purpose.tx_default_profile
    assert record.profile["charging_schedule"]["charging_schedule_period"] == [
        {"start_period": 0, "limit": 11000.0}
    ]
    # What the charger received, decoded from the wire, matches what was sent.
    [(_, connector_id, sent)] = charger.received
    assert connector_id == 0
    assert sent["charging_profile_id"] == 7
    assert sent["charging_profile_purpose"] == "TxDefaultProfile"
    assert sent["charging_schedule"]["charging_rate_unit"] == "W"


async def test_snake_case_profiles_are_accepted_too(charger):
    wire = parse_profile(profile(profile_id=3)).to_wire()
    response = await main.send_set_charging_profile(charger.id, 1, wire)
    assert response.status == ChargingProfileStatus.accepted


async def test_same_profile_id_replaces_the_record(charger):
    await main.send_set_charging_profile(charger.id, 1, profile(profile_id=1, limit=9000.0))
    await main.send_set_charging_profile(
        charger.id, 1, profile(profile_id=1, limit=4000.0, stack=3)
    )
    [record] = await records(charger)
    assert record.stack_level == 3
    assert record.profile["charging_schedule"]["charging_schedule_period"][0]["limit"] == 4000.0


async def test_same_connector_purpose_and_stack_level_replaces_the_record(charger):
    await main.send_set_charging_profile(charger.id, 1, profile(profile_id=1, stack=1))
    await main.send_set_charging_profile(charger.id, 1, profile(profile_id=2, stack=1))
    assert [r.charging_profile_id for r in await records(charger)] == [2]


async def test_same_stack_level_on_another_connector_or_purpose_coexists(charger, accepted_id_tag):
    await charger.start_transaction(2, accepted_id_tag)
    await main.send_set_charging_profile(charger.id, 0, profile(profile_id=1))
    await main.send_set_charging_profile(charger.id, 1, profile(profile_id=2))
    await main.send_set_charging_profile(charger.id, 2, profile(profile_id=3, purpose="TxProfile"))
    await main.send_set_charging_profile(
        charger.id, 0, profile(profile_id=4, purpose="ChargePointMaxProfile")
    )
    assert sorted(r.charging_profile_id for r in await records(charger)) == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "answer", [ChargingProfileStatus.rejected, ChargingProfileStatus.not_supported]
)
async def test_a_refusal_from_the_charger_leaves_no_record(charger, answer):
    charger.force_profile_status = answer
    response = await main.send_set_charging_profile(charger.id, 1, profile())
    assert response.status == answer
    assert await records(charger) == []


async def test_a_profile_the_charger_itself_must_refuse_is_rejected_and_not_recorded(charger):
    # stackLevel 6 is above this charger's ChargeProfileMaxStackLevel of 5.
    response = await main.send_set_charging_profile(charger.id, 1, profile(stack=6))
    assert response.status == ChargingProfileStatus.rejected
    assert await records(charger) == []


@pytest.mark.parametrize(
    "connector_id, body, message",
    [
        (1, profile(purpose="ChargePointMaxProfile"), "connector 0"),
        (0, profile(purpose="TxProfile"), "above 0"),
        (1, {"chargingProfileId": 1}, "invalid charging profile"),
        (1, profile(stack=-1), "stack_level"),
        (1, profile(periods=[{"startPeriod": 10, "limit": 5}]), "startPeriod 0"),
        (1, profile(purpose="Bogus"), "charging_profile_purpose"),
        (1, "not a profile", "JSON object"),
    ],
)
async def test_an_invalid_profile_is_refused_before_anything_is_sent(
    charger, connector_id, body, message
):
    with pytest.raises(ValueError, match=message):
        await main.send_set_charging_profile(charger.id, connector_id, body)
    assert charger.received == []
    assert await records(charger) == []


async def test_a_tx_profile_needs_an_open_transaction(charger):
    with pytest.raises(ValueError, match="no open transaction"):
        await main.send_set_charging_profile(charger.id, 1, profile(purpose="TxProfile"))
    assert charger.received == []


async def test_a_tx_profile_gets_the_open_transactions_id_filled_in(charging):
    response = await main.send_set_charging_profile(
        charging.id, 1, profile(purpose="TxProfile", limit=3000.0)
    )
    assert response.status == ChargingProfileStatus.accepted
    [(_, _, sent)] = charging.received
    assert sent["transaction_id"] == charging.transaction_id  # s5.16: CS SHALL include it
    [record] = await records(charging)
    assert record.transaction_id == charging.transaction_id


async def test_a_tx_profile_naming_the_wrong_transaction_is_refused(charging):
    with pytest.raises(ValueError, match="not the open transaction"):
        await main.send_set_charging_profile(
            charging.id, 1, profile(purpose="TxProfile", transactionId=charging.transaction_id + 99)
        )
    assert await records(charging) == []


async def test_a_tx_profile_ends_with_its_transaction_and_defaults_stay(charging):
    await main.send_set_charging_profile(charging.id, 1, profile(profile_id=1, purpose="TxProfile"))
    await main.send_set_charging_profile(charging.id, 1, profile(profile_id=2))
    assert len(await records(charging)) == 2

    await charging.stop_transaction(1)  # main.py's on_stop is what drops the record
    assert [r.charging_profile_id for r in await records(charging)] == [2]


async def test_only_the_stopped_connectors_tx_profile_is_dropped(charging, accepted_id_tag):
    await charging.start_transaction(2, accepted_id_tag)
    await main.send_set_charging_profile(charging.id, 1, profile(profile_id=1, purpose="TxProfile"))
    await main.send_set_charging_profile(charging.id, 2, profile(profile_id=2, purpose="TxProfile"))
    await charging.stop_transaction(1)
    assert [r.charging_profile_id for r in await records(charging)] == [2]


# --------------------------------------------------------------------------------------------
# The charger's own response to a limit: suspend at zero, resume when lifted
# --------------------------------------------------------------------------------------------


async def test_a_zero_limit_suspends_the_transaction_and_lifting_it_resumes(charging):
    charging.limits_applied.clear()
    await main.send_set_charging_profile(charging.id, 1, profile(limit=0.0))
    await wait_limits_applied(charging)
    # Charging -> SuspendedEVSE (s4.9 C5), reported by the charger, taken by the state machine.
    assert await connector_status(charging.id) == ChargePointStatus.suspended_evse

    await main.send_clear_charging_profile(charging.id, profile_id=1)
    await wait_limits_applied(charging)
    # SuspendedEVSE -> Charging (E3).
    assert await connector_status(charging.id) == ChargePointStatus.charging


async def test_a_nonzero_limit_does_not_disturb_a_running_transaction(charging):
    charging.limits_applied.clear()
    await main.send_set_charging_profile(charging.id, 1, profile(limit=3000.0))
    await wait_limits_applied(charging)
    assert await connector_status(charging.id) == ChargePointStatus.charging


# --------------------------------------------------------------------------------------------
# ClearChargingProfile
# --------------------------------------------------------------------------------------------


async def install_three(charger):
    await main.send_set_charging_profile(
        charger.id, 0, profile(profile_id=1, purpose="ChargePointMaxProfile")
    )
    await main.send_set_charging_profile(charger.id, 0, profile(profile_id=2, stack=0))
    await main.send_set_charging_profile(charger.id, 1, profile(profile_id=3, stack=1))


async def test_clear_by_id(charger):
    await install_three(charger)
    response = await main.send_clear_charging_profile(charger.id, profile_id=2)
    assert response.status == ClearChargingProfileStatus.accepted
    assert sorted(r.charging_profile_id for r in await records(charger)) == [1, 3]
    [(_, sent)] = [e for e in charger.received if e[0] == "ClearChargingProfile"]
    assert sent == {"id": 2}


async def test_clear_by_purpose_and_stack_level_and_connector(charger):
    await install_three(charger)
    await main.send_clear_charging_profile(charger.id, purpose="TxDefaultProfile")
    assert [r.charging_profile_id for r in await records(charger)] == [1]

    await install_three(charger)
    await main.send_clear_charging_profile(charger.id, connector_id=1)
    assert sorted(r.charging_profile_id for r in await records(charger)) == [1, 2]

    await install_three(charger)
    await main.send_clear_charging_profile(charger.id, stack_level=1)
    assert sorted(r.charging_profile_id for r in await records(charger)) == [1, 2]


async def test_clear_reads_the_criteria_together(charger):
    await install_three(charger)
    response = await main.send_clear_charging_profile(
        charger.id, connector_id=1, purpose="ChargePointMaxProfile"
    )
    assert response.status == ClearChargingProfileStatus.unknown
    assert len(await records(charger)) == 3


async def test_clear_with_no_criteria_clears_everything(charger):
    await install_three(charger)
    response = await main.send_clear_charging_profile(charger.id)
    assert response.status == ClearChargingProfileStatus.accepted
    assert await records(charger) == []


async def test_unknown_from_the_charger_drops_the_stale_record(charger):
    """The Central System thought a profile was installed; the charger says it never was. The
    charger is the authority, so the record for the same criteria goes."""
    await InstalledChargingProfile.install(charger.id, 1, parse_profile(profile(profile_id=9)))
    response = await main.send_clear_charging_profile(charger.id, profile_id=9)
    assert response.status == ClearChargingProfileStatus.unknown
    assert await records(charger) == []


async def test_clear_rejects_an_unknown_purpose_before_sending(charger):
    with pytest.raises(ValueError):
        await main.send_clear_charging_profile(charger.id, purpose="Bogus")
    assert charger.received == []


# --------------------------------------------------------------------------------------------
# GetCompositeSchedule
# --------------------------------------------------------------------------------------------


async def test_composite_schedule_is_what_the_charger_computes(charger):
    await main.send_set_charging_profile(charger.id, 0, profile(profile_id=1, limit=6000.0))
    response = await main.send_get_composite_schedule(charger.id, 1, 600)
    assert response.status == GetCompositeScheduleStatus.accepted
    assert response.connector_id == 1
    assert response.schedule_start
    schedule = response.charging_schedule
    assert schedule["duration"] == 600
    assert schedule["charging_rate_unit"] == "W"
    assert [(p["start_period"], p["limit"]) for p in schedule["charging_schedule_period"]] == [
        (0, 6000.0)
    ]


async def test_composite_schedule_in_amps(charger):
    await main.send_set_charging_profile(charger.id, 0, profile(profile_id=1, limit=6900.0))
    response = await main.send_get_composite_schedule(charger.id, 1, 600, "A")
    # 6900 W / (230 V * 3 phases) = 10 A.
    assert response.charging_schedule["charging_rate_unit"] == "A"
    assert response.charging_schedule["charging_schedule_period"][0]["limit"] == 10.0


async def test_composite_schedule_for_an_unknown_connector_is_rejected(charger):
    response = await main.send_get_composite_schedule(charger.id, 9, 600)
    assert response.status == GetCompositeScheduleStatus.rejected
    assert response.charging_schedule is None


async def test_composite_schedule_for_the_whole_charger(charger):
    await main.send_set_charging_profile(
        charger.id, 0, profile(profile_id=1, purpose="ChargePointMaxProfile", limit=9000.0)
    )
    response = await main.send_get_composite_schedule(charger.id, 0, 600)
    assert response.charging_schedule["charging_schedule_period"][0]["limit"] == 9000.0


@pytest.mark.parametrize("duration, unit", [(0, None), (-1, None), (600, "kW")])
async def test_composite_schedule_bad_arguments_are_refused_before_sending(charger, duration, unit):
    with pytest.raises(ValueError):
        await main.send_get_composite_schedule(charger.id, 1, duration, unit)
    assert charger.received == []


# --------------------------------------------------------------------------------------------
# RemoteStartTransaction carrying a profile (s5.16.2)
# --------------------------------------------------------------------------------------------


async def test_remote_start_profile_reaches_the_charger_and_is_recorded_with_the_transaction(
    charger, accepted_id_tag
):
    response = await main.send_remote_start_transaction(
        charger.id, accepted_id_tag, 1, profile(purpose="TxProfile", limit=3000.0)
    )
    assert response.status == RemoteStartStopStatus.accepted
    await asyncio.wait_for(charger.remote_started.wait(), timeout=5)

    [(_, _, _, extra)] = [e for e in charger.received if e[0] == "RemoteStartTransaction"]
    assert extra["charging_profile"]["charging_profile_purpose"] == "TxProfile"
    assert "transaction_id" not in extra["charging_profile"]  # s5.16.2: SHALL NOT be set

    [record] = await records(charger)
    transaction = await Transaction.find_open(charger.id, 1)
    assert record.purpose == Purpose.tx_profile
    assert record.transaction_id == transaction.transaction_id


async def test_remote_start_without_a_profile_records_nothing(charger, accepted_id_tag):
    await main.send_remote_start_transaction(charger.id, accepted_id_tag, 1)
    await asyncio.wait_for(charger.remote_started.wait(), timeout=5)
    assert await records(charger) == []
    [(_, _, _, extra)] = [e for e in charger.received if e[0] == "RemoteStartTransaction"]
    assert "charging_profile" not in extra


@pytest.mark.parametrize(
    "body, message",
    [
        (profile(purpose="TxDefaultProfile"), "must be a TxProfile"),
        (profile(purpose="ChargePointMaxProfile"), "must be a TxProfile"),
        (profile(purpose="TxProfile", transactionId=5), "must not set transactionId"),
        ({"nonsense": True}, "invalid charging profile"),
    ],
)
async def test_remote_start_refuses_a_wrong_profile_before_sending(
    charger, accepted_id_tag, body, message
):
    with pytest.raises(ValueError, match=message):
        await main.send_remote_start_transaction(charger.id, accepted_id_tag, 1, body)
    assert charger.received == []


async def test_an_unrelated_start_does_not_pick_up_someone_elses_pending_profile(
    charger, accepted_id_tag
):
    """The profile is remembered against (charger, connector, idTag) until that start arrives."""
    main._pending_remote_start_profiles[(charger.id, 1, "SOMEONE_ELSE")] = (
        parse_profile(profile(purpose="TxProfile")), 10**12,
    )
    await charger.start_transaction(1, accepted_id_tag)
    assert await records(charger) == []


async def test_an_expired_pending_profile_is_not_recorded(charger, accepted_id_tag):
    main._pending_remote_start_profiles[(charger.id, 1, accepted_id_tag)] = (
        parse_profile(profile(purpose="TxProfile")), 0,  # a deadline long past
    )
    await charger.start_transaction(1, accepted_id_tag)
    assert await records(charger) == []


# --------------------------------------------------------------------------------------------
# Guard rails shared by all of it
# --------------------------------------------------------------------------------------------


async def test_records_are_readable_while_the_charger_is_offline(server, registered_charge_point):
    identity, key = registered_charge_point
    async with await connect(server, identity, key) as ws:
        client = SmartChargingChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await client.call(call.Heartbeat(), suppress=False)
            await main.send_set_charging_profile(identity, 0, profile(profile_id=1))
        finally:
            listener.cancel()
    # The charger has disconnected: this is the database alone -- what survives a restart.
    remaining = await main.get_installed_charging_profiles(identity)
    assert [r.charging_profile_id for r in remaining] == [1]
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_set_charging_profile(identity, 0, profile(profile_id=2))


async def test_records_are_ordered_and_can_be_filtered_by_connector(charger):
    await install_three(charger)
    everything = await records(charger)
    assert [(r.connector_id, r.purpose.value, r.stack_level) for r in everything] == [
        (0, "ChargePointMaxProfile", 0), (0, "TxDefaultProfile", 0), (1, "TxDefaultProfile", 1),
    ]
    assert [r.charging_profile_id for r in await records(charger, 1)] == [3]


async def test_all_of_it_is_forbidden_while_pending(server, charge_point_identity):
    """Like every Central-System-initiated message here except configuration and TriggerMessage."""
    _record, key = await ChargePoint.register(charge_point_identity)  # default: Pending
    async with await connect(server, charge_point_identity, key) as ws:
        client = SmartChargingChargePoint(charge_point_identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await client.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor="V"),
                suppress=False,
            )
            with pytest.raises(PendingChargerError):
                await main.send_set_charging_profile(client.id, 1, profile())
            with pytest.raises(PendingChargerError):
                await main.send_clear_charging_profile(client.id)
            with pytest.raises(PendingChargerError):
                await main.send_get_composite_schedule(client.id, 1, 600)
            assert client.received == []
        finally:
            listener.cancel()


async def test_not_connected_raises_a_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    for attempt in (
        main.send_set_charging_profile(charge_point_identity, 1, profile()),
        main.send_clear_charging_profile(charge_point_identity),
        main.send_get_composite_schedule(charge_point_identity, 1, 600),
    ):
        with pytest.raises(RuntimeError, match="not currently connected"):
            await attempt


# --------------------------------------------------------------------------------------------
# Admin API
# --------------------------------------------------------------------------------------------


async def http_get(url):
    loop = asyncio.get_event_loop()

    def fetch():
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    return await loop.run_in_executor(None, fetch)


@pytest.fixture
def admin(server, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "t")

    def call_admin(path, **params):
        query = urllib.parse.urlencode({**params, "token": "t"})
        return http_get(f"{server.replace('ws://', 'http://')}/admin/{path}?{query}")

    return call_admin


async def test_admin_set_list_composite_and_clear_end_to_end(charger, admin):
    status, body = await admin(
        "set-charging-profile", identity=charger.id, connector_id=0,
        profile=json.dumps(profile(profile_id=5, limit=8000.0)),
    )
    assert (status, body) == (200, {"status": "Accepted"})

    status, body = await admin("charging-profiles", identity=charger.id)
    assert status == 200
    [listed] = body["charging_profiles"]
    assert (listed["connector_id"], listed["charging_profile_id"], listed["purpose"]) == (
        0, 5, "TxDefaultProfile"
    )
    assert listed["profile"]["charging_schedule"]["charging_rate_unit"] == "W"

    status, body = await admin(
        "get-composite-schedule", identity=charger.id, connector_id=1, duration=300, unit="W"
    )
    assert status == 200 and body["status"] == "Accepted"
    assert body["charging_schedule"]["charging_schedule_period"][0]["limit"] == 8000.0

    status, body = await admin("clear-charging-profile", identity=charger.id, id=5)
    assert (status, body) == (200, {"status": "Accepted"})
    status, body = await admin("charging-profiles", identity=charger.id)
    assert body == {"charging_profiles": []}


async def test_admin_rejects_bad_input_with_400(charger, admin):
    status, body = await admin(
        "set-charging-profile", identity=charger.id, connector_id=1, profile="{not json"
    )
    assert status == 400
    status, body = await admin(
        "set-charging-profile", identity=charger.id, connector_id=1,
        profile=json.dumps({"chargingProfileId": 1}),
    )
    assert status == 400 and "invalid charging profile" in body["error"]
    status, _ = await admin("clear-charging-profile", identity=charger.id, purpose="Bogus")
    assert status == 400
    status, _ = await admin(
        "get-composite-schedule", identity=charger.id, connector_id=1, duration=0
    )
    assert status == 400


async def test_admin_remote_start_with_a_profile(charger, admin, accepted_id_tag):
    status, body = await admin(
        "remote-start", identity=charger.id, id_tag=accepted_id_tag, connector_id=1,
        profile=json.dumps(profile(purpose="TxProfile", limit=2500.0)),
    )
    assert (status, body) == (200, {"status": "Accepted"})
    await asyncio.wait_for(charger.remote_started.wait(), timeout=5)
    assert [r.purpose for r in await records(charger)] == [Purpose.tx_profile]
