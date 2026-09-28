"""Tests for configuration management: an operator inspects what a charger supports and tunes
one of its settings, and the platform empties a charger's local authorization cache when it
might be stale. Also covers DataTransfer, the vendor-specific escape hatch a charger can use to
send this Central System something outside the standard message set.

See instructions/06-remaining-flows.md section D. Like the other 06 test files, these call
main.send_get_configuration / send_change_configuration / send_clear_cache directly -- the same
functions main.py's admin API (see operate.py's get-configuration / change-configuration /
clear-cache subcommands) calls -- and use a local test double rather than
simulate_charge_point.py's SimulatedChargePoint, which is a standalone entry-point script never
meant to be imported.
"""

import asyncio

import pytest
import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result, datatypes
from ocpp.v16.enums import Action, ClearCacheStatus, ConfigurationStatus
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from models import ChargePoint, ConfigurationEntry
from ocpp_client_auth import basic_auth_header


class ConfigurableChargePoint(cp):
    """A test double implementing GetConfiguration/ChangeConfiguration/ClearCache the way OCPP
    1.6 s5.9/s5.6/s5.4 require. `report_authorization_key_value` lets a test simulate a
    non-compliant charger that ignores OCPP-J 1.6 s6.2.2's "should not give back the
    authorization key" and reports one anyway, to prove main.py itself never persists it
    regardless of what a charger sends.
    """

    def __init__(
        self,
        id,
        connection,
        reject_change_configuration=False,
        reject_clear_cache=False,
        report_authorization_key_value=None,
        **kwargs,
    ):
        super().__init__(id, connection, **kwargs)
        self.reject_change_configuration = reject_change_configuration
        self.reject_clear_cache = reject_clear_cache
        self.configuration = {
            "AuthorizationKey": (report_authorization_key_value, True),
            "HeartbeatInterval": ("30", False),
            "NumberOfConnectors": ("1", True),
        }

    @on(Action.get_configuration)
    def on_get_configuration(self, key=None, **kwargs):
        requested = key if key else list(self.configuration)
        configuration_key = []
        unknown_key = []
        for name in requested:
            if name not in self.configuration:
                unknown_key.append(name)
                continue
            value, readonly = self.configuration[name]
            configuration_key.append(datatypes.KeyValue(key=name, readonly=readonly, value=value))
        return call_result.GetConfiguration(
            configuration_key=configuration_key, unknown_key=unknown_key
        )

    @on(Action.change_configuration)
    def on_change_configuration(self, key, value, **kwargs):
        if self.reject_change_configuration:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        if key not in self.configuration:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)
        _old_value, readonly = self.configuration[key]
        if readonly:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        self.configuration[key] = (value, readonly)
        if key == "HeartbeatInterval":
            return call_result.ChangeConfiguration(status=ConfigurationStatus.reboot_required)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.clear_cache)
    def on_clear_cache(self, **kwargs):
        if self.reject_clear_cache:
            return call_result.ClearCache(status=ClearCacheStatus.rejected)
        return call_result.ClearCache(status=ClearCacheStatus.accepted)


async def wait_until_registered(client):
    """See test_remote_start_stop.py's helper of the same name for why this is needed."""
    await client.call(call.Heartbeat(), suppress=False)


