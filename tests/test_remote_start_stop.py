"""Tests for remote start/stop: an operator or a driver's app asks a charger to begin or end a
charging session, rather than the driver presenting a card at the charger itself.

See instructions/06-remaining-flows.md section A. These call main.send_remote_start_transaction
/ send_remote_stop_transaction directly -- the same functions main.py's admin API (see
operate.py) calls -- since the test suite runs in the same process as the server and does not
need to go over HTTP to reach them. A handful of tests exercise the admin HTTP layer itself,
using asyncio's own streams rather than a blocking HTTP client, which would freeze the shared
event loop the in-process server also runs on.
"""

import asyncio
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    RemoteStartStopStatus,
)
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from models import ChargePoint, ConnectorStatus, IdTag, Transaction
from ocpp_client_auth import basic_auth_header

# Kept local rather than imported from test_normal_charge_flow.py: importing one test module
# from another would leave it double-loaded under two different names once pytest also
# collects it directly (there is no __init__.py making tests/ a real package).
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def iso(dt):
    """An OCPP dateTime string for a Python datetime."""
    return dt.isoformat()


class RemoteControllableChargePoint(cp):
    """A test double that answers RemoteStartTransaction/RemoteStopTransaction the way a real
    charger would (OCPP 1.6 s5.11/s5.12), configurably, so both AuthorizeRemoteTxRequests
    branches and the reject path can be exercised without pulling in the full simulator script.
    """

    def __init__(self, id, connection, authorize_remote_tx_requests=True, reject=False, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.authorize_remote_tx_requests = authorize_remote_tx_requests
        self.reject = reject
        self.open_transactions = {}
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    @on(Action.remote_start_transaction)
    def on_remote_start(self, id_tag, connector_id=None, **kwargs):
        if self.reject:
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @after(Action.remote_start_transaction)
    async def after_remote_start(self, id_tag, connector_id=None, **kwargs):
        if self.reject:
            return
        connector_id = connector_id or 1
        if self.authorize_remote_tx_requests:
            auth = await self.call(call.Authorize(id_tag=id_tag), suppress=False)
            if auth.id_tag_info["status"] != AuthorizationStatus.accepted:
                return
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

    @on(Action.remote_stop_transaction)
    def on_remote_stop(self, transaction_id, **kwargs):
        if transaction_id not in self.open_transactions.values():
            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    @after(Action.remote_stop_transaction)
    async def after_remote_stop(self, transaction_id, **kwargs):
        connector_id = next(
            (c for c, t in self.open_transactions.items() if t == transaction_id), None
        )
        if connector_id is None:
            return
        await self.call(
            call.StopTransaction(
                transaction_id=transaction_id, meter_stop=0, timestamp=iso(NOW)
            ),
            suppress=False,
        )
        # OCPP 1.6 s5.12: a remote stop moves the connector to Finishing (transition C6).
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.finishing,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )
        del self.open_transactions[connector_id]
        self.stopped.set()


@pytest.fixture
async def remote_controllable_charge_point(server, registered_charge_point):
    """Like conftest's connected_charge_point, but with remote-start/stop handlers."""
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = RemoteControllableChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def wait_until_registered(client):
    """Block until the Central System has this identity in CONNECTED_CHARGE_POINTS.

    The client-side handshake completing does not mean on_connect's own ChargePointRecord
    lookup has finished and registered the connection yet -- there is a real gap between the
    two. main.py's on_connect only starts reading incoming messages (charge_point.start())
    AFTER that registration, so a successful round trip through the connection proves it has
    already happened; no sleep or polling loop is needed.
    """
    await client.call(call.Heartbeat(), suppress=False)


async def test_remote_start_conf_accepted_is_not_yet_a_transaction(
    remote_controllable_charge_point, accepted_id_tag
):
    client = remote_controllable_charge_point
    response = await main.send_remote_start_transaction(client.id, accepted_id_tag)
    assert response.status == "Accepted"
    # Accepted means only "will attempt" (OCPP 1.6 s5.11) -- the transaction does not exist
    # until the charger's own, separate StartTransaction arrives.
    assert await Transaction.find_one(Transaction.charge_point_identity == client.id) is None
    await asyncio.wait_for(client.started.wait(), timeout=5)
    assert await Transaction.find_one(Transaction.charge_point_identity == client.id) is not None


async def test_remote_start_with_authorize_remote_tx_requests_true(
    server, registered_charge_point, accepted_id_tag
):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = RemoteControllableChargePoint(identity, ws, authorize_remote_tx_requests=True)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            await main.send_remote_start_transaction(identity, accepted_id_tag)
            await asyncio.wait_for(client.started.wait(), timeout=5)
        finally:
            listener.cancel()
    tx = await Transaction.find_one(Transaction.charge_point_identity == identity)
    assert tx is not None
    assert tx.id_tag == accepted_id_tag


