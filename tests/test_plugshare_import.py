"""Tests for the PlugShare import script."""

import json
import pytest
import pytest_asyncio
from datetime import UTC, datetime
from pathlib import Path

from models import Site, SiteSource, SiteType


@pytest.fixture
def plugshare_fixture_data():
    """Minimal synthetic PlugShare export for testing."""
    return [
        {
            "id": 100001,
            "name": "Test Charger 1",
            "address": "123 Main St, Test City",
            "latitude": 42.5,
            "longitude": 20.5,
            "connector_types": ["Type 2", "CCS2"],
            "url": "https://plugshare.com/location/100001",
            "coming_soon": False,
            "under_repair": False,
            "station_count": 2,
            "stations": [
                {"id": 1, "outlets": []},
                {"id": 2, "outlets": []},
            ],
            "access": 1,
        },
        {
            "id": 100002,
            "name": "Test Charger 2",
            "address": "456 Oak Ave, Test City",
            "latitude": 42.6,
            "longitude": 20.6,
            "connector_types": ["CHAdeMO"],
            "url": "https://plugshare.com/location/100002",
            "coming_soon": False,
            "under_repair": True,
            "station_count": 1,
            "stations": [{"id": 3, "outlets": []}],
            "access": 1,
        },
        {
            "id": 100003,
            "name": "Test Charger Coming Soon",
            "address": "789 Pine Ln, Test City",
            "latitude": 42.7,
            "longitude": 20.7,
            "connector_types": ["Type 2"],
            "url": "https://plugshare.com/location/100003",
            "coming_soon": True,
            "under_repair": False,
            "station_count": 1,
            "stations": [{"id": 4, "outlets": []}],
            "access": 1,
        },
    ]


@pytest_asyncio.fixture
async def import_sites(db, plugshare_fixture_data):
    """Helper to import fixture data and return created/updated/skipped counts."""

    async def _import(data, dry_run=False):
        created = 0
        updated = 0
        skipped = 0

        for entry in data:
            # Skip if coming_soon
            if entry.get("coming_soon"):
                skipped += 1
                continue

            # Validate required fields
            name = entry.get("name")
            latitude = entry.get("latitude")
            longitude = entry.get("longitude")

            if not name or latitude is None or longitude is None:
                skipped += 1
                continue

            external_id = str(entry["id"])
            address = entry.get("address")
            connector_types = entry.get("connector_types", [])
            external_url = entry.get("url")
            external_charge_point_count = len(entry.get("stations", []))

            existing_site = await Site.find_one(Site.external_id == external_id)

            if existing_site:
                if not dry_run:
                    await existing_site.set({
                        "name": name,
                        "latitude": latitude,
                        "longitude": longitude,
                        "address": address,
                        "connector_types": connector_types,
                        "external_url": external_url,
                        "external_charge_point_count": external_charge_point_count,
                        "updated_at": datetime.now(UTC),
                    })
                updated += 1
            else:
                if not dry_run:
                    site = Site(
                        name=name,
                        site_type=SiteType.other,
                        latitude=latitude,
                        longitude=longitude,
                        address=address,
                        source=SiteSource.external_reference,
                        external_id=external_id,
                        external_url=external_url,
                        connector_types=connector_types,
                        external_charge_point_count=external_charge_point_count,
                    )
                    await site.insert()
                created += 1

        return created, updated, skipped

    return _import


@pytest.mark.asyncio
async def test_normal_entry_creates_site(db, import_sites, plugshare_fixture_data):
    """A normal entry creates a Site with correct source, type, and charge point count."""
    data = [plugshare_fixture_data[0]]  # Just the first entry
    created, updated, skipped = await import_sites(data)

    assert created == 1
    assert updated == 0
    assert skipped == 0

    site = await Site.find_one(Site.external_id == "100001")
    assert site is not None
    assert site.name == "Test Charger 1"
    assert site.source == SiteSource.external_reference
    assert site.site_type == SiteType.other
    assert site.external_charge_point_count == 2
    assert site.connector_types == ["Type 2", "CCS2"]


@pytest.mark.asyncio
async def test_coming_soon_entry_skipped(db, import_sites, plugshare_fixture_data):
    """An entry with coming_soon: true creates nothing."""
    data = [plugshare_fixture_data[2]]  # The coming_soon entry
    created, updated, skipped = await import_sites(data)

    assert created == 0
    assert updated == 0
    assert skipped == 1

    site = await Site.find_one(Site.external_id == "100003")
    assert site is None


@pytest.mark.asyncio
async def test_import_twice_creates_then_updates(db, import_sites):
    """Running the import twice creates once and updates on the second run."""
    data = [
        {
            "id": 200001,
            "name": "Test Charger Twice",
            "address": "999 Test St",
            "latitude": 42.8,
            "longitude": 20.8,
            "connector_types": ["Type 2"],
            "url": "https://plugshare.com/location/200001",
            "coming_soon": False,
            "under_repair": False,
            "station_count": 1,
            "stations": [{"id": 5, "outlets": []}],
            "access": 1,
        }
    ]

    # First import
    created1, updated1, skipped1 = await import_sites(data)
    assert created1 == 1
    assert updated1 == 0
    assert skipped1 == 0

    initial_site_count = await Site.find(Site.external_id == "200001").count()
    assert initial_site_count == 1

    # Second import of the same data
    created2, updated2, skipped2 = await import_sites(data)
    assert created2 == 0
    assert updated2 == 1
    assert skipped2 == 0

    final_site_count = await Site.find(Site.external_id == "200001").count()
    assert final_site_count == 1


@pytest.mark.asyncio
async def test_manual_site_type_preserved_on_reimport(db, import_sites):
    """A Site whose site_type was manually changed keeps that value after reimport."""
    data = [
        {
            "id": 200002,
            "name": "Test Charger Manual",
            "address": "888 Test St",
            "latitude": 42.9,
            "longitude": 20.9,
            "connector_types": ["CCS2"],
            "url": "https://plugshare.com/location/200002",
            "coming_soon": False,
            "under_repair": False,
            "station_count": 1,
            "stations": [{"id": 6, "outlets": []}],
            "access": 1,
        }
    ]

    # First import
    created1, updated1, skipped1 = await import_sites(data)
    assert created1 == 1

    # Manually change the site_type
    site = await Site.find_one(Site.external_id == "200002")
    assert site.site_type == SiteType.other
    await site.set({"site_type": SiteType.hotel})
    site_after_change = await Site.find_one(Site.external_id == "200002")
    assert site_after_change.site_type == SiteType.hotel

    # Second import
    created2, updated2, skipped2 = await import_sites(data)
    assert created2 == 0
    assert updated2 == 1

    # Verify site_type is preserved
    site_final = await Site.find_one(Site.external_id == "200002")
    assert site_final.site_type == SiteType.hotel
