"""Tests for seed_demo_fleet.py (instructions/11-demo-fleet.md §B, §C).

Real MongoDB (the shared `db` fixture), small inline synthetic PlugShare exports, and a manifest
under tmp_path. The Sites are created by the real import code (import_plugshare_sites), exactly
as in production, so "run the import afterwards" tests the real update path. Every PlugShare id
is unique to this run: the test database is shared across the session.
"""

import itertools
import json
import random

import pytest
from ocpp.v16.enums import AuthorizationStatus, RegistrationStatus

from import_plugshare_sites import import_entries
from models import ChargePoint, IdTag, PowerType, Site, SiteSource, SiteType
from seed_demo_fleet import (
    ID_TAG_PARENT,
    REMOTE_ID_TAG,
    SeedError,
    build_plan,
    seed,
)

# Eight-digit ids, as PlugShare's longest station id has seven: keeps every idTag well inside
# OCPP's 20 characters, and sequential from a random base so tests never collide.
_ids = itertools.count(random.randrange(10_000_000, 80_000_000, 1000))


def outlet(code=7, power_type="AC", kilowatts=None, status=None):
    return {
        "connector": code,
        "id": next(_ids),
        "is_dc": power_type == "DC",
        "kilowatts": kilowatts,
        "power": 0,
        "power_type": power_type,
        "status": status,
    }


def station(*outlets):
    return {"id": next(_ids), "network_id": None, "outlets": list(outlets)}


def location(*stations, name="Synthetic site", coming_soon=False):
    location_id = next(_ids)
    return {
        "id": location_id,
        "name": name,
        "address": "Test road 1",
        "latitude": 42.5,
        "longitude": 19.2,
        "connector_types": ["Type 2"],
        "url": f"http://api.plugshare.com/view/location/{location_id}",
        "coming_soon": coming_soon,
        "under_repair": False,
        "station_count": len(stations),
        "stations": list(stations),
    }


def identity_of(station_entry):
    return f"PS-{station_entry['id']}"


async def site_for(entry):
    return await Site.find_one(Site.external_id == str(entry["id"]))


def read_manifest(path):
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_keys(path):
    return {c["identity"]: c["authorization_key"] for c in read_manifest(path)["chargers"]}


@pytest.fixture
def manifest_path(tmp_path):
    return tmp_path / "demo_fleet_manifest.json"


async def test_two_station_site_and_three_outlet_station(db, manifest_path):
    three = station(outlet(7), outlet(20, "DC", 150.0), outlet(3, "DC"))
    single = station(outlet(10, kilowatts=15.36))
    export = [location(three, single)]
    await import_entries(export)
    site = await site_for(export[0])

    report = await seed(export, manifest_path)

    assert report["chargers_created"] == 2
    assert report["connectors"] == 4
    record = await ChargePoint.find_one(ChargePoint.identity == identity_of(three))
    assert record.simulated is True
    assert record.registration_status == RegistrationStatus.accepted
    assert record.site_id == site.id
    assert [spec.connector_id for spec in record.connector_specs] == [1, 2, 3]
    assert [spec.connector_type for spec in record.connector_specs] == [
        "Type 2", "CCS2", "CHAdeMO",
    ]
    # Code 20 is CCS2/DC with its kW kept; a null kW stays None, never guessed.
    ccs2 = record.connector_specs[1]
    assert (ccs2.power_type, ccs2.max_power_kw) == (PowerType.DC, 150.0)
    assert record.connector_specs[0].max_power_kw is None
    other = await ChargePoint.find_one(ChargePoint.identity == identity_of(single))
    assert other.site_id == site.id
    assert other.connector_specs[0].max_power_kw == 15.36

    site = await site_for(export[0])
    assert site.source == SiteSource.simulated
    assert site.site_type == SiteType.other
    assert site.external_charge_point_count == 2

    manifest = read_manifest(manifest_path)
    entries = {c["identity"]: c for c in manifest["chargers"]}
    assert set(entries) == {identity_of(three), identity_of(single)}
    assert record.verify_authorization_key(entries[identity_of(three)]["authorization_key"])
    assert entries[identity_of(three)]["site_name"] == "Synthetic site"
    assert entries[identity_of(three)]["connectors"][1] == {
        "connector_id": 2,
        "connector_type": "CCS2",
        "power_type": "DC",
        "max_power_kw": 150.0,
        "out_of_order": False,
        "id_tag": f"DEMO-{three['id']}-2",
    }


