"""Turn every imported PlugShare site into a simulated one, with a mock charger per station.

    python seed_demo_fleet.py
    python seed_demo_fleet.py --dry-run
    python seed_demo_fleet.py --file montenegro_only.json --manifest demo_fleet_manifest.json

This is demo data (11-demo-fleet.md). For each PlugShare location that import_plugshare_sites.py
already turned into a Site, it marks the Site `source=simulated` and registers one ChargePoint
per PlugShare station (identity `PS-<station id>`, `simulated=True`) with one connector per
PlugShare outlet, described by a ConnectorSpec. It also creates one driver idTag per connector,
plus DEMO-REMOTE for trying `operate.py remote-start` by hand.

Everything is validated before anything is written. The charger keys go into the manifest, which
run_demo_fleet.py reads; like charge_point_credentials.json for seed.py, it is the only copy.
Safe to re-run: existing mock chargers are updated in place, and a key the manifest still holds
is kept rather than rotated. remove_demo_fleet.py (12-remove-demo-fleet.md) undoes all of it.
"""

import argparse
import asyncio
import json
import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ocpp.v16.enums import AuthorizationStatus, RegistrationStatus
from pymongo.errors import PyMongoError

from models import (
    ChargePoint,
    ConnectorSpec,
    IdTag,
    PowerType,
    Site,
    SiteSource,
    display_mongodb_url,
    init_db,
)

DEFAULT_FILE = "montenegro_only.json"
DEFAULT_MANIFEST = "demo_fleet_manifest.json"

# PlugShare's numeric `outlets[].connector` codes, translated to plain names because
# 07-public-map-platform.md §A forbids storing a third party's codes. Every row was checked:
# translating each location's outlets reproduces that location's own `connector_types` list.
CONNECTOR_TYPES = {
    7: "Type 2",
    20: "CCS2",
    3: "CHAdeMO",
    10: "Wall (Euro)",
    14: "Three Phase",
    15: "Caravan Mains Socket",
    2: "J-1772",
}

ID_TAG_PARENT = "DEMO-FLEET"
REMOTE_ID_TAG = "DEMO-REMOTE"
# OCPP 1.6 s7.28: an IdToken is a CiString20Type (s7.15). Authorize.req and StartTransaction.req
# reject anything longer, so a tag that does not fit must stop the seed, not fail at runtime.
ID_TAG_MAX_LEN = 20


class SeedError(Exception):
    """A reason to abort the seed before anything is written."""


@dataclass
class ConnectorPlan:
    """One PlugShare outlet, as the connector it becomes."""

    connector_id: int
    connector_type: str
    power_type: PowerType
    max_power_kw: float | None
    # Manifest only, never MongoDB -- the same rule 09 applies to `under_repair`: a third
    # party's report of a fault is not live status.
    out_of_order: bool
    id_tag: str

    def spec(self):
        return ConnectorSpec(
            connector_id=self.connector_id,
            connector_type=self.connector_type,
            power_type=self.power_type,
            max_power_kw=self.max_power_kw,
        )


@dataclass
class ChargerPlan:
    """One PlugShare station, as the mock charger it becomes."""

    identity: str
    connectors: list[ConnectorPlan]


@dataclass
class LocationPlan:
    """One PlugShare location (our Site) and the chargers planned for it."""

    external_id: str
    name: str
    chargers: list[ChargerPlan]
    site: Site | None = field(default=None, repr=False)


def plan_connector(station_id, index, outlet):
    """Map outlet `index` (0-based) of station `station_id` to its ConnectorPlan (§B)."""
    code = outlet.get("connector")
    if code not in CONNECTOR_TYPES:
        raise SeedError(f"unknown PlugShare connector code {code!r} on station {station_id}")
    try:
        power_type = PowerType(outlet.get("power_type"))
    except ValueError as exc:
        raise SeedError(
            f"unknown power_type {outlet.get('power_type')!r} on station {station_id}"
        ) from exc
    # OCPP 1.6 s3.8: connector ids start at 1; 0 is the charger as a whole.
    connector_id = index + 1
    id_tag = f"DEMO-{station_id}-{connector_id}"
    if len(id_tag) > ID_TAG_MAX_LEN:
        raise SeedError(
            f"idTag {id_tag!r} for station {station_id} is {len(id_tag)} characters; "
            f"OCPP 1.6 allows at most {ID_TAG_MAX_LEN}"
        )
    return ConnectorPlan(
        connector_id=connector_id,
        connector_type=CONNECTOR_TYPES[code],
        power_type=power_type,
        max_power_kw=outlet.get("kilowatts"),
        out_of_order=outlet.get("status") == "OUTOFORDER",
        id_tag=id_tag,
    )


