"""Tests for reservations: a driver books a connector ahead of time (through an app, say) so it
is held for their idTag until they arrive, cancel, or take too long.

See instructions/06-remaining-flows.md section E. Like the other 06 test files, these call
main.send_reserve_now / main.send_cancel_reservation directly -- the same functions main.py's
admin API (see operate.py's reserve-now / cancel-reservation subcommands) calls -- and use a
local test double, not simulate_charge_point.py's SimulatedChargePoint, which is a standalone
entry-point script never meant to be imported.
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
    CancelReservationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    RemoteStartStopStatus,
    ReservationStatus,
)
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from connector_state_machine import ConnectorState
from models import ChargePoint, ConnectorStatus, Reservation
from ocpp_client_auth import basic_auth_header

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
# Deliberately far from "now" in either direction, regardless of when this suite actually runs --
# unlike NOW above (only ever used for StatusNotification/StartTransaction timestamps here, never
# compared against Reservation.sweep_expired's real _utcnow()).
FUTURE_EXPIRY = datetime(2099, 1, 1, tzinfo=UTC)
PAST_EXPIRY = datetime(2000, 1, 1, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


class ReservableChargePoint(cp):
    """A test double implementing ReserveNow and CancelReservation the way OCPP 1.6 s5.15/s5.3
    require: Available -> Reserved (A7) on Accepted, Reserved -> Available (G1) once cancelled,
    Occupied/Faulted/Unavailable answered from the connector's actual current status. Also
    answers RemoteStartTransaction, minimally, so a connector can be put mid-session (Occupied)
    or a reservation consumed by a real StartTransaction.
    """

    def __init__(self, id, connection, reject_reserve_now=False, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.reject_reserve_now = reject_reserve_now
        self.connector_states = {}
        self.open_transactions = {}
        self.reservations = {}  # reservation_id -> connector_id | None
        self.started = asyncio.Event()
        self.reserved = asyncio.Event()
        self.cancelled = asyncio.Event()

    def get_connector_state(self, connector_id):
        state = self.connector_states.get(connector_id)
        if state is None:
            state = ConnectorState(connector_id)
            self.connector_states[connector_id] = state
        return state

    async def send_status(self, connector_id, status):
        self.get_connector_state(connector_id).change_to(status)
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=status,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )

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

    @on(Action.reserve_now)
    def on_reserve_now(self, connector_id, expiry_date, id_tag, reservation_id, **kwargs):
        if self.reject_reserve_now:
            return call_result.ReserveNow(status=ReservationStatus.rejected)
        if connector_id == 0:
            self.reservations[reservation_id] = None
            return call_result.ReserveNow(status=ReservationStatus.accepted)
        state = self.get_connector_state(connector_id)
        if connector_id in self.open_transactions or state.status == ChargePointStatus.reserved:
            return call_result.ReserveNow(status=ReservationStatus.occupied)
        if state.status == ChargePointStatus.faulted:
            return call_result.ReserveNow(status=ReservationStatus.faulted)
        if state.status == ChargePointStatus.unavailable:
            return call_result.ReserveNow(status=ReservationStatus.unavailable)
        self.reservations[reservation_id] = connector_id
        return call_result.ReserveNow(status=ReservationStatus.accepted)

    @after(Action.reserve_now)
    async def after_reserve_now(self, connector_id, expiry_date, id_tag, reservation_id, **kwargs):
        if self.reject_reserve_now or self.reservations.get(reservation_id) is None:
            return
        await self.send_status(self.reservations[reservation_id], ChargePointStatus.reserved)
        self.reserved.set()

    @on(Action.cancel_reservation)
    def on_cancel_reservation(self, reservation_id, **kwargs):
        if reservation_id not in self.reservations:
            return call_result.CancelReservation(status=CancelReservationStatus.rejected)
        return call_result.CancelReservation(status=CancelReservationStatus.accepted)

    @after(Action.cancel_reservation)
    async def after_cancel_reservation(self, reservation_id, **kwargs):
        connector_id = self.reservations.pop(reservation_id, None)
        if connector_id is not None:
            if self.get_connector_state(connector_id).status == ChargePointStatus.reserved:
                await self.send_status(connector_id, ChargePointStatus.available)
        self.cancelled.set()


async def wait_until_registered(client):
    """See test_remote_start_stop.py's helper of the same name for why this is needed."""
    await client.call(call.Heartbeat(), suppress=False)


