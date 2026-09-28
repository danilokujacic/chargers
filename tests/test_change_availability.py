"""Tests for availability management: an operator takes a connector (or a whole charge point)
out of service for maintenance, or brings it back, without unplugging anything themselves.

See instructions/06-remaining-flows.md section B. Like test_remote_start_stop.py, these call
main.send_change_availability directly -- the same function main.py's admin API (see
operate.py's change-availability subcommand) calls.

ChangeAvailability.conf's status (Accepted/Rejected/Scheduled) is the CHARGER's decision, not
the Central System's, so a local test double plays that role -- the same relationship
test_remote_start_stop.py's RemoteControllableChargePoint has to RemoteStartTransaction. It is
not simulate_charge_point.py's SimulatedChargePoint reused directly, for the same reason
test_remote_start_stop.py gives: that module is a standalone entry-point script (its last line
runs main() at import time), never meant to be imported.
"""

import asyncio
from datetime import UTC, datetime

import pytest
import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ChargePointStatus,
    RemoteStartStopStatus,
)
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from connector_state_machine import ConnectorState
from models import ChargePoint, ConnectorStatus
from ocpp_client_auth import basic_auth_header

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


class AvailabilityControllableChargePoint(cp):
    """A test double implementing ChangeAvailability the way OCPP 1.6 s5.2 requires: Accepted
    applies at once, Scheduled defers until an in-progress transaction on that connector ends,
    and connectorId 0 addresses every connector this charger has actually reported on plus
    itself. Also answers RemoteStartTransaction, minimally, so a transaction can be put "in
    progress" to test the deferral.
    """

    def __init__(self, id, connection, reject=False, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.reject = reject
        self.open_transactions = {}
        self.connector_states = {}
        self.known_connectors = set()
        self.deferred_availability = {}
        self.status_history = []
        self.availability_applied = asyncio.Event()
        self.started = asyncio.Event()

    def get_connector_state(self, connector_id):
        state = self.connector_states.get(connector_id)
        if state is None:
            state = ConnectorState(connector_id)
            self.connector_states[connector_id] = state
        return state

    async def send_status(self, connector_id, status):
        if connector_id != 0:
            self.known_connectors.add(connector_id)
        self.get_connector_state(connector_id).change_to(status)
        self.status_history.append((connector_id, status))
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=status,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )

    def _targets(self, connector_id):
        return ({0} | self.known_connectors) if connector_id == 0 else {connector_id}

    def _blocked(self, connector_id, avail_type):
        if avail_type == AvailabilityType.inoperative and connector_id in self.open_transactions:
            return True
        target = (
            ChargePointStatus.unavailable
            if avail_type == AvailabilityType.inoperative
            else ChargePointStatus.available
        )
        state = self.get_connector_state(connector_id)
        return state.status != target and not state.can_change_to(target)

    async def _apply(self, connector_id, avail_type):
        self.deferred_availability.pop(connector_id, None)
        target = (
            ChargePointStatus.unavailable
            if avail_type == AvailabilityType.inoperative
            else ChargePointStatus.available
        )
        if self.get_connector_state(connector_id).status != target:
            await self.send_status(connector_id, target)

    @on(Action.change_availability)
    def on_change_availability(self, connector_id, type, **kwargs):
        if self.reject:
            return call_result.ChangeAvailability(status=AvailabilityStatus.rejected)
        avail_type = AvailabilityType(type)
        scheduled = any(self._blocked(c, avail_type) for c in self._targets(connector_id))
        status = AvailabilityStatus.scheduled if scheduled else AvailabilityStatus.accepted
        return call_result.ChangeAvailability(status=status)

    @after(Action.change_availability)
    async def after_change_availability(self, connector_id, type, **kwargs):
        if self.reject:
            return
        avail_type = AvailabilityType(type)
        for target in self._targets(connector_id):
            if self._blocked(target, avail_type):
                self.deferred_availability[target] = avail_type
            else:
                await self._apply(target, avail_type)
        self.availability_applied.set()

    @on(Action.remote_start_transaction)
    def on_remote_start(self, id_tag, connector_id=None, **kwargs):
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @after(Action.remote_start_transaction)
    async def after_remote_start(self, id_tag, connector_id=None, **kwargs):
        connector_id = connector_id or 1
        await self.send_status(connector_id, ChargePointStatus.preparing)
        start = await self.call(
            call.StartTransaction(
                connector_id=connector_id, id_tag=id_tag, meter_start=0, timestamp=iso(NOW)
            ),
            suppress=False,
        )
        self.open_transactions[connector_id] = start.transaction_id
        await self.send_status(connector_id, ChargePointStatus.charging)
        self.started.set()

    async def finish_transaction(self, connector_id, transaction_id):
        """Stand in for the charger ending the transaction that was blocking a deferred change."""
        await self.call(
            call.StopTransaction(transaction_id=transaction_id, meter_stop=0, timestamp=iso(NOW)),
            suppress=False,
        )
        await self.send_status(connector_id, ChargePointStatus.finishing)
        self.open_transactions.pop(connector_id, None)
        deferred = self.deferred_availability.get(connector_id)
        if deferred is not None:
            await self._apply(connector_id, deferred)
            self.availability_applied.set()


