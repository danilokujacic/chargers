"""Tests for TriggerMessage / Remote Trigger (instructions/06-remaining-flows.md section J;
OCPP 1.6 s5.16): the Central System asks a charger to send one of its own messages now.

The test double sends the requested message from an @after hook, i.e. only once
TriggerMessage.conf is on the wire, as s5.16 requires; tests wait on `sent`, which is set only
after the Central System has answered that message, so no sleeps are needed.
"""

import asyncio
import urllib.error
import urllib.parse
import urllib.request

import pytest
import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    ChargePointErrorCode,
    ChargePointStatus,
    MessageTrigger,
    RegistrationStatus,
    TriggerMessageStatus,
)
from websockets.typing import Subprotocol

import main
from models import ChargePoint, ConnectorStatus
from ocpp_client_auth import basic_auth_header

VENDOR = "TriggerVendor"


class TriggerableChargePoint(cp):
    """Answers TriggerMessage per s5.16, then sends the requested message."""

    def __init__(self, id, connection, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.answer = TriggerMessageStatus.accepted
        self.sent = asyncio.Event()
        self.heartbeat_response = None
        self.boot_response = None

    @on("TriggerMessage")
    def on_trigger_message(self, requested_message, connector_id=None, **kwargs):
        return call_result.TriggerMessage(status=self.answer)

    @after("TriggerMessage")
    async def after_trigger_message(self, requested_message, connector_id=None, **kwargs):
        if self.answer != TriggerMessageStatus.accepted:
            return
        message = MessageTrigger(requested_message)
        if message == MessageTrigger.heartbeat:
            self.heartbeat_response = await self.call(call.Heartbeat(), suppress=False)
        elif message == MessageTrigger.boot_notification:
            self.boot_response = await self.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor=VENDOR),
                suppress=False,
            )
        elif message == MessageTrigger.status_notification:
            await self.call(
                call.StatusNotification(
                    connector_id=connector_id,
                    error_code=ChargePointErrorCode.no_error,
                    status=ChargePointStatus.preparing,
                ),
                suppress=False,
            )
        elif message == MessageTrigger.meter_values:
            await self.call(
                call.MeterValues(
                    connector_id=connector_id,
                    meter_value=[
                        {"timestamp": "2026-01-01T00:00:00Z", "sampled_value": [{"value": "77"}]}
                    ],
                ),
                suppress=False,
            )
        self.sent.set()


async def connect(server, identity, key):
    return websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": basic_auth_header(identity, key)},
    )


@pytest.fixture
async def triggerable(server, registered_charge_point):
    identity, key = registered_charge_point
    async with await connect(server, identity, key) as ws:
        client = TriggerableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            # See test_remote_start_stop.py: one real round trip guarantees the registry entry.
            await client.call(call.Heartbeat(), suppress=False)
            yield client
        finally:
            listener.cancel()


async def wait_sent(client):
    await asyncio.wait_for(client.sent.wait(), timeout=5)


async def test_trigger_heartbeat(triggerable):
    response = await main.send_trigger_message(triggerable.id, MessageTrigger.heartbeat)
    assert response.status == TriggerMessageStatus.accepted
    await wait_sent(triggerable)
    assert triggerable.heartbeat_response.current_time


async def test_trigger_status_notification_for_a_connector_is_recorded(triggerable):
    response = await main.send_trigger_message(
        triggerable.id, MessageTrigger.status_notification, connector_id=1
    )
    assert response.status == TriggerMessageStatus.accepted
    await wait_sent(triggerable)
    record = await ConnectorStatus.find_one(
        ConnectorStatus.charge_point_identity == triggerable.id, ConnectorStatus.connector_id == 1
    )
    assert record.status == ChargePointStatus.preparing


async def test_trigger_meter_values_is_recorded(triggerable):
    response = await main.send_trigger_message(
        triggerable.id, MessageTrigger.meter_values, connector_id=1
    )
    assert response.status == TriggerMessageStatus.accepted
    await wait_sent(triggerable)
    record = await ConnectorStatus.find_one(
        ConnectorStatus.charge_point_identity == triggerable.id, ConnectorStatus.connector_id == 1
    )
    assert record.last_meter_values[0]["sampled_value"][0]["value"] == "77"


async def test_trigger_boot_notification_reregisters_an_accepted_charger(triggerable):
    response = await main.send_trigger_message(triggerable.id, MessageTrigger.boot_notification)
    assert response.status == TriggerMessageStatus.accepted
    await wait_sent(triggerable)
    assert triggerable.boot_response.status == RegistrationStatus.accepted
    record = await ChargePoint.find_one(ChargePoint.identity == triggerable.id)
    assert record.charge_point_vendor == VENDOR


@pytest.mark.parametrize(
    "answer", [TriggerMessageStatus.rejected, TriggerMessageStatus.not_implemented]
)
async def test_charger_refusal_is_returned_as_is(triggerable, answer):
    triggerable.answer = answer
    response = await main.send_trigger_message(triggerable.id, MessageTrigger.heartbeat)
    assert response.status == answer
    assert not triggerable.sent.is_set()


async def test_connector_id_refused_for_messages_that_do_not_take_one(triggerable):
    with pytest.raises(ValueError):
        await main.send_trigger_message(triggerable.id, MessageTrigger.heartbeat, connector_id=1)


async def test_invalid_message_name_raises(triggerable):
    with pytest.raises(ValueError):
        await main.send_trigger_message(triggerable.id, "NotAMessage")


async def test_trigger_is_allowed_while_pending(server, charge_point_identity):
    """s4.2 only bars RemoteStart/RemoteStop while Pending; TriggerMessage must still go out."""
    _record, key = await ChargePoint.register(charge_point_identity)  # default: Pending
    async with await connect(server, charge_point_identity, key) as ws:
        client = TriggerableChargePoint(charge_point_identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await client.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor="V"),
                suppress=False,
            )
            response = await main.send_trigger_message(client.id, MessageTrigger.heartbeat)
            assert response.status == TriggerMessageStatus.accepted
            await wait_sent(client)
        finally:
            listener.cancel()


async def test_trigger_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_trigger_message(charge_point_identity, MessageTrigger.heartbeat)


# --------------------------------------------------------------------------------------------
# Admin API
# --------------------------------------------------------------------------------------------


async def http_get(url):
    loop = asyncio.get_event_loop()

    def fetch():
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    return await loop.run_in_executor(None, fetch)


async def test_admin_trigger_message_end_to_end(triggerable, server, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "t")
    query = urllib.parse.urlencode(
        {"identity": triggerable.id, "message": "Heartbeat", "token": "t"}
    )
    status, body = await http_get(f"{server.replace('ws://', 'http://')}/admin/trigger-message?{query}")
    assert status == 200
    assert b"Accepted" in body
    await wait_sent(triggerable)


async def test_admin_trigger_message_rejects_unknown_message(server, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "t")
    query = urllib.parse.urlencode({"identity": "x", "message": "Bogus", "token": "t"})
    status, _body = await http_get(f"{server.replace('ws://', 'http://')}/admin/trigger-message?{query}")
    assert status == 400
