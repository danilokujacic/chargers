"""Tests for offline operation and local authorization: a charger keeps letting cards through
even when it cannot reach this Central System, using two of its own records -- the Local
Authorization List this system pushes ahead of time, and the Authorization Cache it builds up
on its own from what it was told while still online.

See instructions/06-remaining-flows.md section F. As the section itself says, most of "keep
working offline" was already built and tested by 04/05 (StartTransaction/StopTransaction trust
whatever timestamp the charger reports, however old); this file adds SendLocalList/
GetLocalListVersion coverage, plus the same offline-timestamp trust exercised in bulk rather
than one pair at a time, per this section's own instruction to do so.

Like the other 06 test files, SendLocalList/GetLocalListVersion tests use a local test double
rather than simulate_charge_point.py's SimulatedChargePoint, which is a standalone entry-point
script never meant to be imported. The Authorization Cache itself is entirely charger-side data
(OCPP 1.6 s3.5.1) -- there is nothing for this Central System to assert on beyond ClearCache,
already covered by test_configuration.py -- so it is not retested here.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest
import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, AuthorizationStatus, UpdateStatus, UpdateType
from websockets.typing import Subprotocol

import main
from commissioning import PendingChargerError
from models import ChargePoint, IdTag, LocalListState, Transaction
from ocpp_client_auth import basic_auth_header


def iso(dt):
    return dt.isoformat()


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class LocalListChargePoint(cp):
    """A test double implementing GetLocalListVersion/SendLocalList the way OCPP 1.6 s5.7/s5.8
    require: Full replaces the whole list, Differential merges add/remove entries, and a
    list_version at or below the current one is a VersionMismatch.
    """

    def __init__(self, id, connection, reject_send_local_list=False, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.reject_send_local_list = reject_send_local_list
        self.local_list = {}
        self.local_list_version = 0

    @on(Action.get_local_list_version)
    def on_get_local_list_version(self, **kwargs):
        return call_result.GetLocalListVersion(list_version=self.local_list_version)

    @on(Action.send_local_list)
    def on_send_local_list(
        self, list_version, update_type, local_authorization_list=None, **kwargs
    ):
        if self.reject_send_local_list:
            return call_result.SendLocalList(status=UpdateStatus.failed)
        if list_version <= self.local_list_version:
            return call_result.SendLocalList(status=UpdateStatus.version_mismatch)
        entries = local_authorization_list or []
        if UpdateType(update_type) == UpdateType.full:
            self.local_list = {entry["id_tag"]: entry.get("id_tag_info") for entry in entries}
        else:
            for entry in entries:
                id_tag = entry["id_tag"]
                id_tag_info = entry.get("id_tag_info")
                if id_tag_info is None:
                    self.local_list.pop(id_tag, None)
                else:
                    self.local_list[id_tag] = id_tag_info
        self.local_list_version = list_version
        return call_result.SendLocalList(status=UpdateStatus.accepted)


async def wait_until_registered(client):
    """See test_remote_start_stop.py's helper of the same name for why this is needed."""
    await client.call(call.Heartbeat(), suppress=False)


