"""Shared fixtures for the normal-charge-flow test suite.

Everything here is real: a real MongoDB connection (on a throwaway, uniquely named database),
a real Central System started in-process on an ephemeral port, and real OCPP-J WebSocket
connections. See instructions/05-normal-charge-flow-tests.md for why each fixture is shaped
this way.
"""

import asyncio
import socket
import uuid

import pytest
import pytest_asyncio
import websockets
from ocpp.v16 import ChargePoint as cp
from ocpp.v16.enums import RegistrationStatus
import redis.asyncio as redis_asyncio
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError
from websockets.typing import Subprotocol

import main
from api.config import redis_url
from models import ChargePoint, IdTag, init_db, mongodb_url
from ocpp_client_auth import basic_auth_header


@pytest_asyncio.fixture(scope="session")
async def mongodb_available():
    """Ping MongoDB once; skip the whole run if it is not reachable.

    MongoDB is not installed on the development machine by default, so a missing database must
    read as "skipped", not as sixty failing tests. Every fixture that touches a document depends
    on this (directly or transitively), so a failed ping skips them all with one clear reason.
    """
    client = AsyncMongoClient(mongodb_url(), serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
    except PyMongoError as exc:
        pytest.skip(f"MongoDB is not reachable at {mongodb_url()}: {exc}")
    finally:
        await client.close()


@pytest_asyncio.fixture(scope="session")
async def redis_available():
    """Ping Redis once; skip the tests that need it if it is not reachable.

    Modelled on mongodb_available: Redis only backs the public API's WebSocket layer
    (08-public-api-service.md), so a machine without it skips those tests rather than failing.
    """
    client = redis_asyncio.Redis.from_url(redis_url())
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis is not reachable at {redis_url()}: {exc}")
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def test_database_name():
    """One throwaway database per test run, never the default ocpp_poc database."""
    return f"ocpp_test_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(scope="session")
async def db(mongodb_available, test_database_name):
    """Connect beanie to the throwaway database for this run; drop it afterward.

    Session-scoped: every test in the run shares one connection and one database, distinguished
    from each other only by each test's own unique charge point identity and idTag.
    """
    client = await init_db(database=test_database_name)
    yield client
    await client.drop_database(test_database_name)
    await client.close()


@pytest.fixture(scope="session")
def port_9000_reserved():
    """Occupy port 9000 for the whole test run, proving the suite does not depend on it free.

    instructions/05-normal-charge-flow-tests.md acceptance criterion 4: the suite must pass
    when port 9000 is already taken by another process -- which, on this machine, it usually
    is. The real server fixture below binds port 0 (an OS-assigned free port) specifically so
    this is a non-issue; this fixture exists only to prove that claim rather than assume it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", 9000))
        sock.listen(1)
    except OSError:
        # Already occupied by someone else (e.g. a stale main.py) -- that also proves the
        # point, so there is nothing more for this fixture to do.
        sock.close()
        yield
        return
    yield
    sock.close()


@pytest_asyncio.fixture(scope="session")
async def server(db, port_9000_reserved):
    """The Central System, running in-process on an OS-assigned ephemeral port.

    Returns the base ws:// URL; append "/<identity>" for a specific charger's connection URL.
    """
    srv = await main.serve(host="127.0.0.1", port=0)
    port = srv.sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}"
    srv.close()
    await srv.wait_closed()


@pytest.fixture
def charge_point_identity(request):
    """A charge point identity unique to this test, so tests never collide on the same row."""
    safe_name = "".join(c if c.isalnum() else "_" for c in request.node.name)[:24]
    return f"T_{safe_name}_{uuid.uuid4().hex[:6]}"


@pytest.fixture
def id_tag_value(request):
    """An idTag string unique to this test. Not yet registered -- see accepted_id_tag."""
    return f"TAG_{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture
async def registered_charge_point(db, charge_point_identity):
    """Register a charge point that is already Accepted, and return (identity, plaintext key).

    Registered Accepted rather than the default Pending: these tests are about the normal
    charge flow, not commissioning (that is instructions/03's concern, tested separately), and
    a Pending charger would trigger a real Route B onboarding attempt against a test client that
    has no ChangeConfiguration handler, failing harmlessly but noisily on every single test.
    """
    _record, key = await ChargePoint.register(
        charge_point_identity, registration_status=RegistrationStatus.accepted
    )
    return charge_point_identity, key


@pytest_asyncio.fixture
async def accepted_id_tag(db, id_tag_value):
    """Register an idTag with status Accepted (OCPP's default, usable state) and return it."""
    await IdTag(id_tag=id_tag_value).insert()
    return id_tag_value


@pytest_asyncio.fixture
async def connected_charge_point(server, registered_charge_point):
    """An authenticated, started OCPP client connection to the test server.

    Yields the client (client.id is its charge point identity, client.call(...) sends
    requests). The listener task and the socket are cleaned up on teardown regardless of how
    the test exits.
    """
    identity, key = registered_charge_point
    header = basic_auth_header(identity, key)
    async with websockets.connect(
        f"{server}/{identity}",
        subprotocols=[Subprotocol("ocpp1.6")],
        additional_headers={"Authorization": header},
    ) as ws:
        client = cp(identity, ws)
        listener = asyncio.create_task(client.start())
        try:
            yield client
        finally:
            listener.cancel()