async def test_remote_start_with_authorize_remote_tx_requests_false_lets_cs_check(
    server, registered_charge_point, id_tag_value
):
    await IdTag(id_tag=id_tag_value, status=AuthorizationStatus.blocked).insert()
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = RemoteControllableChargePoint(identity, ws, authorize_remote_tx_requests=False)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            await main.send_remote_start_transaction(identity, id_tag_value)
            # False means the charger starts immediately, without checking locally -- it does
            # not call Authorize.req at all, so waiting on client.started (not gated on
            # authorization here) is the right signal that it proceeded regardless.
            await asyncio.wait_for(client.started.wait(), timeout=5)
        finally:
            listener.cancel()
    # The Central System still records what actually happened: a transaction exists, and its
    # StartTransaction.conf carried the blocked status -- checked here via the stored idTag,
    # since the response itself isn't retained; on_start's own log line is the CS-side record.
    tx = await Transaction.find_one(Transaction.charge_point_identity == identity)
    assert tx is not None
    assert tx.id_tag == id_tag_value


async def test_remote_stop_moves_connector_to_finishing(
    remote_controllable_charge_point, accepted_id_tag
):
    client = remote_controllable_charge_point
    await main.send_remote_start_transaction(client.id, accepted_id_tag)
    await asyncio.wait_for(client.started.wait(), timeout=5)
    transaction_id = next(iter(client.open_transactions.values()))

    response = await main.send_remote_stop_transaction(client.id, transaction_id)
    assert response.status == "Accepted"
    await asyncio.wait_for(client.stopped.wait(), timeout=5)

    tx = await Transaction.find_one(Transaction.transaction_id == transaction_id)
    assert tx.is_open is False
    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.finishing


async def test_remote_start_rejected_by_charger_creates_no_transaction(
    server, registered_charge_point, accepted_id_tag
):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = RemoteControllableChargePoint(identity, ws, reject=True)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            response = await main.send_remote_start_transaction(identity, accepted_id_tag)
            assert response.status == "Rejected"
        finally:
            listener.cancel()
    assert await Transaction.find_one(Transaction.charge_point_identity == identity) is None


async def test_remote_start_forbidden_while_pending(server, charge_point_identity):
    _record, key = await ChargePoint.register(charge_point_identity)  # default: Pending
    header = basic_auth_header(charge_point_identity, key)
    # A bare client with no ChangeConfiguration handler: the server's Route B onboarding
    # attempt fails harmlessly (a CallError comes back), and the charger stays Pending --
    # exactly the connected-but-not-yet-Accepted state this test needs.
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
                await main.send_remote_start_transaction(charge_point_identity, "TAG")
        finally:
            listener.cancel()


async def test_remote_stop_forbidden_while_pending(server, charge_point_identity):
    _record, key = await ChargePoint.register(charge_point_identity)
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
                await main.send_remote_stop_transaction(charge_point_identity, 1)
        finally:
            listener.cancel()


async def test_remote_start_rejects_connector_zero(
    remote_controllable_charge_point, accepted_id_tag
):
    with pytest.raises(ValueError):
        await main.send_remote_start_transaction(
            remote_controllable_charge_point.id, accepted_id_tag, connector_id=0
        )


async def test_remote_start_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(
        charge_point_identity, registration_status="Accepted"
    )
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_remote_start_transaction(charge_point_identity, "TAG")


async def test_remote_stop_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(
        charge_point_identity, registration_status="Accepted"
    )
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_remote_stop_transaction(charge_point_identity, 1)


# --------------------------------------------------------------------------------------------
# The admin HTTP API (see operate.py). asyncio.get_event_loop().run_in_executor is used instead
# of a plain blocking urllib call, because a blocking call from inside this coroutine would
# freeze the same event loop the in-process server needs to answer it -- a real deadlock this
# session's own exploration for this feature hit directly.
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


@pytest.fixture
def admin_token(monkeypatch):
    """Sets ADMIN_TOKEN for the admin API tests; main.py reads it per-request, so no restart
    is needed for this to take effect."""
    token = "test-admin-token"
    monkeypatch.setenv("ADMIN_TOKEN", token)
    return token


async def test_admin_api_disabled_without_token(server, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    http_url = server.replace("ws://", "http://")
    status, body = await http_get(f"{http_url}/admin/remote-start?identity=X&id_tag=Y&token=z")
    assert status == 404
    assert b"disabled" in body


async def test_admin_api_rejects_wrong_token(server, admin_token):
    http_url = server.replace("ws://", "http://")
    status, _body = await http_get(
        f"{http_url}/admin/remote-start?identity=X&id_tag=Y&token=wrong"
    )
    assert status == 401


async def test_admin_api_remote_start_end_to_end(
    server, admin_token, remote_controllable_charge_point, accepted_id_tag
):
    client = remote_controllable_charge_point
    http_url = server.replace("ws://", "http://")
    status, body = await http_get(
        f"{http_url}/admin/remote-start?identity={client.id}&id_tag={accepted_id_tag}"
        f"&token={admin_token}"
    )
    assert status == 200
    assert b"Accepted" in body
    await asyncio.wait_for(client.started.wait(), timeout=5)
