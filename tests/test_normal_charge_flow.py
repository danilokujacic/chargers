"""Tests for the normal charge flow: a driver taps a card, charges, and leaves.

See instructions/04-normal-charge-flow.md for the flow and instructions/05-normal-charge-flow-
tests.md for what these tests are required to prove. Every OCPP exchange here is real: a real
WebSocket connection to a real (in-process) Central System, real MongoDB documents asserted on
afterward -- not just the response payloads.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest
import websockets
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, datatypes
from ocpp.v16.enums import (
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    Measurand,
    Reason,
    UnitOfMeasure,
)
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from models import ChargePoint, ConnectorStatus, IdTag, Transaction
from ocpp_client_auth import basic_auth_header


def iso(dt):
    """An OCPP dateTime string for a Python datetime."""
    return dt.isoformat()


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------------------------


async def test_full_charge_session(connected_charge_point, accepted_id_tag):
    client = connected_charge_point
    id_tag = accepted_id_tag

    boot = await client.call(
        call.BootNotification(charge_point_model="TestModel", charge_point_vendor="TestVendor"),
        suppress=False,
    )
    assert boot.status == "Accepted"

    auth = await client.call(call.Authorize(id_tag=id_tag), suppress=False)
    assert auth.id_tag_info["status"] == "Accepted"

    prep = await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.preparing,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    assert prep is not None

    start_time = NOW + timedelta(seconds=5)
    start = await client.call(
        call.StartTransaction(
            connector_id=1, id_tag=id_tag, meter_start=1000, timestamp=iso(start_time)
        ),
        suppress=False,
    )
    assert start.id_tag_info["status"] == "Accepted"
    assert isinstance(start.transaction_id, int) and start.transaction_id > 0
    transaction_id = start.transaction_id

    charging = await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=iso(start_time),
        ),
        suppress=False,
    )
    assert charging is not None

    for minutes, energy in ((10, 500), (20, 1200)):
        meter_resp = await client.call(
            call.MeterValues(
                connector_id=1,
                transaction_id=transaction_id,
                meter_value=[
                    datatypes.MeterValue(
                        timestamp=iso(start_time + timedelta(minutes=minutes)),
                        sampled_value=[
                            datatypes.SampledValue(
                                value=str(1000 + energy),
                                measurand=Measurand.energy_active_import_register,
                                unit=UnitOfMeasure.wh,
                            )
                        ],
                    )
                ],
            ),
            suppress=False,
        )
        assert meter_resp is not None

    stop_time = start_time + timedelta(minutes=30)
    stop = await client.call(
        call.StopTransaction(
            transaction_id=transaction_id,
            meter_stop=2200,
            timestamp=iso(stop_time),
            id_tag=id_tag,
            reason=Reason.local,
        ),
        suppress=False,
    )
    assert stop.id_tag_info["status"] == "Accepted"

    finishing = await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.finishing,
            timestamp=iso(stop_time),
        ),
        suppress=False,
    )
    assert finishing is not None
    available = await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.available,
            timestamp=iso(stop_time + timedelta(seconds=10)),
        ),
        suppress=False,
    )
    assert available is not None

    # The documents, not just the responses: a handler can answer correctly and write nothing.
    tx = await Transaction.find_one(Transaction.transaction_id == transaction_id)
    assert tx is not None
    assert tx.charge_point_identity == client.id
    assert tx.connector_id == 1
    assert tx.id_tag == id_tag
    assert tx.meter_start == 1000
    assert tx.meter_stop == 2200
    assert tx.started_at == start_time
    assert tx.stopped_at == stop_time
    assert tx.is_open is False
    assert len(tx.meter_values) == 2

    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.available


async def test_transaction_id_is_allocated_not_hardcoded(
    server, registered_charge_point, accepted_id_tag
):
    identity_a, key_a = registered_charge_point
    identity_b = f"{identity_a}_B"
    _record_b, key_b = await ChargePoint.register(
        identity_b, registration_status="Accepted"
    )

    async def start_one(identity, key):
        header = basic_auth_header(identity, key)
        async with websockets.connect(
            f"{server}/{identity}",
            subprotocols=[Subprotocol("ocpp1.6")],
            additional_headers={"Authorization": header},
        ) as ws:
            client = cp(identity, ws)
            listener = asyncio.create_task(client.start())
            try:
                start = await client.call(
                    call.StartTransaction(
                        connector_id=1,
                        id_tag=accepted_id_tag,
                        meter_start=0,
                        timestamp=iso(NOW),
                    ),
                    suppress=False,
                )
                return start.transaction_id
            finally:
                listener.cancel()

    id_a = await start_one(identity_a, key_a)
    id_b = await start_one(identity_b, key_b)

    assert isinstance(id_a, int) and id_a > 0
    assert isinstance(id_b, int) and id_b > 0
    assert id_a != id_b


async def test_energy_is_recorded(connected_charge_point, accepted_id_tag):
    client = connected_charge_point
    start = await client.call(
        call.StartTransaction(
            connector_id=1, id_tag=accepted_id_tag, meter_start=2000, timestamp=iso(NOW)
        ),
        suppress=False,
    )
    energy_delivered = 3456
    await client.call(
        call.StopTransaction(
            transaction_id=start.transaction_id,
            meter_stop=2000 + energy_delivered,
            timestamp=iso(NOW + timedelta(hours=1)),
        ),
        suppress=False,
    )
    tx = await Transaction.find_one(Transaction.transaction_id == start.transaction_id)
    assert tx.meter_stop - tx.meter_start == energy_delivered


async def test_connector_status_persisted(connected_charge_point):
    client = connected_charge_point
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.preparing,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.available,
            timestamp=iso(NOW + timedelta(minutes=1)),
        ),
        suppress=False,
    )
    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.available

    connector_zero = await ConnectorStatus.find_one(
        ConnectorStatus.charge_point_identity == client.id, ConnectorStatus.connector_id == 0
    )
    assert connector_zero is None  # never reported on, so never created


# --------------------------------------------------------------------------------------------
# Authorization of the driver
# --------------------------------------------------------------------------------------------


async def test_unknown_id_tag_is_invalid(connected_charge_point, id_tag_value):
    auth = await connected_charge_point.call(call.Authorize(id_tag=id_tag_value), suppress=False)
    assert auth.id_tag_info["status"] == "Invalid"


async def test_blocked_id_tag_is_refused(connected_charge_point, id_tag_value):
    await IdTag(id_tag=id_tag_value, status=AuthorizationStatus.blocked).insert()
    auth = await connected_charge_point.call(call.Authorize(id_tag=id_tag_value), suppress=False)
    assert auth.id_tag_info["status"] == "Blocked"


async def test_expired_id_tag_is_expired(connected_charge_point, id_tag_value):
    past = NOW - timedelta(days=1)
    await IdTag(
        id_tag=id_tag_value, status=AuthorizationStatus.accepted, expiry_date=past
    ).insert()
    auth = await connected_charge_point.call(call.Authorize(id_tag=id_tag_value), suppress=False)
    assert auth.id_tag_info["status"] == "Expired"


async def test_parent_group_can_stop_transaction(connected_charge_point, id_tag_value):
    client = connected_charge_point
    # parentIdTag is CiString20 (max 20 chars, per the OCPP schema): short, plain suffixes.
    parent = f"P_{id_tag_value[-8:]}"
    tag_a = f"A_{id_tag_value[-8:]}"
    tag_b = f"B_{id_tag_value[-8:]}"
    tag_c = f"C_{id_tag_value[-8:]}"
    await IdTag(id_tag=tag_a, parent_id_tag=parent).insert()
    await IdTag(id_tag=tag_b, parent_id_tag=parent).insert()
    await IdTag(id_tag=tag_c).insert()  # no parent group -- an unrelated tag

    start1 = await client.call(
        call.StartTransaction(connector_id=1, id_tag=tag_a, meter_start=0, timestamp=iso(NOW)),
        suppress=False,
    )
    stop1 = await client.call(
        call.StopTransaction(
            transaction_id=start1.transaction_id,
            meter_stop=100,
            timestamp=iso(NOW + timedelta(minutes=10)),
            id_tag=tag_b,
        ),
        suppress=False,
    )
    assert stop1.id_tag_info["status"] == "Accepted"
    tx1 = await Transaction.find_one(Transaction.transaction_id == start1.transaction_id)
    assert tx1.is_open is False

    start2 = await client.call(
        call.StartTransaction(connector_id=1, id_tag=tag_a, meter_start=0, timestamp=iso(NOW)),
        suppress=False,
    )
    stop2 = await client.call(
        call.StopTransaction(
            transaction_id=start2.transaction_id,
            meter_stop=50,
            timestamp=iso(NOW + timedelta(minutes=5)),
            id_tag=tag_c,
        ),
        suppress=False,
    )
    assert stop2.id_tag_info["status"] == "Invalid"


# --------------------------------------------------------------------------------------------
# State machine integration
# --------------------------------------------------------------------------------------------


async def test_illegal_transition_is_logged_but_persisted(connected_charge_point, caplog):
    client = connected_charge_point
    # Available -> Preparing -> Charging -> Finishing is legal; Finishing -> Charging is not
    # (see connector_state_machine's transition table: no such entry exists).
    for status in (
        ChargePointStatus.preparing,
        ChargePointStatus.charging,
        ChargePointStatus.finishing,
    ):
        await client.call(
            call.StatusNotification(
                connector_id=1,
                error_code=ChargePointErrorCode.no_error,
                status=status,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )

    caplog.clear()
    result = await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    assert result is not None  # no CALLError -- the message is accepted regardless

    assert any(
        "ILLEGAL STATUS TRANSITION" in record.message for record in caplog.records
    ), [r.message for r in caplog.records]

    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.status == ChargePointStatus.charging  # recorded despite being illegal


async def test_connector_zero_rejects_charging_status(connected_charge_point, caplog):
    client = connected_charge_point
    caplog.clear()
    result = await client.call(
        call.StatusNotification(
            connector_id=0,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    assert result is not None
    assert any(
        "connector 0" in record.message and "Charging" in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]


async def test_suspend_and_resume_keeps_one_transaction(connected_charge_point, accepted_id_tag):
    client = connected_charge_point
    start = await client.call(
        call.StartTransaction(
            connector_id=1, id_tag=accepted_id_tag, meter_start=0, timestamp=iso(NOW)
        ),
        suppress=False,
    )
    for status in (
        ChargePointStatus.charging,
        ChargePointStatus.suspended_ev,
        ChargePointStatus.charging,
    ):
        await client.call(
            call.StatusNotification(
                connector_id=1,
                error_code=ChargePointErrorCode.no_error,
                status=status,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )

    open_transactions = await Transaction.find(
        Transaction.charge_point_identity == client.id,
        Transaction.connector_id == 1,
        Transaction.is_open == True,  # noqa: E712
    ).to_list()
    assert len(open_transactions) == 1
    assert open_transactions[0].transaction_id == start.transaction_id


# --------------------------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------------------------


async def test_duplicate_stop_transaction_is_idempotent(connected_charge_point, accepted_id_tag):
    client = connected_charge_point
    start = await client.call(
        call.StartTransaction(
            connector_id=1, id_tag=accepted_id_tag, meter_start=100, timestamp=iso(NOW)
        ),
        suppress=False,
    )
    stop_time = NOW + timedelta(minutes=15)
    await client.call(
        call.StopTransaction(
            transaction_id=start.transaction_id, meter_stop=900, timestamp=iso(stop_time)
        ),
        suppress=False,
    )
    tx_after_first = await Transaction.find_one(Transaction.transaction_id == start.transaction_id)
    snapshot = tx_after_first.model_dump()

    # Redeliver with DIFFERENT values, as a reconnecting charger might: the first delivery wins.
    await client.call(
        call.StopTransaction(
            transaction_id=start.transaction_id,
            meter_stop=999999,
            timestamp=iso(NOW + timedelta(days=1)),
        ),
        suppress=False,
    )
    tx_after_second = await Transaction.find_one(Transaction.transaction_id == start.transaction_id)
    assert tx_after_second.model_dump() == snapshot


async def test_stop_for_unknown_transaction_is_recorded(connected_charge_point):
    client = connected_charge_point
    unseen_id = secrets.randbelow(1_000_000_000) + 1
    result = await client.call(
        call.StopTransaction(transaction_id=unseen_id, meter_stop=42, timestamp=iso(NOW)),
        suppress=False,
    )
    assert result is not None
    tx = await Transaction.find_one(Transaction.transaction_id == unseen_id)
    assert tx is not None
    assert tx.incomplete is True
    assert tx.meter_stop == 42


async def test_offline_timestamps_are_trusted(connected_charge_point, accepted_id_tag):
    client = connected_charge_point
    # datetime.now(UTC) carries microseconds; MongoDB's BSON datetime only stores millisecond
    # precision, so comparing a round-tripped value against a microsecond-precise "now" would
    # fail on precision alone. NOW (used throughout this file) has none, so the round trip is
    # exact -- the point under test is the two-hour gap, not sub-millisecond precision.
    long_ago_start = NOW - timedelta(hours=2)
    long_ago_stop = long_ago_start + timedelta(minutes=45)

    start = await client.call(
        call.StartTransaction(
            connector_id=1,
            id_tag=accepted_id_tag,
            meter_start=0,
            timestamp=iso(long_ago_start),
        ),
        suppress=False,
    )
    stop = await client.call(
        call.StopTransaction(
            transaction_id=start.transaction_id,
            meter_stop=5000,
            timestamp=iso(long_ago_stop),
        ),
        suppress=False,
    )
    assert stop is not None  # not rejected for being "too old"

    tx = await Transaction.find_one(Transaction.transaction_id == start.transaction_id)
    assert tx.started_at == long_ago_start
    assert tx.stopped_at == long_ago_stop


async def test_unauthenticated_connection_is_refused(server, registered_charge_point):
    identity, _key = registered_charge_point
    with pytest.raises(InvalidStatus) as exc_info:
        async with websockets.connect(
            f"{server}/{identity}", subprotocols=[Subprotocol("ocpp1.6")]
        ):
            pass
    assert exc_info.value.response.status_code == 401

    assert await Transaction.find_one(Transaction.charge_point_identity == identity) is None
    assert (
        await ConnectorStatus.find_one(ConnectorStatus.charge_point_identity == identity) is None
    )


async def test_wrong_key_is_refused(server, registered_charge_point):
    identity, _key = registered_charge_point
    wrong_key = secrets.token_bytes(20).hex().upper()
    header = basic_auth_header(identity, wrong_key)
    with pytest.raises(InvalidStatus) as exc_info:
        async with websockets.connect(
            f"{server}/{identity}",
            subprotocols=[Subprotocol("ocpp1.6")],
            additional_headers={"Authorization": header},
        ):
            pass
    assert exc_info.value.response.status_code == 401


async def test_identity_mismatch_is_forbidden(server, registered_charge_point):
    identity_a, key_a = registered_charge_point
    identity_b = f"{identity_a}_OTHER"
    await ChargePoint.register(identity_b, registration_status="Accepted")
    # Valid credentials for A, presented on B's connection URL.
    header = basic_auth_header(identity_a, key_a)
    with pytest.raises(InvalidStatus) as exc_info:
        async with websockets.connect(
            f"{server}/{identity_b}",
            subprotocols=[Subprotocol("ocpp1.6")],
            additional_headers={"Authorization": header},
        ):
            pass
    assert exc_info.value.response.status_code == 403