@pytest.fixture
async def reservable_charge_point(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = ReservableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def test_reserve_now_accepted_moves_connector_to_reserved(
    reservable_charge_point, id_tag_value
):
    client = reservable_charge_point
    reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.accepted
    await asyncio.wait_for(client.reserved.wait(), timeout=5)

    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.reserved
    reservation = await Reservation.find_active(client.id, reservation_id)
    assert reservation is not None
    assert reservation.connector_id == 1
    assert reservation.id_tag == id_tag_value


async def test_reserve_now_occupied_when_transaction_in_progress(
    reservable_charge_point, accepted_id_tag, id_tag_value
):
    client = reservable_charge_point
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)

    _reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.occupied
    assert await Reservation.find_one(Reservation.charge_point_identity == client.id) is None


async def test_reserve_now_faulted(reservable_charge_point, id_tag_value):
    client = reservable_charge_point
    await client.send_status(1, ChargePointStatus.faulted)

    _reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.faulted


async def test_reserve_now_unavailable(reservable_charge_point, id_tag_value):
    client = reservable_charge_point
    await client.send_status(1, ChargePointStatus.unavailable)

    _reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.unavailable


async def test_reserve_now_connector_zero_does_not_move_any_connector(
    reservable_charge_point, id_tag_value
):
    client = reservable_charge_point
    reservation_id, response = await main.send_reserve_now(
        client.id, 0, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.accepted

    reservation = await Reservation.find_active(client.id, reservation_id)
    assert reservation is not None
    assert reservation.connector_id == 0
    assert client.get_connector_state(1).status == ChargePointStatus.available


async def test_reserve_now_rejected_by_charger_persists_nothing(
    reservable_charge_point, id_tag_value
):
    client = reservable_charge_point
    client.reject_reserve_now = True
    reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.rejected
    assert await Reservation.find_active(client.id, reservation_id) is None


async def test_start_transaction_releases_matching_reservation(
    reservable_charge_point, id_tag_value
):
    client = reservable_charge_point
    reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.accepted
    await asyncio.wait_for(client.reserved.wait(), timeout=5)

    # A driver presenting the reserved idTag: Reserved -> Preparing (G2) is the one path OCPP
    # 1.6 s4.9 allows a reserved connector to actually start a session on.
    await main.send_remote_start_transaction(client.id, id_tag_value)
    await asyncio.wait_for(client.started.wait(), timeout=5)

    assert await Reservation.find_active(client.id, reservation_id) is None
    reservation = await Reservation.find_one(Reservation.reservation_id == reservation_id)
    assert reservation.is_active is False
    assert reservation.released_reason == "consumed"


async def test_cancel_reservation_moves_connector_back_to_available(
    reservable_charge_point, id_tag_value
):
    client = reservable_charge_point
    reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(FUTURE_EXPIRY)
    )
    assert response.status == ReservationStatus.accepted
    await asyncio.wait_for(client.reserved.wait(), timeout=5)

    cancel_response = await main.send_cancel_reservation(client.id, reservation_id)
    assert cancel_response.status == CancelReservationStatus.accepted
    await asyncio.wait_for(client.cancelled.wait(), timeout=5)

    assert client.get_connector_state(1).status == ChargePointStatus.available
    reservation = await Reservation.find_one(Reservation.reservation_id == reservation_id)
    assert reservation.is_active is False
    assert reservation.released_reason == "cancelled"


async def test_cancel_reservation_rejected_for_unknown_id(reservable_charge_point):
    response = await main.send_cancel_reservation(reservable_charge_point.id, 999999)
    assert response.status == CancelReservationStatus.rejected


async def test_sweep_expired_reservations(reservable_charge_point, id_tag_value):
    client = reservable_charge_point
    reservation_id, response = await main.send_reserve_now(
        client.id, 1, id_tag_value, iso(PAST_EXPIRY)
    )
    assert response.status == ReservationStatus.accepted
    await asyncio.wait_for(client.reserved.wait(), timeout=5)

    released = await Reservation.sweep_expired()
    assert released >= 1

    reservation = await Reservation.find_one(Reservation.reservation_id == reservation_id)
    assert reservation.is_active is False
    assert reservation.released_reason == "expired"
    # The sweep only updates this Central System's own bookkeeping -- a real charger's own
    # clock is what reverts its connector, which this test double never simulates.
    assert client.get_connector_state(1).status == ChargePointStatus.reserved


async def test_reserve_now_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_reserve_now(charge_point_identity, 1, "TAG", iso(FUTURE_EXPIRY))
        finally:
            listener.cancel()


async def test_cancel_reservation_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_cancel_reservation(charge_point_identity, 1)
        finally:
            listener.cancel()


async def test_reserve_now_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_reserve_now(charge_point_identity, 1, "TAG", iso(FUTURE_EXPIRY))


async def test_cancel_reservation_when_not_connected_raises_clear_error(
    db, charge_point_identity
):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_cancel_reservation(charge_point_identity, 1)
