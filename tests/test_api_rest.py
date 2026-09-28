"""REST tests for the public API (instructions/08-public-api-service.md §F, §H, §L).

Real MongoDB (the shared `db` fixture); httpx.ASGITransport drives the real FastAPI routing and
serialization without a TCP server. No Redis is needed: REST never touches it.
"""

import httpx
import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from ocpp.v16.enums import ChargePointStatus

from api.app import app
from models import (
    ChargePoint,
    ConnectorStatus,
    Site,
    SiteSource,
    SiteType,
    Transaction,
)


@pytest_asyncio.fixture
async def client(db):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
        yield c


async def make_site(name="Site", source=SiteSource.operator, **extra):
    site = Site(
        name=name, site_type=SiteType.parking, latitude=42.4, longitude=19.2, source=source,
        **extra,
    )
    await site.insert()
    return site


async def make_cp(identity, site=None, statuses=(), **fields):
    record, _key = await ChargePoint.register(identity, **fields)
    if site is not None:
        await record.set_location(site_id=site.id)
    for connector_id, status in enumerate(statuses, start=1):
        await ConnectorStatus(
            charge_point_identity=identity, connector_id=connector_id, status=status
        ).insert()
    return record


async def summary_for(client, site_id):
    response = await client.get("/api/v1/sites")
    assert response.status_code == 200
    return next(s for s in response.json() if s["id"] == site_id)


S = ChargePointStatus


@pytest.mark.parametrize(
    "statuses, expected",
    [
        ([S.faulted, S.available], "faulted"),
        ([S.charging, S.available, S.reserved], "available"),
        ([S.charging, S.reserved], "reserved"),
        ([S.charging, S.suspended_ev], "occupied"),
        ([S.unavailable], "unavailable"),
        ([], "unavailable"),
    ],
)
async def test_operator_site_aggregate_status_precedence(
    client, charge_point_identity, statuses, expected
):
    site = await make_site()
    await make_cp(charge_point_identity, site, statuses)
    summary = await summary_for(client, str(site.id))
    assert summary["aggregate_status"] == expected
    assert summary["charge_point_count"] == 1


async def test_external_reference_site_is_unknown_with_static_count(client):
    site = await make_site(
        source=SiteSource.external_reference,
        connector_types=["CCS2"],
        external_charge_point_count=4,
    )
    summary = await summary_for(client, str(site.id))
    assert summary["aggregate_status"] == "unknown"
    assert summary["charge_point_count"] == 4
    assert summary["connector_types"] == ["CCS2"]
    detail = (await client.get(f"/api/v1/sites/{site.id}")).json()
    assert detail["charge_points"] == []


async def test_standalone_charge_point_appears_as_a_site_of_one(client, charge_point_identity):
    record = await make_cp(charge_point_identity, statuses=[S.available])
    await record.set_location(latitude=42.1, longitude=19.1)
    summary = await summary_for(client, f"standalone:{charge_point_identity}")
    assert summary["site_type"] == "standalone"
    assert summary["source"] == "operator"
    assert summary["aggregate_status"] == "available"
    detail = (await client.get(f"/api/v1/sites/standalone:{charge_point_identity}")).json()
    assert detail["charge_points"][0]["identity"] == charge_point_identity


async def test_charge_point_without_location_is_not_listed(client, charge_point_identity):
    await make_cp(charge_point_identity)
    response = await client.get("/api/v1/sites")
    assert all(charge_point_identity not in s["id"] for s in response.json())


async def test_site_detail_shape(client, charge_point_identity):
    site = await make_site(address="Bulevar 1", connector_types=["Type 2"])
    await make_cp(charge_point_identity, site, [S.charging])
    body = (await client.get(f"/api/v1/sites/{site.id}")).json()
    assert body["address"] == "Bulevar 1"
    assert body["charge_points"] == [
        {
            "identity": charge_point_identity,
            "connectors": [{"connector_id": 1, "status": "Charging", "error_code": "NoError"}],
        }
    ]


async def test_unknown_site_is_404_and_malformed_id_is_422(client):
    assert (await client.get(f"/api/v1/sites/{PydanticObjectId()}")).status_code == 404
    assert (await client.get("/api/v1/sites/not-an-object-id")).status_code == 422


async def test_charge_point_detail_and_current_transaction(client, charge_point_identity):
    await make_cp(charge_point_identity, statuses=[S.charging])
    body = (await client.get(f"/api/v1/charge-points/{charge_point_identity}")).json()
    assert body["current_transaction"] is None
    assert body["connectors"][0]["status"] == "Charging"

    transaction = await Transaction.start(
        charge_point_identity, 1, "TAG", 100, __import__("datetime").datetime.now()
    )
    await transaction.add_meter_values(
        [{"timestamp": "2026-01-01T00:00:00Z", "sampled_value": [{"value": "1500"}]}]
    )
    body = (await client.get(f"/api/v1/charge-points/{charge_point_identity}")).json()
    assert body["current_transaction"] == {
        "transaction_id": transaction.transaction_id,
        "meter_start": 100,
        "latest_meter_value": 1500,
    }


async def test_unknown_charge_point_is_404(client):
    assert (await client.get("/api/v1/charge-points/nobody")).status_code == 404


async def test_every_route_is_get_or_websocket():
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue  # WebSocket route
        if route.path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            continue
        assert methods <= {"GET", "HEAD"}, (route.path, methods)


async def test_cors_default_allows_no_origin(client):
    response = await client.get("/api/v1/sites", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in response.headers


