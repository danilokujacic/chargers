"""Seed the charge point registry with chargers and freshly generated keys.

    python seed.py
    python seed.py --identities CP001 "RDAM 123"

Each charger is registered with a random 20-byte authorization key, or re-keyed
if it is already registered, so running this repeatedly always leaves usable
credentials. The keys are written to charge_point_credentials.json, which stands
in for installing them on the physical devices: the database keeps only hashes,
so this file is the sole copy.

Chargers are seeded as Accepted because the key is installed directly here --
the "setting during or before installation" case of OCPP-J 1.6 s6.2.2, which
the spec calls the desired, secure situation. A charger onboarded over OCPP
instead would start out Pending.
"""

import argparse
import asyncio
import json
import pathlib

from ocpp.v16.enums import RegistrationStatus
from pymongo.errors import PyMongoError

from models import ChargePoint, display_mongodb_url, init_db

DEFAULT_IDENTITIES = ["CP001", "CP002", "CP003"]
CREDENTIALS_FILE = "charge_point_credentials.json"


async def seed(identities):
    """Register or re-key each identity, returning {identity: plaintext key}."""
    credentials = {}
    for identity in identities:
        charge_point = await ChargePoint.find_one(ChargePoint.identity == identity)
        if charge_point is None:
            charge_point, key = await ChargePoint.register(
                identity, registration_status=RegistrationStatus.accepted
            )
            action = "registered"
        else:
            key = await charge_point.rotate_authorization_key()
            action = "re-keyed"
        credentials[charge_point.identity] = key
        print(f"{action:<12} {charge_point.identity:<24} {key}")
    return credentials


async def main(args):
    try:
        client = await init_db()
    except PyMongoError as exc:
        print(f"Could not reach MongoDB at {display_mongodb_url()}: {type(exc).__name__}")
        print("Start MongoDB, or set MONGODB_URL to point elsewhere.")
        return 1
    try:
        credentials = await seed(args.identities)
    finally:
        await client.close()

    path = pathlib.Path(args.credentials)
    path.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"Wrote {len(credentials)} keys to {path} -- this is the only copy.")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--identities",
        nargs="+",
        default=DEFAULT_IDENTITIES,
        help=f"charge point identities to seed (default: {' '.join(DEFAULT_IDENTITIES)})",
    )
    parser.add_argument(
        "--credentials", default=CREDENTIALS_FILE, help="where to write the issued keys"
    )
    return parser.parse_args()


raise SystemExit(asyncio.run(main(parse_args())))
