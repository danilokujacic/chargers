"""Operator CLI for Central-System-initiated actions against a connected charger.

Talks to main.py's admin API rather than to the charger directly: main.py holds the live
WebSocket connections (see CONNECTED_CHARGE_POINTS in main.py), so anything that needs to send
a charger a message has to go through the running server, not around it. The admin API is
plain HTTP, multiplexed onto the same port as the OCPP WebSocket endpoint (see
main.py's handle_admin_request), since this version of websockets only speaks HTTP/1.1 GET on
its listening socket and a second port felt like more machinery than one admin call needs.

    python operate.py remote-start CP001 TAG001
    python operate.py remote-start CP001 TAG001 --connector-id 2
    python operate.py remote-stop CP001 42

Needs ADMIN_TOKEN set to the same value main.py was started with -- there is no operator
identity model in this POC, only a shared secret -- and ADMIN_URL pointing at the server
(default http://localhost:9000).
"""

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ADMIN_URL = "http://localhost:9000"


def admin_url():
    """The base URL of main.py's admin API."""
    return os.environ.get("ADMIN_URL", DEFAULT_ADMIN_URL)


def admin_token():
    """The shared secret main.py requires. Refuses to guess or default it."""
    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        raise SystemExit(
            "ADMIN_TOKEN is not set. Set it to the same value main.py was started with."
        )
    return token


def call_admin(path, **params):
    """GET path on the admin API with params in the query string, return (status, body)."""
    params["token"] = admin_token()
    url = f"{admin_url()}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {admin_url()}: {exc.reason}. Is main.py running?")


def remote_start(args):
    params = {"identity": args.identity, "id_tag": args.id_tag}
    if args.connector_id is not None:
        params["connector_id"] = args.connector_id
    status, body = call_admin("/admin/remote-start", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def remote_stop(args):
    status, body = call_admin(
        "/admin/remote-stop", identity=args.identity, transaction_id=args.transaction_id
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(required=True)

    start_parser = subparsers.add_parser(
        "remote-start", help="ask a connected charger to start a transaction (OCPP 1.6 s5.11)"
    )
    start_parser.add_argument("identity", help="charge point identity")
    start_parser.add_argument("id_tag", help="idTag to authorize the session with")
    start_parser.add_argument(
        "--connector-id",
        type=int,
        default=None,
        help="connector to start on; omit to let the charger choose (never 0)",
    )
    start_parser.set_defaults(handler=remote_start)

    stop_parser = subparsers.add_parser(
        "remote-stop", help="ask a connected charger to stop a transaction (OCPP 1.6 s5.12)"
    )
    stop_parser.add_argument("identity", help="charge point identity")
    stop_parser.add_argument("transaction_id", type=int)
    stop_parser.set_defaults(handler=remote_stop)

    return parser.parse_args()


args = parse_args()
raise SystemExit(args.handler(args))