@pytest.fixture
async def local_list_charge_point(server, registered_charge_point):
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = LocalListChargePoint(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            await wait_until_registered(client)
            yield client
        finally:
            listener.cancel()


async def test_send_local_list_full_update_persists_version_and_tags(
    local_list_charge_point, id_tag_value
):
    client = local_list_charge_point
    await IdTag(id_tag=id_tag_value).insert()

    new_version, response = await main.send_send_local_list(
        client.id, id_tags=[id_tag_value], update_type=UpdateType.full
    )
    assert response.status == UpdateStatus.accepted
    assert new_version == 1
    assert client.local_list_version == 1
    assert id_tag_value in client.local_list

    state = await LocalListState.get_or_create(client.id)
    assert state.list_version == 1
    assert state.id_tags == [id_tag_value]


async def test_send_local_list_differential_update_merges(local_list_charge_point, id_tag_value):
    client = local_list_charge_point
    tag_a = id_tag_value
    tag_b = f"{id_tag_value}_b"
    await IdTag(id_tag=tag_a).insert()
    await IdTag(id_tag=tag_b).insert()

    await main.send_send_local_list(client.id, id_tags=[tag_a], update_type=UpdateType.full)
    new_version, response = await main.send_send_local_list(
        client.id, id_tags=[tag_b], remove_id_tags=[tag_a], update_type=UpdateType.differential
    )
    assert response.status == UpdateStatus.accepted
    assert new_version == 2
    assert client.local_list == {tag_b: {"status": "Accepted"}}

    state = await LocalListState.get_or_create(client.id)
    assert state.id_tags == [tag_b]


async def test_send_local_list_omitting_id_tags_on_full_update_sends_everything(
    local_list_charge_point, id_tag_value
):
    client = local_list_charge_point
    await IdTag(id_tag=id_tag_value).insert()

    _new_version, response = await main.send_send_local_list(client.id, update_type=UpdateType.full)
    assert response.status == UpdateStatus.accepted
    assert id_tag_value in client.local_list


async def test_send_local_list_version_mismatch_when_stale(local_list_charge_point):
    client = local_list_charge_point
    client.local_list_version = 5  # ahead of this Central System's own count of 0

    new_version, response = await main.send_send_local_list(client.id, update_type=UpdateType.full)
    assert new_version == 1  # this Central System still only ever counts up from its own record
    assert response.status == UpdateStatus.version_mismatch

    state = await LocalListState.get_or_create(client.id)
    assert state.list_version == 0  # never advanced: the push never actually took effect


async def test_send_local_list_rejected_by_charger_persists_nothing(
    local_list_charge_point, id_tag_value
):
    client = local_list_charge_point
    client.reject_send_local_list = True
    await IdTag(id_tag=id_tag_value).insert()

    _new_version, response = await main.send_send_local_list(
        client.id, id_tags=[id_tag_value], update_type=UpdateType.full
    )
    assert response.status == UpdateStatus.failed

    state = await LocalListState.get_or_create(client.id)
    assert state.list_version == 0
    assert state.id_tags == []


async def test_get_local_list_version_reports_current_version(local_list_charge_point):
    client = local_list_charge_point
    client.local_list_version = 3
    response = await main.send_get_local_list_version(client.id)
    assert response.list_version == 3


async def test_send_local_list_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_send_local_list(charge_point_identity)
        finally:
            listener.cancel()


async def test_get_local_list_version_forbidden_while_pending(server, charge_point_identity):
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
                await main.send_get_local_list_version(charge_point_identity)
        finally:
            listener.cancel()


async def test_send_local_list_when_not_connected_raises_clear_error(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_send_local_list(charge_point_identity)


async def test_get_local_list_version_when_not_connected_raises_clear_error(
    db, charge_point_identity
):
    await ChargePoint.register(charge_point_identity, registration_status="Accepted")
    with pytest.raises(RuntimeError, match="not currently connected"):
        await main.send_get_local_list_version(charge_point_identity)


# --------------------------------------------------------------------------------------------
# OCPP 1.6 s3.5: "the Charge Point is designed to operate stand-alone" -- these exercise the
# same offline-timestamp trust instructions/04-normal-charge-flow.md and 05's own tests already
# require, but in bulk, as this section's own instruction asks for, and the two related
# consequences it calls out: a transaction recorded for a now-refused tag, and StopTransaction
# arriving with no matching Start.
# --------------------------------------------------------------------------------------------


async def test_bulk_offline_transactions_trusted_with_old_timestamps(
    connected_charge_point, accepted_id_tag
):
    client = connected_charge_point
    # A charger reconnecting after an extended outage delivers every queued transaction from
    # the whole outage window at once, each with its own (old) timestamp -- not just one.
    sessions = []
    for hours_ago in (5, 4, 3, 2, 1):
        started_at = NOW - timedelta(hours=hours_ago)
        stopped_at = started_at + timedelta(minutes=20)
        start = await client.call(
            call.StartTransaction(
                connector_id=1, id_tag=accepted_id_tag, meter_start=0, timestamp=iso(started_at)
            ),
            suppress=False,
        )
        await client.call(
            call.StopTransaction(
                transaction_id=start.transaction_id, meter_stop=1000, timestamp=iso(stopped_at)
            ),
            suppress=False,
        )
        sessions.append((start.transaction_id, started_at, stopped_at))

    for transaction_id, started_at, stopped_at in sessions:
        tx = await Transaction.find_one(Transaction.transaction_id == transaction_id)
        assert tx.started_at == started_at
        assert tx.stopped_at == stopped_at
        assert tx.is_open is False


async def test_bulk_stop_without_matching_start_are_recorded_as_incomplete(
    connected_charge_point,
):
    client = connected_charge_point
    unseen_ids = [secrets.randbelow(1_000_000_000) + 1 for _ in range(5)]
    for unseen_id in unseen_ids:
        await client.call(
            call.StopTransaction(transaction_id=unseen_id, meter_stop=42, timestamp=iso(NOW)),
            suppress=False,
        )

    for unseen_id in unseen_ids:
        tx = await Transaction.find_one(Transaction.transaction_id == unseen_id)
        assert tx is not None
        assert tx.incomplete is True


async def test_transaction_recorded_for_tag_now_refused(connected_charge_point, id_tag_value):
    """"A transaction may be reported for a tag the Central System would now refuse. It
    happened; record it and let billing decide" -- 06-remaining-flows.md section F.
    """
    await IdTag(id_tag=id_tag_value, status=AuthorizationStatus.blocked).insert()
    client = connected_charge_point

    start = await client.call(
        call.StartTransaction(
            connector_id=1, id_tag=id_tag_value, meter_start=0, timestamp=iso(NOW)
        ),
        suppress=False,
    )
    assert start.id_tag_info["status"] == AuthorizationStatus.blocked

    tx = await Transaction.find_one(Transaction.transaction_id == start.transaction_id)
    assert tx is not None
    assert tx.id_tag == id_tag_value
