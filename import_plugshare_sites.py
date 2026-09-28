#!/usr/bin/env python3
"""Import PlugShare charging locations into the Site collection."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from models import SiteSource, SiteType, Site, init_db
from pymongo.errors import PyMongoError


def parse_args():
    parser = argparse.ArgumentParser(description="Import PlugShare sites into MongoDB")
    parser.add_argument(
        "--file",
        default="montenegro_only.json",
        help="Path to PlugShare export JSON file (default: montenegro_only.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report what would happen without writing to MongoDB",
    )
    return parser.parse_args()


async def main(args):
    try:
        await init_db()
    except PyMongoError as e:
        print(f"Error: MongoDB unreachable: {e}", file=sys.stderr)
        return 1

    # Load the JSON file
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        return 1

    try:
        with open(file_path) as f:
            entries = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading {args.file}: {e}", file=sys.stderr)
        return 1

    created = 0
    updated = 0
    skipped = 0
    skipped_entries = []

    for entry in entries:
        # 1. Skip if coming_soon is True
        if entry.get("coming_soon"):
            skipped += 1
            skipped_entries.append((entry.get("name", entry.get("id")), "coming soon"))
            continue

        # 2. Validate required fields
        name = entry.get("name")
        latitude = entry.get("latitude")
        longitude = entry.get("longitude")

        if not name or latitude is None or longitude is None:
            skipped += 1
            skipped_entries.append((entry.get("name", entry.get("id")), "invalid"))
            continue

        external_id = str(entry["id"])
        address = entry.get("address")
        connector_types = entry.get("connector_types", [])
        external_url = entry.get("url")
        external_charge_point_count = len(entry.get("stations", []))

        # 3. Look up existing Site by external_id
        existing_site = await Site.find_one(Site.external_id == external_id)

        if existing_site:
            # Update existing site
            if not args.dry_run:
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
            # Create new site
            if not args.dry_run:
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

    # Report results
    print(f"created: {created}")
    print(f"updated: {updated}")
    print(f"skipped: {skipped}")

    if skipped_entries:
        print("\nSkipped entries:")
        for name, reason in skipped_entries:
            print(f"  skipped ({reason}): {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