def build_plan(entries):
    """The full §B mapping for a parsed PlugShare export. Pure: touches no database.

    Skips `coming_soon` locations, as import_plugshare_sites.py does. Raises SeedError for an
    unknown connector code or an idTag that would not fit OCPP's 20 characters.
    """
    plan = []
    for entry in entries:
        if entry.get("coming_soon"):
            continue
        chargers = []
        for station in entry.get("stations", []):
            station_id = station["id"]
            connectors = [
                plan_connector(station_id, index, outlet)
                for index, outlet in enumerate(station.get("outlets", []))
            ]
            chargers.append(ChargerPlan(identity=f"PS-{station_id}", connectors=connectors))
        plan.append(
            LocationPlan(
                external_id=str(entry["id"]), name=entry.get("name", ""), chargers=chargers
            )
        )
    return plan


def load_manifest_keys(path):
    """{identity: key} from an existing manifest, or {} when there is none to reuse."""
    try:
        manifest = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return {c["identity"]: c["authorization_key"] for c in manifest.get("chargers", [])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def write_manifest(path, manifest):
    """Write the manifest atomically: a temp file in the same directory, then os.replace, so
    a crash mid-write never leaves a truncated file holding the only copy of the keys."""
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


async def _attach_sites(plan):
    """Find each location's Site. Raises SeedError if any location has none."""
    sites = await Site.find({"external_id": {"$in": [loc.external_id for loc in plan]}}).to_list()
    by_external_id = {}
    for site in sites:
        by_external_id.setdefault(site.external_id, site)
    missing = [loc for loc in plan if loc.external_id not in by_external_id]
    if missing:
        examples = ", ".join(f"{loc.name!r} ({loc.external_id})" for loc in missing[:3])
        raise SeedError(
            f"{len(missing)} PlugShare location(s) have no Site, e.g. {examples}. "
            f"Run 'python import_plugshare_sites.py' first."
        )
    for loc in plan:
        loc.site = by_external_id[loc.external_id]


async def seed(entries, manifest_path, dry_run=False, source_file=DEFAULT_FILE):
    """Seed the demo fleet for a parsed export and return the report counts.

    Needs init_db() to have run. Validates everything first and raises SeedError, having
    written nothing, when the export or the database is not in a state to seed from. With
    dry_run=True it computes the same report and writes nothing: no MongoDB writes, no manifest.
    """
    plan = build_plan(entries)
    await _attach_sites(plan)

    skipped_sites = [loc for loc in plan if loc.site.source == SiteSource.operator]
    live = [loc for loc in plan if loc.site.source != SiteSource.operator]
    chargers = [(loc, charger) for loc in live for charger in loc.chargers]

    identities = [charger.identity for _loc, charger in chargers]
    existing = {
        cp.identity: cp
        for cp in await ChargePoint.find({"identity": {"$in": identities}}).to_list()
    }
    real = sorted(identity for identity, cp in existing.items() if not cp.simulated)
    if real:
        raise SeedError(
            f"{', '.join(real)} already belong(s) to a real (simulated=False) charger; "
            f"refusing to take over a real charger"
        )

    id_tags = [c.id_tag for _loc, charger in chargers for c in charger.connectors]
    id_tags.append(REMOTE_ID_TAG)
    existing_tags = {
        tag.id_tag for tag in await IdTag.find({"id_tag": {"$in": id_tags}}).to_list()
    }
    new_tags = [tag for tag in id_tags if tag not in existing_tags]

    manifest_keys = load_manifest_keys(manifest_path)
    report = {
        "sites_converted": sum(loc.site.source != SiteSource.simulated for loc in live),
        "sites_already_simulated": sum(loc.site.source == SiteSource.simulated for loc in live),
        "sites_skipped": len(skipped_sites),
        "skipped_site_names": [loc.site.name for loc in skipped_sites],
        "chargers_created": 0,
        "chargers_updated": 0,
        "keys_rotated": 0,
        "connectors": sum(len(charger.connectors) for _loc, charger in chargers),
        "connectors_with_kw": sum(
            c.max_power_kw is not None for _loc, charger in chargers for c in charger.connectors
        ),
        "connectors_out_of_order": sum(
            c.out_of_order for _loc, charger in chargers for c in charger.connectors
        ),
        "id_tags_created": len(new_tags),
    }

    manifest_chargers = []
    now = datetime.now(UTC)
    for loc, charger in chargers:
        specs = [c.spec() for c in charger.connectors]
        record = existing.get(charger.identity)
        if record is None:
            report["chargers_created"] += 1
            key = None
            if not dry_run:
                # Route A, as in seed.py: the key counts as installed at the factory, so the
                # charger is Accepted from its first boot (OCPP-J 1.6 s6.2.2).
                _record, key = await ChargePoint.register(
                    charger.identity,
                    registration_status=RegistrationStatus.accepted,
                    simulated=True,
                    site_id=loc.site.id,
                    connector_specs=specs,
                )
        else:
            report["chargers_updated"] += 1
            key = manifest_keys.get(charger.identity)
            # Only a hash is stored, so a key survives a re-run only if the manifest still has
            # it; otherwise the charger could never authenticate again, and it is rotated.
            if key is None or not record.verify_authorization_key(key):
                report["keys_rotated"] += 1
                key = None if dry_run else await record.rotate_authorization_key()
            if not dry_run:
                await record.set({
                    "site_id": loc.site.id,
                    "connector_specs": [spec.model_dump(mode="json") for spec in specs],
                    "updated_at": now,
                })
        manifest_chargers.append(
            {
                "identity": charger.identity,
                "authorization_key": key,
                "site_name": loc.site.name,
                "connectors": [
                    {
                        "connector_id": c.connector_id,
                        "connector_type": c.connector_type,
                        "power_type": c.power_type.value,
                        "max_power_kw": c.max_power_kw,
                        "out_of_order": c.out_of_order,
                        "id_tag": c.id_tag,
                    }
                    for c in charger.connectors
                ],
            }
        )

    if dry_run:
        return report

    for loc in live:
        if loc.site.source != SiteSource.simulated:
            # Only `source` (and updated_at) changes: site_type, the PlugShare station count
            # and everything else stay exactly as 09 imported them, so 12 can revert this.
            await loc.site.set({"source": SiteSource.simulated, "updated_at": now})
    if new_tags:
        await IdTag.insert_many(
            [
                IdTag(id_tag=tag, status=AuthorizationStatus.accepted, parent_id_tag=ID_TAG_PARENT)
                for tag in new_tags
            ]
        )
    write_manifest(
        manifest_path,
        {
            "generated_at": now.isoformat(),
            "source_file": str(source_file),
            "chargers": manifest_chargers,
        },
    )
    return report


def print_report(report, dry_run):
    """Print the §C.6 counts, phrased as a plan on a dry run."""

    def done(verb):
        return f"to {verb}" if dry_run else f"{verb.removesuffix('e')}ed"

    print(
        f"sites {done('convert')}: {report['sites_converted']}"
        f" (already simulated: {report['sites_already_simulated']})"
    )
    print(f"sites skipped: {report['sites_skipped']}")
    for name in report["skipped_site_names"]:
        print(f"  skipped (operator site): {name}")
    print(
        f"chargers {done('create')}: {report['chargers_created']}, "
        f"{done('update')}: {report['chargers_updated']}, "
        f"keys {done('rotate')}: {report['keys_rotated']}"
    )
    print(
        f"connectors: {report['connectors']} (with kW: {report['connectors_with_kw']}, "
        f"out of order: {report['connectors_out_of_order']})"
    )
    print(f"idTags {done('create')}: {report['id_tags_created']}")


async def main(args):
    try:
        entries = json.loads(pathlib.Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Could not read {args.file}: {exc}")
        return 1
    client = None
    try:
        client = await init_db()
        report = await seed(entries, args.manifest, dry_run=args.dry_run, source_file=args.file)
    except SeedError as exc:
        print(f"Aborted, nothing was written: {exc}")
        return 1
    except PyMongoError as exc:
        print(f"Could not reach MongoDB at {display_mongodb_url()}: {type(exc).__name__}")
        print("Start MongoDB, or set MONGODB_URL to point elsewhere.")
        return 1
    finally:
        if client is not None:
            await client.close()

    print_report(report, args.dry_run)
    if args.dry_run:
        print("\nDry run: nothing was written.")
    else:
        print(f"\nWrote {args.manifest} -- the only copy of the demo chargers' keys.")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default=DEFAULT_FILE, help="PlugShare export to seed from")
    parser.add_argument(
        "--manifest", default=DEFAULT_MANIFEST, help="where to write the mock chargers' keys"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would happen and write nothing"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
