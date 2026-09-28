"""WebSocket tests for the public API (instructions/08-public-api-service.md §G, §K).

Everything is real: MongoDB, Redis, a real uvicorn server on an OS-assigned port running the
real api.app (lifespan included, so the real Redis subscriber is what delivers events), and a
real WebSocket client. Events are driven through Redis exactly the way main.py publishes them.
"""

import asyncio
import json

import pytest
import pytest_asyncio
import redis.asyncio as redis_asyncio
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from api.app import app
from api.config import events_channel, redis_url
from models import ChargePoint, Site, SiteSource, SiteType, init_db


async def _start_server():
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"ws://127.0.0.1:{port}"


async def _stop_server(server, task, test_database_name):
    server.should_exit = True
    await task
    # The app's lifespan closed the client Beanie was bound to; rebind for later tests.
    await init_db(database=test_database_name)


@pytest_asyncio.fixture
async def live_api(db, redis_available, test_database_name, monkeypatch):
    monkeypatch.setenv("MONGODB_DB", test_database_name)
    server, task, base = await _start_server()
    yield base
    await _stop_server(server, task, test_database_name)


@pytest_asyncio.fixture
async def redis_client(redis_available):
    client = redis_asyncio.Redis.from_url(redis_url())
    yield client
    await client.aclose()


async def publish(redis_client, identity, event_type="connector_status", **data):
    event = {
        "type": event_type,
        "charge_point_identity": identity,
        "connector_id": 1,
        "at": "2026-09-28T18:14:10+00:00",
        "data": data,
    }
    await redis_client.publish(events_channel(), json.dumps(event))
    return event


async def recv_json(ws, timeout=5):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def site_with_charger(identity):
    site = Site(name="S", site_type=SiteType.hotel, latitude=42.0, longitude=19.0)
    await site.insert()
    record, _key = await ChargePoint.register(identity)
    await record.set_location(site_id=site.id)
    return site


async def test_site_socket_gets_initial_state_then_only_its_own_events(
    live_api, redis_client, charge_point_identity
):
    site = await site_with_charger(charge_point_identity)
    async with websockets.connect(f"{live_api}/api/v1/ws/sites/{site.id}") as ws:
        initial = await recv_json(ws)
        assert initial["id"] == str(site.id)
        assert initial["charge_points"][0]["identity"] == charge_point_identity

        # An unrelated charger's event first: it must never arrive; ours must, in order.
        await publish(redis_client, "SOMEONE_ELSE", status="Faulted")
        first = await publish(redis_client, charge_point_identity, status="Charging")
        second = await publish(redis_client, charge_point_identity, status="Available")
        assert await recv_json(ws) == first
        assert await recv_json(ws) == second


async def test_charge_point_socket_filters_by_identity(
    live_api, redis_client, charge_point_identity
):
    await ChargePoint.register(charge_point_identity)
    async with websockets.connect(f"{live_api}/api/v1/ws/charge-points/{charge_point_identity}") as ws:
        assert (await recv_json(ws))["identity"] == charge_point_identity
        await publish(redis_client, "SOMEONE_ELSE", status="Charging")
        mine = await publish(redis_client, charge_point_identity, status="Charging")
        assert await recv_json(ws) == mine


async def test_event_reaches_only_the_right_of_two_open_sockets(
    live_api, redis_client, charge_point_identity
):
    other_identity = charge_point_identity + "_B"
    await ChargePoint.register(charge_point_identity)
    await ChargePoint.register(other_identity)
    async with (
        websockets.connect(f"{live_api}/api/v1/ws/charge-points/{charge_point_identity}") as a,
        websockets.connect(f"{live_api}/api/v1/ws/charge-points/{other_identity}") as b,
    ):
        await recv_json(a)
        await recv_json(b)
        event = await publish(redis_client, other_identity, status="Charging")
        assert await recv_json(b) == event
        with pytest.raises(asyncio.TimeoutError):
            await recv_json(a, timeout=0.5)


async def test_unknown_site_and_charge_point_are_refused_with_1008(live_api):
    for path in ("sites/670f00000000000000000000", "sites/garbage", "charge-points/nobody"):
        async with websockets.connect(f"{live_api}/api/v1/ws/{path}") as ws:
            with pytest.raises(ConnectionClosed) as info:
                await ws.recv()
            assert info.value.rcvd.code == 1008


async def test_external_reference_site_is_refused_with_a_distinct_reason(live_api):
    site = Site(
        name="Ext", site_type=SiteType.other, latitude=1.0, longitude=1.0,
        source=SiteSource.external_reference,
    )
    await site.insert()
    async with websockets.connect(f"{live_api}/api/v1/ws/sites/{site.id}") as ws:
        with pytest.raises(ConnectionClosed) as info:
            await ws.recv()
    assert info.value.rcvd.code == 1008
    assert "no operator-managed charger" in info.value.rcvd.reason


async def test_client_disconnect_removes_it_from_the_manager(
    live_api, charge_point_identity
):
    await ChargePoint.register(charge_point_identity)
    manager = app.state.manager
    async with websockets.connect(f"{live_api}/api/v1/ws/charge-points/{charge_point_identity}") as ws:
        await recv_json(ws)
        assert len(manager) == 1
    for _ in range(100):
        if len(manager) == 0:
            break
        await asyncio.sleep(0.02)
    assert len(manager) == 0


async def test_websockets_refused_1013_but_rest_works_with_redis_down(
    db, test_database_name, monkeypatch, charge_point_identity
):
    monkeypatch.setenv("MONGODB_DB", test_database_name)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    # config.redis_url() is read at startup, so the env change above is what this server sees.
    await ChargePoint.register(charge_point_identity)
    server, task, base = await _start_server()
    try:
        assert app.state.subscriber is None
        async with websockets.connect(
            f"{base}/api/v1/ws/charge-points/{charge_point_identity}"
        ) as ws:
            with pytest.raises(ConnectionClosed) as info:
                await ws.recv()
            assert info.value.rcvd.code == 1013
        import httpx

        async with httpx.AsyncClient() as http:
            response = await http.get(
                base.replace("ws://", "http://") + f"/api/v1/charge-points/{charge_point_identity}"
            )
        assert response.status_code == 200
    finally:
        await _stop_server(server, task, test_database_name)


async def test_main_publishes_events_the_api_envelope_expects(
    redis_client, connected_charge_point, monkeypatch
):
    """The producer half of the seam: a real StatusNotification through the real Central System
    ends up on the Redis channel in the §E.2 envelope shape."""
    from ocpp.v16 import call

    import main

    await main.connect_event_publisher()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(events_channel())
    await pubsub.get_message(timeout=1)
    try:
        await connected_charge_point.call(
            call.StatusNotification(
                connector_id=1, error_code="NoError", status="Preparing"
            )
        )
        message = None
        for _ in range(50):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message:
                break
        event = json.loads(message["data"])
        assert event["type"] == "connector_status"
        assert event["charge_point_identity"] == connected_charge_point.id
        assert event["connector_id"] == 1
        assert event["data"] == {"status": "Preparing", "error_code": "NoError"}
        assert "at" in event
    finally:
        await pubsub.aclose()
        await main._event_publisher.aclose()
        main._event_publisher = None
