"""Apply database migrations, in order, each exactly once.

    python migrate.py            # apply everything not applied yet
    python migrate.py --status   # list applied and pending migrations, change nothing

MongoDB has no schema to alter: a new field on a document class simply reads as its default on
older documents (PLATFORM_GUIDE.md §10.5). A migration here is anything else a new version needs
done to the database once: creating indexes, backfilling a field so raw queries see it, renaming
a stored value. Each migration is recorded in the `schema_migrations` collection when it
succeeds, so running this on every deploy is safe. Every migration must itself be idempotent, so
that one interrupted halfway through can simply run again.

To add one: write an async function taking the database, append a Migration to MIGRATIONS with
the next number, and never edit or reorder one that has been released.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo.errors import PyMongoError

from models import display_mongodb_url, get_database, init_db

MIGRATIONS_COLLECTION = "schema_migrations"


@dataclass(frozen=True)
class Migration:
    id: str
    description: str
    apply: object  # async def (database) -> str, a one-line summary of what it did


async def ensure_indexes(database):
    """init_db() creates every declared index on each start; this records that it happened
    and reports what exists, so a deploy log shows the indexes are in place."""
    names = sorted(await database.list_collection_names())
    total = 0
    for name in names:
        total += len(await (await database[name].list_indexes()).to_list())
    return f"{total} indexes across {len(names)} collections"


async def backfill_charge_point_fields(database):
    """Give chargers registered before task 11 an explicit simulated=false and an empty
    connector_specs list. The application reads the defaults anyway; the backfill makes raw
    queries such as {simulated: false} match every real charger."""
    collection = database["charge_points"]
    simulated = await collection.update_many(
        {"simulated": {"$exists": False}}, {"$set": {"simulated": False}}
    )
    specs = await collection.update_many(
        {"connector_specs": {"$exists": False}}, {"$set": {"connector_specs": []}}
    )
    return f"simulated set on {simulated.modified_count}, connector_specs on {specs.modified_count}"


MIGRATIONS = [
    Migration("0001_indexes", "Create every collection's indexes", ensure_indexes),
    Migration(
        "0002_charge_point_fields",
        "Backfill ChargePoint.simulated and .connector_specs on older chargers",
        backfill_charge_point_fields,
    ),
]


async def applied_ids(database):
    records = await database[MIGRATIONS_COLLECTION].find({}, {"_id": 1}).to_list()
    return {record["_id"] for record in records}


async def run(status_only):
    database = get_database()
    done = await applied_ids(database)
    pending = [m for m in MIGRATIONS if m.id not in done]
    if status_only:
        for migration in MIGRATIONS:
            mark = "applied" if migration.id in done else "pending"
            print(f"{mark:<8} {migration.id}  {migration.description}")
        return 0
    if not pending:
        print(f"Database is up to date ({len(MIGRATIONS)} migrations applied).")
        return 0
    for migration in pending:
        summary = await migration.apply(database)
        await database[MIGRATIONS_COLLECTION].insert_one(
            {
                "_id": migration.id,
                "description": migration.description,
                "summary": summary,
                "applied_at": datetime.now(UTC),
            }
        )
        print(f"applied  {migration.id}: {summary}")
    return 0


async def main(args):
    client = None
    try:
        client = await init_db()
        return await run(args.status)
    except PyMongoError as exc:
        print(f"Could not reach MongoDB at {display_mongodb_url()}: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if client is not None:
            await client.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--status", action="store_true", help="list applied and pending migrations only"
    )
    return parser.parse_args()


raise SystemExit(asyncio.run(main(parse_args())))
