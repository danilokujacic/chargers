"""Operator CLI for the charge point registry in models.py.

    python register_charge_point.py register CP001
    python register_charge_point.py list
    python register_charge_point.py rotate CP001
    python register_charge_point.py verify CP001 <40-hex-key>

Needs MongoDB reachable at MONGODB_URL (default mongodb://localhost:27017).
"""

import argparse
import asyncio

from pymongo.errors import DuplicateKeyError, PyMongoError

from models import ChargePoint, init_db, mongodb_url


def print_key(identity, key):
    """Show a freshly generated key, which is never recoverable afterwards."""
    print(f"authorization key for {identity} (shown once): {key}")
    print("Install it on the device, or push it over OCPP with:")
    print(f'    ChangeConfiguration(key="AuthorizationKey", value="{key}")')


async def register(args):
    try:
        charge_point, key = await ChargePoint.register(args.identity)
    except DuplicateKeyError:
        print(f"{args.identity} is already registered (use 'rotate' to issue a new key).")
        return 1
    print(f"Registered {charge_point.identity}, status {charge_point.registration_status}.")
    print_key(charge_point.identity, key)
    return 0


async def rotate(args):
    charge_point = await ChargePoint.find_one(ChargePoint.identity == args.identity)
    if charge_point is None:
        print(f"{args.identity} is not registered.")
        return 1
    key = await charge_point.rotate_authorization_key()
    print(f"Rotated the key for {charge_point.identity}.")
    print_key(charge_point.identity, key)
    return 0


async def verify(args):
    charge_point = await ChargePoint.authenticate(args.identity, args.key)
    if charge_point is None:
        print(f"Rejected: unknown identity or wrong key for {args.identity}.")
        return 1
    print(f"Accepted {charge_point.identity}, status {charge_point.registration_status}.")
    return 0


async def list_charge_points(args):
    charge_points = await ChargePoint.find_all().to_list()
    if not charge_points:
        print("No charge points registered yet.")
        return 0
    for charge_point in charge_points:
        print(
            f"{charge_point.identity:<24} {charge_point.registration_status:<10} "
            f"key set {charge_point.key_updated_at:%Y-%m-%d %H:%M:%S} UTC"
        )
    return 0


async def main(args):
    try:
        client = await init_db()
    except PyMongoError as exc:
        print(f"Could not reach MongoDB at {mongodb_url()}: {type(exc).__name__}")
        print("Start MongoDB, or set MONGODB_URL to point elsewhere.")
        return 1
    try:
        return await args.handler(args)
    finally:
        await client.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(required=True)

    register_parser = subparsers.add_parser("register", help="register a charger and issue a key")
    register_parser.add_argument("identity", help="charge point identity, as used in the OCPP URL")
    register_parser.set_defaults(handler=register)

    rotate_parser = subparsers.add_parser("rotate", help="issue a new key for a charger")
    rotate_parser.add_argument("identity")
    rotate_parser.set_defaults(handler=rotate)

    verify_parser = subparsers.add_parser("verify", help="check an identity and key pair")
    verify_parser.add_argument("identity")
    verify_parser.add_argument("key", help="40-character hexadecimal authorization key")
    verify_parser.set_defaults(handler=verify)

    list_parser = subparsers.add_parser("list", help="list registered chargers, without keys")
    list_parser.set_defaults(handler=list_charge_points)

    return parser.parse_args()


raise SystemExit(asyncio.run(main(parse_args())))
