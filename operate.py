"""Operator CLI for Central-System-initiated actions against a connected charger.

Talks to main.py's admin API rather than to the charger directly: main.py holds the live
WebSocket connections (see CONNECTED_CHARGE_POINTS in main.py), so anything that needs to send
a charger a message has to go through the running server, not around it. The admin API is
plain HTTP, multiplexed onto the same port as the OCPP WebSocket endpoint (see
main.py's handle_admin_request), since this version of websockets only speaks HTTP/1.1 GET on
its listening socket and a second port felt like more machinery than one admin call needs.

    python operate.py remote-start CP001 TAG001
    python operate.py remote-start CP001 TAG001 --connector-id 2
    python operate.py remote-start CP001 TAG001 --connector-id 1 --limit 16 --unit A
    python operate.py remote-stop CP001 42
    python operate.py change-availability CP001 1 Inoperative
    python operate.py change-availability CP001 0 Operative
    python operate.py reset CP001 Soft
    python operate.py unlock-connector CP001 1
    python operate.py set-charging-profile CP001 0 --purpose TxDefaultProfile --limit 16
    python operate.py set-charging-profile CP001 1 --profile-file profile.json
    python operate.py clear-charging-profile CP001 --id 1
    python operate.py clear-charging-profile CP001 --purpose TxDefaultProfile --stack-level 0
    python operate.py clear-charging-profile CP001 --all
    python operate.py get-composite-schedule CP001 1 3600 --unit W
    python operate.py charging-profiles CP001
    python operate.py trigger-message CP001 StatusNotification
    python operate.py trigger-message CP001 MeterValues --connector-id 1
    python operate.py get-configuration CP001
    python operate.py get-configuration CP001 --keys HeartbeatInterval,NumberOfConnectors
    python operate.py change-configuration CP001 HeartbeatInterval 60
    python operate.py clear-cache CP001
    python operate.py reserve-now CP001 1 TAG001 2026-01-01T12:00:00+00:00
    python operate.py cancel-reservation CP001 1
    python operate.py get-local-list-version CP001
    python operate.py send-local-list CP001 --id-tags TAG001,TAG002
    python operate.py send-local-list CP001 --update-type Differential --remove-id-tags TAG002
    python operate.py fault-history CP001
    python operate.py fault-history CP001 --connector-id 1
    python operate.py update-firmware CP001 example-1.0.0.txt 2026-01-01T00:00:00+00:00
    python operate.py firmware-status CP001
    python operate.py get-diagnostics CP001
    python operate.py diagnostics-status CP001
    python operate.py create-site "Petrol Podgorica" gas_station 42.4304 19.2594
    python operate.py set-charge-point-location CP001 --site-id 670f1234abcd5678ef901234
    python operate.py set-charge-point-location CP001 --latitude 42.43 --longitude 19.26

Needs ADMIN_TOKEN set to the same value main.py was started with -- there is no operator
identity model in this POC, only a shared secret -- and ADMIN_URL pointing at the server
(default http://localhost:9000).

create-site / set-charge-point-location (07-public-map-platform.md §A/§B) are the two
exceptions to this file's opening paragraph: they are pure database writes, not messages to a
charger, so they work whether or not the charger in question is currently connected -- a
physical location doesn't depend on connectivity. They stay on the same admin-token trust
boundary as everything else here anyway, rather than a second, separate mechanism.
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


def profile_from_args(args, purpose=None):
    """The charging profile the operator described, as a JSON string for the admin API.

    Either a whole profile (--profile-file / --profile-json, camelCase exactly as in the OCPP
    spec), or the common simple case built from flags: one constant limit, counted from the
    start of the transaction (or, for a ChargePointMaxProfile, from now) with no end.
    Returns None when the operator gave neither.
    """
    if args.profile_file is not None:
        with open(args.profile_file, encoding="utf-8") as handle:
            return handle.read()
    if args.profile_json is not None:
        return args.profile_json
    if args.limit is None:
        return None
    profile = {
        "chargingProfileId": args.profile_id,
        "stackLevel": args.stack_level,
        "chargingProfilePurpose": purpose or args.purpose,
        "chargingProfileKind": "Relative",
        "chargingSchedule": {
            "chargingRateUnit": args.unit,
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": args.limit}],
        },
    }
    return json.dumps(profile)


def add_profile_arguments(parser, with_purpose):
    parser.add_argument("--profile-file", help="JSON file holding a whole ChargingProfile")
    parser.add_argument("--profile-json", help="a whole ChargingProfile, as a JSON string")
    parser.add_argument("--limit", type=float, help="simple profile: one constant limit")
    parser.add_argument("--unit", choices=["A", "W"], default="A", help="unit of --limit")
    parser.add_argument("--profile-id", type=int, default=1, help="simple profile: its id")
    parser.add_argument("--stack-level", type=int, default=0, help="simple profile: stack level")
    if with_purpose:
        parser.add_argument(
            "--purpose",
            choices=["ChargePointMaxProfile", "TxDefaultProfile", "TxProfile"],
            default="TxDefaultProfile",
            help="simple profile: what the limit is for",
        )


def remote_start(args):
    params = {"identity": args.identity, "id_tag": args.id_tag}
    if args.connector_id is not None:
        params["connector_id"] = args.connector_id
    profile = profile_from_args(args, purpose="TxProfile")
    if profile is not None:
        params["profile"] = profile
    status, body = call_admin("/admin/remote-start", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def remote_stop(args):
    status, body = call_admin(
        "/admin/remote-stop", identity=args.identity, transaction_id=args.transaction_id
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def change_availability(args):
    status, body = call_admin(
        "/admin/change-availability",
        identity=args.identity, connector_id=args.connector_id, type=args.type,
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def reset(args):
    status, body = call_admin("/admin/reset", identity=args.identity, type=args.type)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def set_charging_profile(args):
    profile = profile_from_args(args)
    if profile is None:
        raise SystemExit("give a profile: --profile-file, --profile-json, or --limit")
    status, body = call_admin(
        "/admin/set-charging-profile",
        identity=args.identity, connector_id=args.connector_id, profile=profile,
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def clear_charging_profile(args):
    params = {"identity": args.identity}
    for name, key in (
        ("id", "id"), ("connector_id", "connector_id"), ("purpose", "purpose"),
        ("stack_level", "stack_level"),
    ):
        if getattr(args, name) is not None:
            params[key] = getattr(args, name)
    if len(params) == 1 and not args.all:
        raise SystemExit("this would clear every profile on the charger: pass --all to confirm")
    status, body = call_admin("/admin/clear-charging-profile", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def get_composite_schedule(args):
    params = {
        "identity": args.identity, "connector_id": args.connector_id, "duration": args.duration,
    }
    if args.unit is not None:
        params["unit"] = args.unit
    status, body = call_admin("/admin/get-composite-schedule", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def charging_profiles(args):
    params = {"identity": args.identity}
    if args.connector_id is not None:
        params["connector_id"] = args.connector_id
    status, body = call_admin("/admin/charging-profiles", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def trigger_message(args):
    params = {"identity": args.identity, "message": args.message}
    if args.connector_id is not None:
        params["connector_id"] = args.connector_id
    status, body = call_admin("/admin/trigger-message", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def unlock_connector(args):
    status, body = call_admin(
        "/admin/unlock-connector", identity=args.identity, connector_id=args.connector_id
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def get_configuration(args):
    params = {"identity": args.identity}
    if args.keys is not None:
        params["keys"] = args.keys
    status, body = call_admin("/admin/get-configuration", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def change_configuration(args):
    status, body = call_admin(
        "/admin/change-configuration", identity=args.identity, key=args.key, value=args.value
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def clear_cache(args):
    status, body = call_admin("/admin/clear-cache", identity=args.identity)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def reserve_now(args):
    params = {
        "identity": args.identity,
        "connector_id": args.connector_id,
        "id_tag": args.id_tag,
        "expiry_date": args.expiry_date,
    }
    if args.parent_id_tag is not None:
        params["parent_id_tag"] = args.parent_id_tag
    status, body = call_admin("/admin/reserve-now", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def cancel_reservation(args):
    status, body = call_admin(
        "/admin/cancel-reservation", identity=args.identity, reservation_id=args.reservation_id
    )
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def get_local_list_version(args):
    status, body = call_admin("/admin/get-local-list-version", identity=args.identity)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def send_local_list(args):
    params = {"identity": args.identity, "update_type": args.update_type}
    if args.id_tags is not None:
        params["id_tags"] = args.id_tags
    if args.remove_id_tags is not None:
        params["remove_id_tags"] = args.remove_id_tags
    status, body = call_admin("/admin/send-local-list", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def fault_history(args):
    params = {"identity": args.identity}
    if args.connector_id is not None:
        params["connector_id"] = args.connector_id
    status, body = call_admin("/admin/fault-history", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def update_firmware(args):
    params = {
        "identity": args.identity,
        "filename": args.filename,
        "retrieve_date": args.retrieve_date,
    }
    if args.retries is not None:
        params["retries"] = args.retries
    if args.retry_interval is not None:
        params["retry_interval"] = args.retry_interval
    status, body = call_admin("/admin/update-firmware", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def firmware_status(args):
    status, body = call_admin("/admin/firmware-status", identity=args.identity)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def get_diagnostics(args):
    params = {"identity": args.identity}
    if args.retries is not None:
        params["retries"] = args.retries
    if args.retry_interval is not None:
        params["retry_interval"] = args.retry_interval
    if args.start_time is not None:
        params["start_time"] = args.start_time
    if args.stop_time is not None:
        params["stop_time"] = args.stop_time
    status, body = call_admin("/admin/get-diagnostics", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def diagnostics_status(args):
    status, body = call_admin("/admin/diagnostics-status", identity=args.identity)
    print(f"HTTP {status}: {body}")
    return 0 if status == 200 else 1


def create_site(args):
    params = {
        "name": args.name,
        "site_type": args.site_type,
        "latitude": args.latitude,
        "longitude": args.longitude,
    }
    if args.address is not None:
        params["address"] = args.address
    status, body = call_admin("/admin/create-site", **params)
    print(f"HTTP {status}: {body}")
    return 0 if status == 201 else 1


def set_charge_point_location(args):
    params = {"identity": args.identity}
    if args.site_id is not None:
        params["site_id"] = args.site_id
    if args.latitude is not None:
        params["latitude"] = args.latitude
    if args.longitude is not None:
        params["longitude"] = args.longitude
    status, body = call_admin("/admin/set-charge-point-location", **params)
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
    add_profile_arguments(start_parser, with_purpose=False)
    start_parser.set_defaults(handler=remote_start)

    stop_parser = subparsers.add_parser(
        "remote-stop", help="ask a connected charger to stop a transaction (OCPP 1.6 s5.12)"
    )
    stop_parser.add_argument("identity", help="charge point identity")
    stop_parser.add_argument("transaction_id", type=int)
    stop_parser.set_defaults(handler=remote_stop)

    availability_parser = subparsers.add_parser(
        "change-availability",
        help="make a connector, or the whole charge point, Operative or Inoperative (s5.2)",
    )
    availability_parser.add_argument("identity", help="charge point identity")
    availability_parser.add_argument(
        "connector_id", type=int, help="0 for the Charge Point and every connector"
    )
    availability_parser.add_argument("type", choices=["Operative", "Inoperative"])
    availability_parser.set_defaults(handler=change_availability)

    reset_parser = subparsers.add_parser(
        "reset", help="reboot a connected charger, gracefully (Soft) or now (Hard) (s5.14)"
    )
    reset_parser.add_argument("identity", help="charge point identity")
    reset_parser.add_argument("type", choices=["Soft", "Hard"])
    reset_parser.set_defaults(handler=reset)

    set_profile_parser = subparsers.add_parser(
        "set-charging-profile",
        help="limit the power/current a charger may deliver, over time (s5.16)",
    )
    set_profile_parser.add_argument("identity", help="charge point identity")
    set_profile_parser.add_argument(
        "connector_id", type=int, help="0 for the whole charger (or a default for every connector)"
    )
    add_profile_arguments(set_profile_parser, with_purpose=True)
    set_profile_parser.set_defaults(handler=set_charging_profile)

    clear_profile_parser = subparsers.add_parser(
        "clear-charging-profile",
        help="remove charging profiles matching every criterion given (s5.5)",
    )
    clear_profile_parser.add_argument("identity", help="charge point identity")
    clear_profile_parser.add_argument("--id", type=int, help="the profile's chargingProfileId")
    clear_profile_parser.add_argument("--connector-id", type=int)
    clear_profile_parser.add_argument(
        "--purpose", choices=["ChargePointMaxProfile", "TxDefaultProfile", "TxProfile"]
    )
    clear_profile_parser.add_argument("--stack-level", type=int)
    clear_profile_parser.add_argument(
        "--all", action="store_true", help="required to clear with no other criterion"
    )
    clear_profile_parser.set_defaults(handler=clear_charging_profile)

    composite_parser = subparsers.add_parser(
        "get-composite-schedule",
        help="ask a charger what limits it will actually apply over the next N seconds (s5.7)",
    )
    composite_parser.add_argument("identity", help="charge point identity")
    composite_parser.add_argument("connector_id", type=int, help="0 for the whole charger")
    composite_parser.add_argument("duration", type=int, help="seconds to look ahead")
    composite_parser.add_argument("--unit", choices=["A", "W"], help="force amps or watts")
    composite_parser.set_defaults(handler=get_composite_schedule)

    profiles_parser = subparsers.add_parser(
        "charging-profiles", help="list the charging profiles installed on a charger (database)"
    )
    profiles_parser.add_argument("identity", help="charge point identity")
    profiles_parser.add_argument("--connector-id", type=int)
    profiles_parser.set_defaults(handler=charging_profiles)

    trigger_parser = subparsers.add_parser(
        "trigger-message",
        help="ask a connected charger to send one of its messages now (s5.16); ok while Pending",
    )
    trigger_parser.add_argument("identity", help="charge point identity")
    trigger_parser.add_argument(
        "message",
        choices=[
            "BootNotification", "DiagnosticsStatusNotification", "FirmwareStatusNotification",
            "Heartbeat", "MeterValues", "StatusNotification",
        ],
    )
    trigger_parser.add_argument(
        "--connector-id", type=int, help="only for MeterValues/StatusNotification; default all"
    )
    trigger_parser.set_defaults(handler=trigger_message)

    unlock_parser = subparsers.add_parser(
        "unlock-connector",
        help="ask a connected charger to physically unlock a connector (s5.17)",
    )
    unlock_parser.add_argument("identity", help="charge point identity")
    unlock_parser.add_argument("connector_id", type=int, help="never 0")
    unlock_parser.set_defaults(handler=unlock_connector)

    get_config_parser = subparsers.add_parser(
        "get-configuration", help="read a connected charger's configuration keys (s5.9)"
    )
    get_config_parser.add_argument("identity", help="charge point identity")
    get_config_parser.add_argument(
        "--keys", default=None, help="comma-separated key names; omit for every key"
    )
    get_config_parser.set_defaults(handler=get_configuration)

    change_config_parser = subparsers.add_parser(
        "change-configuration", help="set one configuration key on a connected charger (s5.6)"
    )
    change_config_parser.add_argument("identity", help="charge point identity")
    change_config_parser.add_argument("key")
    change_config_parser.add_argument("value")
    change_config_parser.set_defaults(handler=change_configuration)

    clear_cache_parser = subparsers.add_parser(
        "clear-cache", help="empty a connected charger's Authorization Cache (s5.4)"
    )
    clear_cache_parser.add_argument("identity", help="charge point identity")
    clear_cache_parser.set_defaults(handler=clear_cache)

    reserve_parser = subparsers.add_parser(
        "reserve-now", help="hold a connector for one idTag until used/cancelled/expired (s5.15)"
    )
    reserve_parser.add_argument("identity", help="charge point identity")
    reserve_parser.add_argument("connector_id", type=int, help="0 for no specific connector")
    reserve_parser.add_argument("id_tag", help="idTag the reservation is held for")
    reserve_parser.add_argument("expiry_date", help="OCPP dateTime string, e.g. an ISO 8601 UTC")
    reserve_parser.add_argument("--parent-id-tag", default=None)
    reserve_parser.set_defaults(handler=reserve_now)

    cancel_reservation_parser = subparsers.add_parser(
        "cancel-reservation", help="give up a reservation before it would otherwise end (s5.3)"
    )
    cancel_reservation_parser.add_argument("identity", help="charge point identity")
    cancel_reservation_parser.add_argument("reservation_id", type=int)
    cancel_reservation_parser.set_defaults(handler=cancel_reservation)

    get_local_list_parser = subparsers.add_parser(
        "get-local-list-version",
        help="read a connected charger's current Local Authorization List version (s5.7)",
    )
    get_local_list_parser.add_argument("identity", help="charge point identity")
    get_local_list_parser.set_defaults(handler=get_local_list_version)

    send_local_list_parser = subparsers.add_parser(
        "send-local-list", help="push a Local Authorization List update (s5.8)"
    )
    send_local_list_parser.add_argument("identity", help="charge point identity")
    send_local_list_parser.add_argument(
        "--update-type", choices=["Full", "Differential"], default="Full"
    )
    send_local_list_parser.add_argument(
        "--id-tags",
        default=None,
        help="comma-separated idTags to add/update; omit on a Full update for every idTag on "
        "record",
    )
    send_local_list_parser.add_argument(
        "--remove-id-tags",
        default=None,
        help="comma-separated idTags to remove; only meaningful with --update-type Differential",
    )
    send_local_list_parser.set_defaults(handler=send_local_list)

    fault_history_parser = subparsers.add_parser(
        "fault-history", help="read a charger's recorded faults, newest first (s4.9)"
    )
    fault_history_parser.add_argument("identity", help="charge point identity")
    fault_history_parser.add_argument(
        "--connector-id", type=int, default=None, help="omit for every connector"
    )
    fault_history_parser.set_defaults(handler=fault_history)

    update_firmware_parser = subparsers.add_parser(
        "update-firmware", help="ask a connected charger to fetch and install firmware (s5.16)"
    )
    update_firmware_parser.add_argument("identity", help="charge point identity")
    update_firmware_parser.add_argument(
        "filename", help="must already exist in this project's firmware_files/ directory"
    )
    update_firmware_parser.add_argument("retrieve_date", help="OCPP dateTime string")
    update_firmware_parser.add_argument("--retries", type=int, default=None)
    update_firmware_parser.add_argument("--retry-interval", type=int, default=None)
    update_firmware_parser.set_defaults(handler=update_firmware)

    firmware_status_parser = subparsers.add_parser(
        "firmware-status", help="read a charger's most recent firmware update progress"
    )
    firmware_status_parser.add_argument("identity", help="charge point identity")
    firmware_status_parser.set_defaults(handler=firmware_status)

    get_diagnostics_parser = subparsers.add_parser(
        "get-diagnostics", help="ask a connected charger to upload a diagnostics archive (s5.1)"
    )
    get_diagnostics_parser.add_argument("identity", help="charge point identity")
    get_diagnostics_parser.add_argument("--retries", type=int, default=None)
    get_diagnostics_parser.add_argument("--retry-interval", type=int, default=None)
    get_diagnostics_parser.add_argument("--start-time", default=None, help="OCPP dateTime string")
    get_diagnostics_parser.add_argument("--stop-time", default=None, help="OCPP dateTime string")
    get_diagnostics_parser.set_defaults(handler=get_diagnostics)

    diagnostics_status_parser = subparsers.add_parser(
        "diagnostics-status", help="read a charger's most recent diagnostics upload progress"
    )
    diagnostics_status_parser.add_argument("identity", help="charge point identity")
    diagnostics_status_parser.set_defaults(handler=diagnostics_status)

    create_site_parser = subparsers.add_parser(
        "create-site", help="create a new operator-managed Site (07-public-map-platform.md §A)"
    )
    create_site_parser.add_argument("name")
    create_site_parser.add_argument(
        "site_type", choices=["gas_station", "hotel", "parking", "other"]
    )
    create_site_parser.add_argument("latitude", type=float)
    create_site_parser.add_argument("longitude", type=float)
    create_site_parser.add_argument("--address", default=None)
    create_site_parser.set_defaults(handler=create_site)

    set_location_parser = subparsers.add_parser(
        "set-charge-point-location",
        help="assign a charger to a Site, or give it its own coordinates (07 §A/§B)",
    )
    set_location_parser.add_argument("identity", help="charge point identity")
    set_location_parser.add_argument(
        "--site-id", default=None, help="an existing Site's id, from create-site's output"
    )
    set_location_parser.add_argument("--latitude", type=float, default=None)
    set_location_parser.add_argument("--longitude", type=float, default=None)
    set_location_parser.set_defaults(handler=set_charge_point_location)

    return parser.parse_args()


args = parse_args()
raise SystemExit(args.handler(args))