@pytest.fixture
async def configurable_charge_point(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = ConfigurableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def test_get_configuration_reports_known_and_unknown_keys(configurable_charge_point):
    client = configurable_charge_point
    response = await main.send_get_configuration(
        client.id, keys=["HeartbeatInterval", "NoSuchKey"]
    )
    assert response.unknown_key == ["NoSuchKey"]
    reported = {entry["key"]: entry["value"] for entry in response.configuration_key}
    assert reported == {"HeartbeatInterval": "30"}

    entry = await ConfigurationEntry.find_one(
        ConfigurationEntry.charge_point_identity == client.id,
        ConfigurationEntry.key == "HeartbeatInterval",
    )
    assert entry.value == "30"
    assert entry.readonly is False


async def test_get_configuration_omitting_keys_returns_everything(configurable_charge_point):
    client = configurable_charge_point
    response = await main.send_get_configuration(client.id)
    reported_keys = {entry["key"] for entry in response.configuration_key}
    assert reported_keys == {"AuthorizationKey", "HeartbeatInterval", "NumberOfConnectors"}


async def test_get_configuration_never_persists_authorization_key_value(
    server, registered_charge_point
):
    """Even a non-compliant charger that reports AuthorizationKey's value anyway (OCPP-J 1.6
    s6.2.2 says it should not) must not get that value written to this Central System's store.
    """
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = ConfigurableChargePoint(
            identity, ws, report_authorization_key_value="DEADBEEF"
        )
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            await main.send_get_configuration(identity, keys=["AuthorizationKey"])
        finally:
            listener.cancel()

    entry = await ConfigurationEntry.find_one(
        ConfigurationEntry.charge_point_identity == identity,
        ConfigurationEntry.key == "AuthorizationKey",
    )
    assert entry is not None
    assert entry.value is None
    assert entry.readonly is True


async def test_change_configuration_accepted_persists_value(configurable_charge_point):
    client = configurable_charge_point
    response = await main.send_change_configuration(client.id, "HeartbeatInterval", "60")
    assert response.status == ConfigurationStatus.reboot_required

    entry = await ConfigurationEntry.find_one(
        ConfigurationEntry.charge_point_identity == client.id,
        ConfigurationEntry.key == "HeartbeatInterval",
    )
    assert entry.value == "60"


async def test_change_configuration_rejected_does_not_persist(configurable_charge_point):
    client = configurable_charge_point
    client.reject_change_configuration = True
    response = await main.send_change_configuration(client.id, "HeartbeatInterval", "60")
    assert response.status == ConfigurationStatus.rejected

    entry = await ConfigurationEntry.find_one(
        ConfigurationEntry.charge_point_identity == client.id,
        ConfigurationEntry.key == "HeartbeatInterval",
    )
    assert entry is None


async def test_change_configuration_not_supported_for_unknown_key(configurable_charge_point):
    response = await main.send_change_configuration(
        configurable_charge_point.id, "NoSuchKey", "1"
    )
    assert response.status == ConfigurationStatus.not_supported


async def test_change_configuration_rejected_for_readonly_key(configurable_charge_point):
    response = await main.send_change_configuration(
        configurable_charge_point.id, "NumberOfConnectors", "2"
    )
    assert response.status == ConfigurationStatus.rejected


async def test_clear_cache_accepted(configurable_charge_point):
    response = await main.send_clear_cache(configurable_charge_point.id)
    assert response.status == ClearCacheStatus.accepted


async def test_clear_cache_rejected(configurable_charge_point):
    configurable_charge_point.reject_clear_cache = True
    response = await main.send_clear_cache(configurable_charge_point.id)
    assert response.status == ClearCacheStatus.rejected


async def test_data_transfer_unknown_vendor_id(configurable_charge_point):
    response = await configurable_charge_point.call(
        call.DataTransfer(vendor_id="com.example.unregistered"), suppress=False
    )
    assert response.status == "UnknownVendorId"


async def test_get_configuration_and_change_configuration_allowed_while_pending(
    server, charge_point_identity
):
    """OCPP 1.6 s4.2 exempts "configuration reads and writes" from the Pending restriction that
    forbids every other Central-System-initiated message in this project.
    """
    _record, key = await ChargePoint.register(charge_point_identity)  # default: Pending
    header = basic_auth_header(charge_point_identity, key)
    async with websockets.connect(
        f"{server}/{charge_point_identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = ConfigurableChargePoint(charge_point_identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await client.call(
                call.BootNotification(charge_point_model="M", charge_point_vendor="V"),
                suppress=False,
            )
            get_response = await main.send_get_configuration(charge_point_identity)
            assert get_response.configuration_key
            change_response = await main.send_change_configuration(
                charge_point_identity, "HeartbeatInterval", "45"
            )
            assert change_response.status == ConfigurationStatus.reboot_required
        finally:
            listener.cancel()


async def test_clear_cache_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_clear_cache(charge_point_identity)
        finally:
            listener.cancel()


async def test_get_configuration_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_get_configuration(charge_point_identity)


async def test_change_configuration_when_not_connected_raises_clear_error(
    db, charge_point_identity
):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_change_configuration(charge_point_identity, "HeartbeatInterval", "60")


async def test_clear_cache_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_clear_cache(charge_point_identity)
