"""Tests for firmware updates and diagnostics uploads: an operator pushes a new firmware image
to a charger, or pulls its logs when something needs investigating, without a truck roll.

See instructions/06-remaining-flows.md section H. Neither UpdateFirmware.conf nor
GetDiagnostics.conf carries an accept/reject status (OCPP 1.6 s5.16/s5.1) -- the charger either
attempts the fetch, or (for diagnostics) simply omits fileName to decline -- so the test double
here has no reject flags, unlike earlier 06 test files; it only needs to answer those two confs
and let the test drive the progress StatusNotification messages on its own schedule,
deterministically, rather than on a timer the way simulate_charge_point.py's real demo does.
"""

import asyncio
from datetime import UTC, datetime

import pytest
import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, DiagnosticsStatus, FirmwareStatus
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from models import ChargePoint, FirmwareUpdate
from ocpp_client_auth import basic_auth_header

FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


class FirmwareChargePoint(cp):
    """A test double answering UpdateFirmware/GetDiagnostics the way OCPP 1.6 s5.16/s5.1
    require: an empty conf for the former, an optional file_name for the latter. Progress
    (FirmwareStatusNotification/DiagnosticsStatusNotification) is sent by calling
    report_firmware_status/report_diagnostics_status directly from a test, not automatically,
    so tests stay deterministic instead of racing a timer.
    """

    def __init__(self, id, connection, decline_diagnostics=False, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.decline_diagnostics = decline_diagnostics
        self.received_location = None
        self.received_retrieve_date = None
        self.diagnostics_location = None

    @on(Action.update_firmware)
    def on_update_firmware(self, location, retrieve_date, **kwargs):
        self.received_location = location
        self.received_retrieve_date = retrieve_date
        return call_result.UpdateFirmware()

    @on(Action.get_diagnostics)
    def on_get_diagnostics(self, location, **kwargs):
        self.diagnostics_location = location
        if self.decline_diagnostics:
            return call_result.GetDiagnostics()
        return call_result.GetDiagnostics(file_name=f"{self.id}-diagnostics.zip")

    async def report_firmware_status(self, status):
        return await self.call(call.FirmwareStatusNotification(status=status), suppress=False)

    async def report_diagnostics_status(self, status):
        return await self.call(
            call.DiagnosticsStatusNotification(status=status), suppress=False
        )


async def wait_until_registered(client):
    """See test_remote_start_stop.py's helper of the same name for why this is needed."""
    await client.call(call.Heartbeat(), suppress=False)


@pytest.fixture
async def firmware_charge_point(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = FirmwareChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def test_update_firmware_builds_a_url_this_system_hosts(firmware_charge_point):
    client = firmware_charge_point
    location = await main.send_update_firmware(client.id, "example-1.0.0.txt", iso(FUTURE))
    assert location == f"{main.firmware_base_url()}/firmware/example-1.0.0.txt"
    assert client.received_location == location
    assert client.received_retrieve_date == iso(FUTURE)


async def test_update_firmware_missing_file_raises_clear_error(firmware_charge_point):
    with pytest.raises(ValueError, match="not found"):
        await main.send_update_firmware(firmware_charge_point.id, "no-such-file.bin", iso(FUTURE))


async def test_update_firmware_creates_record_unconditionally(firmware_charge_point):
    client = firmware_charge_point
    await main.send_update_firmware(client.id, "example-1.0.0.txt", iso(FUTURE))
    record = await main.get_firmware_status(client.id)
    assert record is not None
    assert record.status == FirmwareStatus.idle
    assert record.history == []


async def test_firmware_status_notifications_recorded_in_order(firmware_charge_point):
    client = firmware_charge_point
    await main.send_update_firmware(client.id, "example-1.0.0.txt", iso(FUTURE))
    for status in (
        FirmwareStatus.downloading, FirmwareStatus.downloaded,
        FirmwareStatus.installing, FirmwareStatus.installed,
    ):
        await client.report_firmware_status(status)

    record = await main.get_firmware_status(client.id)
    assert record.status == FirmwareStatus.installed
    assert [entry["status"] for entry in record.history] == [
        "Downloading", "Downloaded", "Installing", "Installed",
    ]


async def test_firmware_status_matches_most_recently_requested_update(firmware_charge_point):
    client = firmware_charge_point
    await main.send_update_firmware(client.id, "example-1.0.0.txt", iso(FUTURE))
    await asyncio.sleep(0.01)  # requested_at strictly increases between the two requests
    await main.send_update_firmware(client.id, "example-1.0.0.txt", iso(FUTURE))

    await client.report_firmware_status(FirmwareStatus.downloading)

    record = await main.get_firmware_status(client.id)
    assert record.status == FirmwareStatus.downloading

    both = await FirmwareUpdate.find(
        FirmwareUpdate.charge_point_identity == client.id
    ).to_list()
    idle_count = sum(1 for r in both if r.status == FirmwareStatus.idle)
    assert idle_count == 1  # the earlier request was left untouched


async def test_update_firmware_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_update_firmware(
                    charge_point_identity, "example-1.0.0.txt", iso(FUTURE)
                )
        finally:
            listener.cancel()


async def test_update_firmware_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_update_firmware(charge_point_identity, "example-1.0.0.txt", iso(FUTURE))


async def test_firmware_status_when_none_requested_returns_none(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    assert await main.get_firmware_status(charge_point_identity) is None


async def test_get_diagnostics_accepted_creates_record(firmware_charge_point):
    client = firmware_charge_point
    response = await main.send_get_diagnostics(client.id)
    assert response.file_name == f"{client.id}-diagnostics.zip"
    assert client.diagnostics_location == f"{main.diagnostics_upload_base_url()}/{client.id}"

    record = await main.get_diagnostics_status(client.id)
    assert record is not None
    assert record.file_name == response.file_name
    assert record.status == DiagnosticsStatus.idle


async def test_get_diagnostics_declined_creates_no_record(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = FirmwareChargePoint(identity, ws, decline_diagnostics=True)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            response = await main.send_get_diagnostics(identity)
            assert response.file_name is None
        finally:
            listener.cancel()
    assert await main.get_diagnostics_status(identity) is None


async def test_diagnostics_status_notifications_recorded(firmware_charge_point):
    client = firmware_charge_point
    await main.send_get_diagnostics(client.id)
    await client.report_diagnostics_status(DiagnosticsStatus.uploading)
    await client.report_diagnostics_status(DiagnosticsStatus.uploaded)

    record = await main.get_diagnostics_status(client.id)
    assert record.status == DiagnosticsStatus.uploaded
    assert [entry["status"] for entry in record.history] == ["Uploading", "Uploaded"]


async def test_get_diagnostics_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_get_diagnostics(charge_point_identity)
        finally:
            listener.cancel()


async def test_get_diagnostics_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_get_diagnostics(charge_point_identity)


async def test_diagnostics_status_when_none_requested_returns_none(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    assert await main.get_diagnostics_status(charge_point_identity) is None