def test_plan_is_pure_and_keeps_kw_as_is():
    working = station(outlet(20, "DC", 15.36))
    export = [location(working), location(station(outlet()), coming_soon=True)]
    plan = build_plan(export)
    assert len(plan) == 1  # coming_soon skipped, as 09 does
    (connector,) = plan[0].chargers[0].connectors
    assert connector.max_power_kw == 15.36
    assert connector.connector_type == "CCS2"


def test_an_id_tag_longer_than_20_characters_aborts():
    too_long = {"id": 12345678901234, "outlets": [outlet()]}  # DEMO-12345678901234-1 is 21
    with pytest.raises(SeedError, match="at most 20"):
        build_plan([location(too_long)])


async def test_out_of_order_outlet_is_in_the_manifest_only(db, manifest_path):
    broken = station(outlet(7, status="OUTOFORDER"), outlet(7, status="UNKNOWN"))
    export = [location(broken)]
    await import_entries(export)

    report = await seed(export, manifest_path)

    assert report["connectors_out_of_order"] == 1
    (entry,) = read_manifest(manifest_path)["chargers"]
    assert [c["out_of_order"] for c in entry["connectors"]] == [True, False]
    # Not stored in MongoDB: a third party's fault report is not live status.
    record = await ChargePoint.find_one(ChargePoint.identity == identity_of(broken))
    assert "out_of_order" not in record.connector_specs[0].model_dump()


async def test_one_id_tag_per_connector_plus_demo_remote(db, manifest_path):
    two = station(outlet(), outlet())
    one = station(outlet())
    export = [location(two, one)]
    await import_entries(export)

    await seed(export, manifest_path)

    expected = [f"DEMO-{two['id']}-1", f"DEMO-{two['id']}-2", f"DEMO-{one['id']}-1"]
    for tag in expected + [REMOTE_ID_TAG]:
        record = await IdTag.find_one(IdTag.id_tag == tag)
        assert record is not None, tag
        assert record.parent_id_tag == ID_TAG_PARENT
        assert record.status == AuthorizationStatus.accepted
    assert [c["id_tag"] for c in read_manifest(manifest_path)["chargers"][0]["connectors"]] == (
        expected[:2]
    )


async def _assert_nothing_written(export, manifest_path):
    for entry in export:
        site = await site_for(entry)
        assert site is None or site.source == SiteSource.external_reference
        for station_entry in entry["stations"]:
            record = await ChargePoint.find_one(ChargePoint.identity == identity_of(station_entry))
            assert record is None or not record.simulated
            for index in range(len(station_entry["outlets"])):
                tag = f"DEMO-{station_entry['id']}-{index + 1}"
                assert await IdTag.find_one(IdTag.id_tag == tag) is None
    assert not manifest_path.exists()


async def test_unknown_connector_code_aborts_with_no_writes(db, manifest_path):
    bad = station(outlet(99))
    export = [location(station(outlet())), location(bad)]
    await import_entries(export)

    with pytest.raises(SeedError) as info:
        await seed(export, manifest_path)

    assert "99" in str(info.value) and str(bad["id"]) in str(info.value)
    await _assert_nothing_written(export, manifest_path)


async def test_missing_site_aborts_with_no_writes(db, manifest_path):
    imported = location(station(outlet()))
    never_imported = location(station(outlet()), name="Not imported")
    await import_entries([imported])

    with pytest.raises(SeedError, match="import_plugshare_sites.py"):
        await seed([imported, never_imported], manifest_path)

    await _assert_nothing_written([imported, never_imported], manifest_path)


