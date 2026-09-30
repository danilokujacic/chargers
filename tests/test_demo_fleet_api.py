"""Public API tests for the demo fleet's data (instructions/11-demo-fleet.md §F).

REST through httpx.ASGITransport on the real app, as in test_api_rest.py; the WebSocket test runs
a real uvicorn server with real Redis, as in test_api_websocket.py. Every site and charger here
is created fresh, with a unique identity.
"""

import asyncio
import json

import httpx
import pytest_asyncio
import redis.asyncio as redis_asyncio
import uvicorn
import websockets
from ocpp.v16.enums import ChargePointStatus

from api.app import app
from api.config import events_channel, redis_url
from models import (
    ChargePoint,
    ConnectorSpec,
    ConnectorStatus,
    PowerType,
    Site,
    SiteSource,
    SiteType,
    init_db,
)

S = ChargePointStatus


@pytest_asyncio.fixture
async def client(db):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
        yield c


async def make_site(source=SiteSource.simulated, **extra):
    site = Site(
        name="Demo site", site_type=SiteType.other, latitude=42.4, longitude=19.2,
        source=source, external_id="ps-test", external_charge_point_count=9, **extra,
    )
    await site.insert()
    return site


def spec(connector_id, connector_type="CCS2", power_type=PowerType.DC, max_power_kw=200.0):
    return ConnectorSpec(
        connector_id=connector_id, connector_type=connector_type, power_type=power_type,
        max_power_kw=max_power_kw,
    )


async def make_charger(identity, site, statuses=(), specs=(), simulated=True):
    record, _key = await ChargePoint.register(
        identity, simulated=simulated, site_id=site.id, connector_specs=list(specs)
    )
    for connector_id, status in enumerate(statuses, start=1):
        await ConnectorStatus(
            charge_point_identity=identity, connector_id=connector_id, status=status
        ).insert()
    return record


async def summary_for(client, site_id):
    response = await client.get("/api/v1/sites")
    assert response.status_code == 200
    return next(s for s in response.json() if s["id"] == site_id)


async def test_simulated_site_is_live_and_lists_its_chargers(client, charge_point_identity):
    site = await make_site()
    await make_charger(charge_point_identity + "_B", site, [S.charging], [spec(1)])
    await make_charger(charge_point_identity + "_A", site, [S.available], [spec(1)])

    summary = await summary_for(client, str(site.id))
    # Live, like an operator site: counted from real chargers (not the stored 9), not "unknown".
    assert summary["source"] == "simulated"
    assert summary["aggregate_status"] == "available"
    assert summary["charge_point_count"] == 2
    detail = (await client.get(f"/api/v1/sites/{site.id}")).json()
    assert [cp["identity"] for cp in detail["charge_points"]] == [
        charge_point_identity + "_A", charge_point_identity + "_B",
    ]


async def test_simulated_site_aggregates_faulted_and_unreported(client, charge_point_identity):
    broken = await make_site()
    await make_charger(charge_point_identity + "_F", broken, [S.faulted], [spec(1)])
    silent = await make_site()
    await make_charger(charge_point_identity + "_S", silent, [], [spec(1)])

    assert (await summary_for(client, str(broken.id)))["aggregate_status"] == "faulted"
    assert (await summary_for(client, str(silent.id)))["aggregate_status"] == "unavailable"


async def test_connector_specs_are_merged_into_connector_out(client, charge_point_identity):
    site = await make_site()
    await make_charger(
        charge_point_identity, site, [S.charging, S.available],
        [spec(1), spec(2, "Type 2", PowerType.AC, None)],
    )

    detail = (await client.get(f"/api/v1/sites/{site.id}")).json()
    assert detail["charge_points"][0]["connectors"] == [
        {
            "connector_id": 1, "status": "Charging", "error_code": "NoError",
            "connector_type": "CCS2", "power_type": "DC", "max_power_kw": 200.0,
        },
        {
            "connector_id": 2, "status": "Available", "error_code": "NoError",
            "connector_type": "Type 2", "power_type": "AC", "max_power_kw": None,
        },
    ]
    one = (await client.get(f"/api/v1/charge-points/{charge_point_identity}")).json()
    assert one["connectors"] == detail["charge_points"][0]["connectors"]


async def test_spec_only_connector_is_listed_as_unavailable(client, charge_point_identity):
    site = await make_site()
    await make_charger(charge_point_identity, site, [S.charging], [spec(1), spec(2)])

    (charger,) = (await client.get(f"/api/v1/sites/{site.id}")).json()["charge_points"]
    assert [(c["connector_id"], c["status"], c["error_code"]) for c in charger["connectors"]] == [
        (1, "Charging", "NoError"), (2, "Unavailable", "NoError"),
    ]
    assert charger["connectors"][1]["connector_type"] == "CCS2"


async def test_charger_without_specs_is_unchanged(client, charge_point_identity):
    site = await make_site(source=SiteSource.operator)
    await make_charger(charge_point_identity, site, [S.available], simulated=False)

    (charger,) = (await client.get(f"/api/v1/sites/{site.id}")).json()["charge_points"]
    assert charger["connectors"] == [
        {
            "connector_id": 1, "status": "Available", "error_code": "NoError",
            "connector_type": None, "power_type": None, "max_power_kw": None,
        }
    ]
    one = (await client.get(f"/api/v1/charge-points/{charge_point_identity}")).json()
    assert one["connectors"] == charger["connectors"]


@pytest_asyncio.fixture
async def live_api(db, redis_available, test_database_name, monkeypatch):
    """The real app on a real uvicorn server, its lifespan (and Redis subscriber) included."""
    monkeypatch.setenv("MONGODB_DB", test_database_name)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}"
    server.should_exit = True
    await task
    # The app's lifespan closed the client Beanie was bound to; rebind for later tests.
    await init_db(database=test_database_name)


async def test_site_websocket_accepts_a_simulated_site(live_api, charge_point_identity):
    site = await make_site()
    await make_charger(charge_point_identity, site, [S.available], [spec(1)])
    publisher = redis_asyncio.Redis.from_url(redis_url())
    try:
        async with websockets.connect(f"{live_api}/api/v1/ws/sites/{site.id}") as ws:
            initial = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert initial["source"] == "simulated"
            assert initial["charge_points"][0]["identity"] == charge_point_identity
            event = {
                "type": "connector_status",
                "charge_point_identity": charge_point_identity,
                "connector_id": 1,
                "at": "2026-09-29T10:00:00+00:00",
                "data": {"status": "Charging", "error_code": "NoError"},
            }
            await publisher.publish(events_channel(), json.dumps(event))
            assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == event
    finally:
        await publisher.aclose()