async def wait_until_registered(client):
    """See test_remote_start_stop.py's helper of the same name for why this is needed."""
    await client.call(call.Heartbeat(), suppress=False)


@pytest.fixture
async def availability_controllable_charge_point(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = AvailabilityControllableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def test_change_availability_accepted_applies_immediately(
    availability_controllable_charge_point,
):
    client = availability_controllable_charge_point
    response = await main.send_change_availability(client.id, 1, AvailabilityType.inoperative)
    assert response.status == AvailabilityStatus.accepted
    await asyncio.wait_for(client.availability_applied.wait(), timeout=5)
    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.unavailable
    assert status.desired_availability == AvailabilityType.inoperative
    assert status.availability_change_scheduled is False


async def test_change_availability_already_in_that_state_is_a_no_op(
    availability_controllable_charge_point,
):
    client = availability_controllable_charge_point
    # The connector starts Available/Operative -- asking for Operative again must not send a
    # StatusNotification, and must still answer Accepted (OCPP 1.6 s5.2).
    response = await main.send_change_availability(client.id, 1, AvailabilityType.operative)
    assert response.status == AvailabilityStatus.accepted
    assert client.status_history == []


async def test_change_availability_scheduled_while_transaction_in_progress(
    availability_controllable_charge_point, accepted_id_tag
):
    client = availability_controllable_charge_point
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)
    transaction_id = client.open_transactions[1]

    response = await main.send_change_availability(client.id, 1, AvailabilityType.inoperative)
    assert response.status == AvailabilityStatus.scheduled

    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.charging  # unchanged: not applied yet
    assert status.desired_availability == AvailabilityType.inoperative
    assert status.availability_change_scheduled is True

    await client.finish_transaction(1, transaction_id)
    await asyncio.wait_for(client.availability_applied.wait(), timeout=5)

    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.unavailable
    assert status.availability_change_scheduled is False


async def test_change_availability_connector_zero_targets_whole_charge_point(
    availability_controllable_charge_point, accepted_id_tag
):
    client = availability_controllable_charge_point
    # Give the charger a known connector (1) with no open transaction, so a connectorId=0
    # broadcast (OCPP 1.6 s5.2: "the Charge Point and all Connectors") has something besides
    # connector 0 itself to apply to.
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)
    await client.finish_transaction(1, client.open_transactions[1])

    response = await main.send_change_availability(client.id, 0, AvailabilityType.inoperative)
    assert response.status == AvailabilityStatus.accepted
    await asyncio.wait_for(client.availability_applied.wait(), timeout=5)

    main_controller = await ConnectorStatus.get_or_create(client.id, 0)
    connector_one = await ConnectorStatus.get_or_create(client.id, 1)
    assert main_controller.status == ChargePointStatus.unavailable
    assert main_controller.desired_availability == AvailabilityType.inoperative
    assert connector_one.status == ChargePointStatus.unavailable
    assert connector_one.desired_availability == AvailabilityType.inoperative


async def test_change_availability_rejected_by_charger_persists_nothing(
    availability_controllable_charge_point,
):
    client = availability_controllable_charge_point
    client.reject = True
    response = await main.send_change_availability(client.id, 1, AvailabilityType.inoperative)
    assert response.status == AvailabilityStatus.rejected
    assert (
        await ConnectorStatus.find_one(
            ConnectorStatus.charge_point_identity == client.id, ConnectorStatus.connector_id == 1
        )
        is None
    )


async def test_change_availability_forbidden_while_pending(server, charge_point_identity):
    _record, key = await ChargePoint.register(charge_point_identity)  # default: Pending
    header = basic_auth_header(charge_point_identity, key)
    async with websockets.connect(
        f"{server}/{charge_point_identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = cp(charge_point_identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await client.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor="V"),
                suppress=False,
            )
            with pytest.raises(PendingChargerError):
                await main.send_change_availability(
                    charge_point_identity, 1, AvailabilityType.inoperative
                )
        finally:
            listener.cancel()


async def test_change_availability_when_not_connected_raises_clear_error(
    db, charge_point_identity
):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_change_availability(charge_point_identity, 1, AvailabilityType.inoperative)


async def test_availability_persists_across_charger_reboot_with_no_memory(
    server, registered_charge_point
):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)

    # First connection: set connector 1 Inoperative, and disconnect -- as if the charger then
    # lost power.
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = AvailabilityControllableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            response = await main.send_change_availability(
                identity, 1, AvailabilityType.inoperative
            )
            assert response.status == AvailabilityStatus.accepted
            await asyncio.wait_for(client.availability_applied.wait(), timeout=5)
        finally:
            listener.cancel()

    # Second connection: a brand new double with no memory of ever being made Inoperative --
    # standing in for a real charger's fresh boot after a power cycle. Only this Central
    # System's own persisted desired_availability (OCPP 1.6 s5.2: "shall persist a reboot")
    # can put it back.
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = AvailabilityControllableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            boot = await client.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor="V"),
                suppress=False,
            )
            assert boot.status == "Accepted"
            await asyncio.wait_for(client.availability_applied.wait(), timeout=5)
        finally:
            listener.cancel()

    status = await ConnectorStatus.get_or_create(identity, 1)
    assert status.status == ChargePointStatus.unavailable
    assert status.desired_availability == AvailabilityType.inoperative
