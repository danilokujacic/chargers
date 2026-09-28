"""Tests for reset and unlock: an operator reboots a misbehaving charger, or frees a cable a
driver can't pull out, without a truck roll.

See instructions/06-remaining-flows.md section C. Like test_remote_start_stop.py and
test_change_availability.py, these call main.send_reset / main.send_unlock_connector directly --
the same functions main.py's admin API (see operate.py's reset / unlock-connector subcommands)
calls -- and use a local test double, not simulate_charge_point.py's SimulatedChargePoint,
for the reason those other test files give: that module is a standalone entry-point script
(its last line runs main() at import time), never meant to be imported.
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
    ChargePointErrorCode,
    ChargePointStatus,
    Reason,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    UnlockStatus,
)
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from models import ChargePoint, Transaction
from ocpp_client_auth import basic_auth_header

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


class ResettableChargePoint(cp):
    """A test double implementing Reset and UnlockConnector the way OCPP 1.6 s5.14/s5.17
    require: a Soft reset gracefully stops any open transaction before "rebooting" (closing the
    connection, standing in for a real power cycle); a Hard reset abandons one instead, exactly
    as a genuine power loss would. Also answers RemoteStartTransaction, minimally, so a
    transaction can be put in progress to test the Soft/Hard difference.
    """

    def __init__(self, id, connection, reject_reset=False, reject_unlock=False, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.reject_reset = reject_reset
        self.reject_unlock = reject_unlock
        self.open_transactions = {}
        self.reset_requested = None
        self.reset_applied = asyncio.Event()
        self.started = asyncio.Event()

    @on(Action.remote_start_transaction)
    def on_remote_start(self, id_tag, connector_id=None, **kwargs):
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @after(Action.remote_start_transaction)
    async def after_remote_start(self, id_tag, connector_id=None, **kwargs):
        connector_id = connector_id or 1
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.preparing,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )
        start = await self.call(
            call.StartTransaction(
                connector_id=connector_id, id_tag=id_tag, meter_start=0, timestamp=iso(NOW)
            ),
            suppress=False,
        )
        self.open_transactions[connector_id] = start.transaction_id
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.charging,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )
        self.started.set()

    @on(Action.unlock_connector)
    def on_unlock_connector(self, connector_id, **kwargs):
        if self.reject_unlock:
            return call_result.UnlockConnector(status=UnlockStatus.unlock_failed)
        return call_result.UnlockConnector(status=UnlockStatus.unlocked)

    @on(Action.reset)
    def on_reset(self, type, **kwargs):
        if self.reject_reset:
            return call_result.Reset(status=ResetStatus.rejected)
        return call_result.Reset(status=ResetStatus.accepted)

    @after(Action.reset)
    async def after_reset(self, type, **kwargs):
        if self.reject_reset:
            return
        reset_type = ResetType(type)
        if reset_type == ResetType.soft:
            for connector_id, transaction_id in list(self.open_transactions.items()):
                await self.call(
                    call.StopTransaction(
                        transaction_id=transaction_id,
                        meter_stop=0,
                        timestamp=iso(NOW),
                        reason=Reason.soft_reset,
                    ),
                    suppress=False,
                )
                await self.call(
                    call.StatusNotification(
                        connector_id=connector_id,
                        error_code=ChargePointErrorCode.no_error,
                        status=ChargePointStatus.finishing,
                        timestamp=iso(NOW),
                    ),
                    suppress=False,
                )
                self.open_transactions.pop(connector_id, None)
        self.reset_requested = reset_type
        self.reset_applied.set()
        await self._connection.close()


async def wait_until_registered(client):
    """See test_remote_start_stop.py's helper of the same name for why this is needed."""
    await client.call(call.Heartbeat(), suppress=False)


@pytest.fixture
async def resettable_charge_point(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = ResettableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def test_soft_reset_stops_open_transaction_then_reboots(
    resettable_charge_point, accepted_id_tag
):
    client = resettable_charge_point
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)
    transaction_id = client.open_transactions[1]

    response = await main.send_reset(client.id, ResetType.soft)
    assert response.status == ResetStatus.accepted
    await asyncio.wait_for(client.reset_applied.wait(), timeout=5)

    tx = await Transaction.find_one(Transaction.transaction_id == transaction_id)
    assert tx.is_open is False
    assert tx.stop_reason == Reason.soft_reset


async def test_hard_reset_abandons_open_transaction(resettable_charge_point, accepted_id_tag):
    client = resettable_charge_point
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)
    transaction_id = client.open_transactions[1]

    response = await main.send_reset(client.id, ResetType.hard)
    assert response.status == ResetStatus.accepted
    await asyncio.wait_for(client.reset_applied.wait(), timeout=5)

    # A Hard reset is a power cycle: OCPP 1.6 s5.14 does not require a graceful stop first, so
    # this Central System must not assume the transaction was closed -- expect its
    # StopTransaction later (or never), not synchronously with the reset.
    tx = await Transaction.find_one(Transaction.transaction_id == transaction_id)
    assert tx.is_open is True


async def test_reset_rejected_by_charger_does_not_disconnect(resettable_charge_point):
    client = resettable_charge_point
    client.reject_reset = True
    response = await main.send_reset(client.id, ResetType.soft)
    assert response.status == ResetStatus.rejected
    # main.CONNECTED_CHARGE_POINTS holds the SERVER's own MyChargePoint for this connection,
    # not this test's client-side double -- checking it is still present is what proves the
    # connection was never closed.
    assert client.id in main.CONNECTED_CHARGE_POINTS


async def test_commissioning_runs_again_after_a_reset(server, registered_charge_point):
    """OCPP 1.6 s4.2.1: the charger reboots and the whole commissioning flow runs again from
    BootNotification. A fresh connection under the same identity, after the double from the
    first one closed its socket, must boot straight back to Accepted -- the same on_boot/
    after_boot path every other boot in this project already goes through.
    """
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = ResettableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            response = await main.send_reset(identity, ResetType.soft)
            assert response.status == ResetStatus.accepted
            await asyncio.wait_for(client.reset_applied.wait(), timeout=5)
        finally:
            listener.cancel()

    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = cp(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            boot = await client.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor="V"),
                suppress=False,
            )
            assert boot.status == "Accepted"
        finally:
            listener.cancel()


async def test_reset_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_reset(charge_point_identity, ResetType.soft)
        finally:
            listener.cancel()


async def test_reset_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_reset(charge_point_identity, ResetType.soft)


async def test_unlock_connector_accepted_does_not_touch_open_transaction(
    resettable_charge_point, accepted_id_tag
):
    client = resettable_charge_point
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)
    transaction_id = client.open_transactions[1]

    response = await main.send_unlock_connector(client.id, 1)
    assert response.status == UnlockStatus.unlocked

    # OCPP 1.6 s5.17: "Unlock Connector does not need to stop an ongoing transaction."
    tx = await Transaction.find_one(Transaction.transaction_id == transaction_id)
    assert tx.is_open is True


async def test_unlock_connector_unlock_failed(resettable_charge_point):
    client = resettable_charge_point
    client.reject_unlock = True
    response = await main.send_unlock_connector(client.id, 1)
    assert response.status == UnlockStatus.unlock_failed


async def test_unlock_connector_rejects_connector_zero(resettable_charge_point):
    with pytest.raises(ValueError):
        await main.send_unlock_connector(resettable_charge_point.id, 0)


async def test_unlock_connector_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_unlock_connector(charge_point_identity, 1)
        finally:
            listener.cancel()


async def test_unlock_connector_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_unlock_connector(charge_point_identity, 1)
