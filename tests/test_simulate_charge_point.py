"""Unit tests for simulate_charge_point.SimulatedChargePoint's multi-connector bookkeeping
(instructions/11-demo-fleet.md §D).

No server and no database: __init__ never touches the connection, so a dummy object stands in,
and these methods only read and write the simulator's own local state.
"""

from ocpp.v16.enums import ChargePointStatus, RemoteStartStopStatus

from simulate_charge_point import SimulatedChargePoint

KEY = "0" * 40


def charger(**kwargs):
    return SimulatedChargePoint("UNIT", object(), authorization_key=KEY, **kwargs)


def test_number_of_connectors_sets_physical_connectors_and_configuration():
    three = charger(number_of_connectors=3)
    assert three.physical_connectors() == {1, 2, 3}
    assert three.configuration["NumberOfConnectors"] == ("3", True)


def test_default_is_one_connector_as_before():
    one = charger()
    assert one.physical_connectors() == {1}
    assert one.configuration["NumberOfConnectors"] == ("1", True)


def test_busy_connectors_covers_open_pending_remote_and_local_starts():
    cp = charger(number_of_connectors=4)
    cp.open_transactions[1] = 101
    cp.pending_remote_starts["TAG"] = 2
    cp.pending_local_starts.add(3)
    assert cp.busy_connectors() == {1, 2, 3}


def test_remote_start_without_connector_picks_the_lowest_free_available_one():
    cp = charger(number_of_connectors=3)
    cp.open_transactions[1] = 101
    cp.get_connector_state(1).change_to(ChargePointStatus.charging)
    cp.get_connector_state(2).change_to(ChargePointStatus.unavailable)

    conf = cp.on_remote_start_transaction(id_tag="DEMO-REMOTE")

    assert conf.status == RemoteStartStopStatus.accepted
    # 1 is busy, 2 is not Available: 3 is the one, and it is now held for the @after half.
    assert cp.pending_remote_starts == {"DEMO-REMOTE": 3}
    assert 3 in cp.busy_connectors()


def test_remote_start_falls_back_to_the_default_connector():
    cp = charger(number_of_connectors=2)
    for connector_id in (1, 2):
        cp.get_connector_state(connector_id).change_to(ChargePointStatus.unavailable)
    cp.on_remote_start_transaction(id_tag="TAG")
    assert cp.pending_remote_starts == {"TAG": 1}


def test_remote_start_on_a_busy_connector_is_rejected():
    # A single-connector charger mid-session: the default-connector fallback must not start a
    # second transaction on top of the first.
    cp = charger()
    cp.open_transactions[1] = 101
    cp.get_connector_state(1).change_to(ChargePointStatus.charging)
    assert cp.on_remote_start_transaction(id_tag="TAG").status == RemoteStartStopStatus.rejected
    assert cp.on_remote_start_transaction(id_tag="TAG", connector_id=1).status == (
        RemoteStartStopStatus.rejected
    )
    assert cp.pending_remote_starts == {}


def test_rejected_remote_start_records_nothing():
    cp = charger(reject_remote_start=True)
    conf = cp.on_remote_start_transaction(id_tag="TAG")
    assert conf.status == RemoteStartStopStatus.rejected
    assert cp.pending_remote_starts == {}