async def test_operator_site_is_skipped_and_left_untouched(db, manifest_path):
    real = station(outlet())
    converted = station(outlet())
    export = [location(real, name="A real operator site"), location(converted)]
    await import_entries(export)
    operator_site = await site_for(export[0])
    await operator_site.set({"source": SiteSource.operator})
    before = (await site_for(export[0])).model_dump()

    report = await seed(export, manifest_path)

    assert report["sites_skipped"] == 1
    assert report["skipped_site_names"] == ["A real operator site"]
    assert (await site_for(export[0])).model_dump() == before
    assert await ChargePoint.find_one(ChargePoint.identity == identity_of(real)) is None
    assert await ChargePoint.find_one(ChargePoint.identity == identity_of(converted))
    identities = [c["identity"] for c in read_manifest(manifest_path)["chargers"]]
    assert identities == [identity_of(converted)]


async def test_identity_owned_by_a_real_charger_aborts(db, manifest_path):
    taken = station(outlet())
    export = [location(taken)]
    await import_entries(export)
    real, _key = await ChargePoint.register(identity_of(taken))

    with pytest.raises(SeedError, match="real"):
        await seed(export, manifest_path)

    after = await ChargePoint.get(real.id)
    assert after.simulated is False
    assert after.authorization_key_hash == real.authorization_key_hash
    await _assert_nothing_written(export, manifest_path)


async def test_second_run_creates_nothing_and_rotates_no_keys(db, manifest_path):
    export = [location(station(outlet(), outlet(20, "DC", 50.0)), station(outlet()))]
    await import_entries(export)
    first = await seed(export, manifest_path)
    keys = manifest_keys(manifest_path)
    ids = {
        identity: (await ChargePoint.find_one(ChargePoint.identity == identity)).id
        for identity in keys
    }

    second = await seed(export, manifest_path)

    assert first["chargers_created"] == 2
    assert (second["chargers_created"], second["keys_rotated"]) == (0, 0)
    assert (second["id_tags_created"], second["sites_converted"]) == (0, 0)
    assert second["chargers_updated"] == 2
    assert manifest_keys(manifest_path) == keys
    for identity, document_id in ids.items():
        assert await ChargePoint.find(ChargePoint.identity == identity).count() == 1
        assert (await ChargePoint.find_one(ChargePoint.identity == identity)).id == document_id


async def test_a_lost_manifest_rotates_the_keys_it_held(db, manifest_path):
    export = [location(station(outlet()))]
    await import_entries(export)
    await seed(export, manifest_path)
    manifest_path.unlink()

    report = await seed(export, manifest_path)

    assert report["keys_rotated"] == 1
    (entry,) = read_manifest(manifest_path)["chargers"]
    record = await ChargePoint.find_one(ChargePoint.identity == entry["identity"])
    assert record.verify_authorization_key(entry["authorization_key"])


async def test_import_afterwards_leaves_sites_simulated(db, manifest_path):
    export = [location(station(outlet())), location(station(outlet()))]
    await import_entries(export)
    await seed(export, manifest_path)

    created, updated, _skipped, _entries = await import_entries(export)

    assert (created, updated) == (0, 2)
    for entry in export:
        assert (await site_for(entry)).source == SiteSource.simulated


async def test_dry_run_reports_and_writes_nothing(db, manifest_path):
    export = [location(station(outlet(20, "DC", 200.0), outlet(status="OUTOFORDER")))]
    await import_entries(export)

    report = await seed(export, manifest_path, dry_run=True)

    assert report["sites_converted"] == 1
    assert report["chargers_created"] == 1
    assert (report["connectors"], report["connectors_with_kw"]) == (2, 1)
    assert report["connectors_out_of_order"] == 1
    assert report["id_tags_created"] >= 2  # DEMO-REMOTE may exist from an earlier test
    await _assert_nothing_written(export, manifest_path)
