"""Tests for the Site model and its operator-facing CLI (instructions/07-public-map-platform.md
§A/§B): a physical place (a fuel station, a hotel car park, ...) that can host one or more
ChargePoints, and the one-off command an operator uses to assign an existing ChargePoint to one
-- or give it its own standalone coordinates -- since OCPP itself has no such concept and nothing
about it is reported by a charger.

The admin HTTP tests reuse test_remote_start_stop.py's own http_get helper pattern (a blocking
urllib call run in an executor, so it doesn't deadlock the in-process server's own event loop).
"""

import asyncio
import urllib.error
import urllib.parse
import urllib.request

import pytest

import main
from models import ChargePoint, Site, SiteType


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
    """See test_remote_start_stop.py's fixture of the same name."""
    token = "test-admin-token"
    monkeypatch.setenv("ADMIN_TOKEN", token)
    return token


# --------------------------------------------------------------------------------------------
# Site / ChargePoint.map_location() / ChargePoint.set_location()
# --------------------------------------------------------------------------------------------


async def test_site_create_defaults_to_operator_source(db):
    site = await Site.create("Petrol Podgorica", SiteType.gas_station, 42.4304, 19.2594)
    assert site.source == "operator"
    assert site.name == "Petrol Podgorica"


async def test_map_location_is_none_with_nothing_set(db, charge_point_identity):
    record, _key = await ChargePoint.register(charge_point_identity)
    assert await record.map_location() is None


async def test_map_location_uses_own_coordinates_with_no_site(db, charge_point_identity):
    record, _key = await ChargePoint.register(charge_point_identity)
    await record.set_location(latitude=42.1, longitude=19.1)
    assert await record.map_location() == (42.1, 19.1)


async def test_map_location_prefers_site_over_own_coordinates(db, charge_point_identity):
    site = await Site.create("Hotel Aria", SiteType.hotel, 42.9, 19.2)
    record, _key = await ChargePoint.register(charge_point_identity)
    # Deliberately give the charger its OWN coordinates too, to prove the site wins anyway.
    await record.set_location(site_id=site.id, latitude=0.0, longitude=0.0)
    assert await record.map_location() == (42.9, 19.2)


async def test_set_location_only_touches_fields_given(db, charge_point_identity):
    record, _key = await ChargePoint.register(charge_point_identity)
    await record.set_location(latitude=42.1, longitude=19.1)
    site = await Site.create("Rest stop", SiteType.other, 43.0, 19.5)
    await record.set_location(site_id=site.id)  # no latitude/longitude passed this time
    assert record.site_id == site.id
    assert record.latitude == 42.1  # untouched, not clobbered by the second call
    assert record.longitude == 19.1


# --------------------------------------------------------------------------------------------
# main.create_site / main.set_charge_point_location, called directly
# --------------------------------------------------------------------------------------------


async def test_create_site_direct(db):
    site = await main.create_site("Eko Bijelo Polje", SiteType.gas_station, 43.04, 19.77)
    found = await Site.get(site.id)
    assert found is not None
    assert found.name == "Eko Bijelo Polje"


async def test_set_charge_point_location_direct_by_site(db, charge_point_identity):
    record, _key = await ChargePoint.register(charge_point_identity)
    site = await main.create_site("Some Hotel", SiteType.hotel, 42.5, 18.9)
    updated = await main.set_charge_point_location(charge_point_identity, site_id=str(site.id))
    assert updated.site_id == site.id


async def test_set_charge_point_location_direct_by_coordinates(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity)
    updated = await main.set_charge_point_location(
        charge_point_identity, latitude=42.2, longitude=19.3
    )
    assert (updated.latitude, updated.longitude) == (42.2, 19.3)


async def test_set_charge_point_location_unregistered_identity_raises(db, charge_point_identity):
    with pytest.raises(ValueError, match="not registered"):
        await main.set_charge_point_location(charge_point_identity, latitude=1.0, longitude=1.0)


async def test_set_charge_point_location_unknown_site_raises(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity)
    with pytest.raises(ValueError, match="no such site"):
        await main.set_charge_point_location(
            charge_point_identity, site_id="507f1f77bcf86cd799439011"
        )


async def test_set_charge_point_location_malformed_site_id_raises(db, charge_point_identity):
    await ChargePoint.register(charge_point_identity)
    with pytest.raises(ValueError, match="not a valid site id"):
        await main.set_charge_point_location(charge_point_identity, site_id="not-an-object-id")


# --------------------------------------------------------------------------------------------
# The admin HTTP API
# --------------------------------------------------------------------------------------------


async def test_admin_create_site_end_to_end(server, admin_token):
    http_url = server.replace("ws://", "http://")
    query = urllib.parse.urlencode(
        {
            "name": "Petrol Podgorica — Bulevar",
            "site_type": "gas_station",
            "latitude": "42.4304",
            "longitude": "19.2594",
            "token": admin_token,
        }
    )
    status, body = await http_get(f"{http_url}/admin/create-site?{query}")
    assert status == 201
    assert b"gas_station" in body


async def test_admin_create_site_rejects_invalid_site_type(server, admin_token):
    http_url = server.replace("ws://", "http://")
    query = urllib.parse.urlencode(
        {
            "name": "Bad Entry",
            "site_type": "spaceport",
            "latitude": "0",
            "longitude": "0",
            "token": admin_token,
        }
    )
    status, _body = await http_get(f"{http_url}/admin/create-site?{query}")
    assert status == 400


async def test_admin_set_charge_point_location_end_to_end(
    server, admin_token, registered_charge_point
):
    identity, _key = registered_charge_point
    http_url = server.replace("ws://", "http://")
    query = urllib.parse.urlencode(
        {"identity": identity, "latitude": "42.1", "longitude": "19.1", "token": admin_token}
    )
    status, body = await http_get(f"{http_url}/admin/set-charge-point-location?{query}")
    assert status == 200
    assert identity.encode() in body


async def test_admin_set_charge_point_location_unregistered_identity(server, admin_token):
    http_url = server.replace("ws://", "http://")
    query = urllib.parse.urlencode(
        {"identity": "NO-SUCH-CP", "latitude": "0", "longitude": "0", "token": admin_token}
    )
    status, _body = await http_get(f"{http_url}/admin/set-charge-point-location?{query}")
    assert status == 400
