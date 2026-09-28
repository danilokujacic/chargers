"""Tests for fault handling: a connector reports something wrong, an operator needs to see it
happened even after it clears, and recovery has to land back exactly where it was, not
somewhere guessed.

See instructions/06-remaining-flows.md section G. `connector_state_machine.recover_from_fault()`
and the Faulted/recovery transitions themselves (A9/.../H9, I1-I8) already existed and were
already tested (doc01/04); this section's new surface is main.get_fault_history() and the
FaultEvent document it reads, both exercised here with a plain OCPP client sending
StatusNotification directly -- there is no Central-System-initiated fault message to test
against a charger double, since real hardware detects its own faults.
"""

from datetime import UTC, datetime

from ocpp.v16 import call
from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus

import main
from models import ConnectorStatus

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


async def test_entering_fault_opens_an_event(connected_charge_point):
    client = connected_charge_point
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.high_temperature,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    events = await main.get_fault_history(client.id, connector_id=1)
    assert len(events) == 1
    assert events[0].error_code == ChargePointErrorCode.high_temperature
    assert events[0].cleared_at is None


async def test_recovering_closes_the_event(connected_charge_point):
    client = connected_charge_point
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.reader_failure,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    # Available -> Faulted (A9) -> Available (I1): the connector's own pre-fault status.
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.available,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    events = await main.get_fault_history(client.id, connector_id=1)
    assert len(events) == 1
    assert events[0].cleared_at is not None


async def test_repeated_faulted_report_does_not_open_a_second_event(connected_charge_point):
    client = connected_charge_point
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.over_voltage,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    # Still Faulted, but now reporting a different error_code -- a real quirk (escalating
    # fault), not a fresh fault: must not open a second event.
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.under_voltage,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    events = await main.get_fault_history(client.id, connector_id=1)
    assert len(events) == 1
    assert events[0].cleared_at is None
    # The connector's live status record still reflects the latest error_code, even though no
    # new fault history entry was opened for it.
    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.error_code == ChargePointErrorCode.under_voltage


async def test_pre_fault_status_persists_for_recovery(connected_charge_point):
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
            error_code=ChargePointErrorCode.ev_communication_error,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    # OCPP 1.6 s4.9: "Connector set to Unavailable shall persist a reboot" is B's own concern,
    # but pre_fault_status is this section's equivalent persistence requirement -- stored on
    # ConnectorStatus so a Central System restart does not lose what to recover back to.
    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.pre_fault_status == ChargePointStatus.preparing

    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.preparing,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    status = await ConnectorStatus.get_or_create(client.id, 1)
    assert status.pre_fault_status is None


async def test_connector_zero_fault_means_whole_unit(connected_charge_point):
    client = connected_charge_point
    await client.call(
        call.StatusNotification(
            connector_id=0,
            error_code=ChargePointErrorCode.internal_error,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    events = await main.get_fault_history(client.id, connector_id=0)
    assert len(events) == 1
    assert events[0].connector_id == 0
    assert events[0].error_code == ChargePointErrorCode.internal_error


async def test_multiple_connectors_fault_independently(connected_charge_point):
    client = connected_charge_point
    await client.call(
        call.StatusNotification(
            connector_id=1,
            error_code=ChargePointErrorCode.ground_failure,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    await client.call(
        call.StatusNotification(
            connector_id=2,
            error_code=ChargePointErrorCode.weak_signal,
            status=ChargePointStatus.faulted,
            timestamp=iso(NOW),
        ),
        suppress=False,
    )
    all_events = await main.get_fault_history(client.id)
    assert len(all_events) == 2
    connector_1_events = await main.get_fault_history(client.id, connector_id=1)
    assert len(connector_1_events) == 1
    assert connector_1_events[0].error_code == ChargePointErrorCode.ground_failure


async def test_get_fault_history_is_newest_first(connected_charge_point):
    client = connected_charge_point
    for error_code in (ChargePointErrorCode.other_error, ChargePointErrorCode.power_meter_failure):
        await client.call(
            call.StatusNotification(
                connector_id=1, error_code=error_code, status=ChargePointStatus.faulted,
                timestamp=iso(NOW),
            ),
            suppress=False,
        )
        await client.call(
            call.StatusNotification(
                connector_id=1, error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.available, timestamp=iso(NOW),
            ),
            suppress=False,
        )
    events = await main.get_fault_history(client.id, connector_id=1)
    assert len(events) == 2
    assert events[0].error_code == ChargePointErrorCode.power_meter_failure
    assert events[1].error_code == ChargePointErrorCode.other_error


async def test_fault_history_is_a_pure_read_that_works_without_a_connection(
    db, charge_point_identity
):
    events = await main.get_fault_history(charge_point_identity)
    assert events == []
