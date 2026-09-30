"""End-to-end tests for run_demo_fleet.py (instructions/11-demo-fleet.md §E).

The fleet runs in-process against the real Central System (the `server` fixture), over real
OCPP-J connections, at --time-scale 0.01 so a five-minute session takes a few seconds. Each test
seeds its own synthetic site through the real import and seed code, with unique PlugShare ids.
Assertions read MongoDB, i.e. what main.py recorded from the fleet's messages.
"""

import asyncio
import itertools
import json
import random
import time

import pytest
from ocpp.v16.enums import ChargePointStatus, Reason

from import_plugshare_sites import import_entries
from models import ConnectorStatus, FaultEvent, Transaction
from run_demo_fleet import load_manifest, parse_args, run_fleet
from seed_demo_fleet import seed

_ids = itertools.count(random.randrange(80_000_000, 99_000_000, 1000))


def outlet(code=7, power_type="AC", kilowatts=None, status=None):
    return {
        "connector": code, "id": next(_ids), "is_dc": power_type == "DC",
        "kilowatts": kilowatts, "power": 0, "power_type": power_type, "status": status,
    }


async def seed_site(tmp_path, *stations):
    """Import and seed one synthetic location; returns (manifest chargers, identities)."""
    location_id = next(_ids)
    export = [{
        "id": location_id, "name": f"Fleet test {location_id}", "address": None,
        "latitude": 42.5, "longitude": 19.2, "connector_types": ["Type 2"],
        "url": None, "coming_soon": False,
        "stations": [{"id": next(_ids), "outlets": list(outlets)} for outlets in stations],
    }]
    await import_entries(export)
    manifest = tmp_path / "manifest.json"
    await seed(export, manifest)
    chargers = load_manifest(manifest)
    return chargers, [c["identity"] for c in chargers]


def fleet_options(server, tmp_path, *extra):
    return parse_args([
        "--url", server,
        "--manifest", str(tmp_path / "manifest.json"),
        "--state-file", str(tmp_path / "state.json"),
        "--time-scale", "0.01",
        "--connect-rate", "50",
        "--seed", "11",
        *extra,
    ])


async def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not met within {timeout}s")


def transactions_of(identities):
    return Transaction.find({"charge_point_identity": {"$in": list(identities)}})


async def statuses_of(identities):
    records = await ConnectorStatus.find(
        {"charge_point_identity": {"$in": list(identities)}}
    ).to_list()
    return {(r.charge_point_identity, r.connector_id): r for r in records}


async def test_fleet_runs_sessions_and_shuts_down_gracefully(server, tmp_path):
    chargers, (working, broken) = await seed_site(
        tmp_path,
        [outlet(7, kilowatts=22.0), outlet(20, "DC", 50.0)],
        [outlet(7, status="OUTOFORDER")],
    )
    options = fleet_options(
        server, tmp_path, "--session-minutes", "1", "2", "--idle-minutes", "1", "2",
        "--initial-busy", "1.0",
    )
    stop = asyncio.Event()
    fleet = asyncio.create_task(run_fleet(chargers, options, stop))

    async def a_metered_session_completed():
        closed = await transactions_of([working]).find(
            {"is_open": False, "stop_reason": Reason.local.value}
        ).to_list()
        return any(t.meter_stop > t.meter_start for t in closed)

    try:
        await wait_until(a_metered_session_completed)
        # Every connector reported, the out-of-order one as Faulted, while running.
        running = await statuses_of([working, broken])
        assert set(running) == {(working, 1), (working, 2), (broken, 1)}
        assert running[(broken, 1)].status == ChargePointStatus.faulted
        assert running[(broken, 1)].info == "Out of order (PlugShare report)"
    finally:
        stop.set()
        result = await asyncio.wait_for(fleet, 25)

    assert result["sessions_started"] >= 2
    assert await transactions_of([working, broken]).find({"is_open": True}).count() == 0
    for transaction in await transactions_of([working]).to_list():
        assert transaction.meter_stop >= transaction.meter_start
    final = await statuses_of([working, broken])
    assert final[(working, 1)].status == ChargePointStatus.unavailable
    assert final[(working, 2)].status == ChargePointStatus.unavailable
    # The broken connector stays Faulted, and its fault is still open: moving it at shutdown
    # would have closed the FaultEvent.
    assert final[(broken, 1)].status == ChargePointStatus.faulted
    (fault,) = await FaultEvent.history_for(broken)
    assert fault.cleared_at is None
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["fleet_alive_at"] is None
    assert state["chargers"][working]["open_transactions"] == {}
    assert set(state["chargers"][working]["registers"]) <= {"1", "2"}


async def test_hard_kill_leftovers_are_closed_with_power_loss(server, tmp_path):
    chargers, (identity,) = await seed_site(tmp_path, [outlet(), outlet()])
    stop = asyncio.Event()
    # Sessions far longer than this run, so both are still open when it is "killed".
    long_sessions = fleet_options(
        server, tmp_path, "--session-minutes", "60", "60", "--initial-busy", "1.0",
    )
    fleet = asyncio.create_task(
        run_fleet(chargers, long_sessions, stop, graceful_shutdown=False)
    )

    async def both_charging():
        return await transactions_of([identity]).find({"is_open": True}).count() == 2

    try:
        await wait_until(both_charging)
        # Let the state file catch up (it is written at most once a second).
        await asyncio.sleep(1.5)
    finally:
        stop.set()
        await asyncio.wait_for(fleet, 25)

    leftovers = await transactions_of([identity]).find({"is_open": True}).to_list()
    assert len(leftovers) == 2
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["fleet_alive_at"] is not None
    assert sorted(state["chargers"][identity]["open_transactions"].values()) == sorted(
        t.transaction_id for t in leftovers
    )

    leftover_ids = {t.transaction_id for t in leftovers}
    stop = asyncio.Event()
    short_sessions = fleet_options(
        server, tmp_path, "--session-minutes", "1", "2", "--initial-busy", "1.0",
    )
    fleet = asyncio.create_task(run_fleet(chargers, short_sessions, stop))

    async def sessions_resumed():
        started = await transactions_of([identity]).find(
            {"transaction_id": {"$nin": list(leftover_ids)}}
        ).count()
        return started >= 2

    try:
        await wait_until(sessions_resumed)
    finally:
        stop.set()
        result = await asyncio.wait_for(fleet, 25)

    assert result["leftovers_closed"] == 2
    for transaction_id in leftover_ids:
        closed = await Transaction.find_one(Transaction.transaction_id == transaction_id)
        assert closed.is_open is False
        assert closed.stop_reason == Reason.power_loss
        assert closed.stopped_by_id_tag is None
        assert closed.meter_stop >= closed.meter_start
    # The same idTags started again: no ConcurrentTx, so the leftovers really were closed.
    resumed = await transactions_of([identity]).find(
        {"transaction_id": {"$nin": list(leftover_ids)}}
    ).to_list()
    assert {t.id_tag for t in resumed} == {t.id_tag for t in leftovers}
    assert await transactions_of([identity]).find({"is_open": True}).count() == 0


@pytest.mark.parametrize("argv", [["--session-minutes", "5", "3"], ["--initial-busy", "2"]])
def test_parse_args_rejects_nonsense(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
