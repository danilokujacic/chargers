"""OCPP 1.6 charge point simulator that exercises the central system in main.py.

Runs a scripted session -- boot, heartbeat, authorize, start transaction -- and
reports PASS/FAIL per step. Seed the registry and start main.py first, then:

    python seed.py
    python simulate_charge_point.py
    python simulate_charge_point.py --cp-id CP002 --id-tag TAG042 --stop

The charger authenticates with HTTP Basic auth (OCPP-J 1.6 s6.2.2), using its
identity as the username and its 20-byte authorization key as the password. The
key is read from the file seed.py wrote, standing in for a key installed on the
device; --authorization-key overrides it.

A charger registered with register_charge_point.py (rather than seed.py) starts out
Pending: the Central System pushes it a unique key over ChangeConfiguration before
accepting it (OCPP-J 1.6 s6.2.2, "Setting the key over OCPP"). This simulator plays
along -- it answers that ChangeConfiguration and re-sends BootNotification once -- so
that onboarding flow can be exercised end to end. --reject-key-change makes it refuse
the new key instead, to exercise the flow's failure branch.
"""

import argparse
import asyncio
import json
import logging
import pathlib
from datetime import UTC, datetime
from urllib.parse import quote

import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result, datatypes
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ConfigurationStatus,
    Measurand,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    UnitOfMeasure,
)
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from ocpp_client_auth import basic_auth_header

logging.basicConfig(level=logging.INFO)  # also shows raw OCPP messages

CREDENTIALS_FILE = "charge_point_credentials.json"


def now():
    return datetime.now(UTC).isoformat()


def load_credentials(path):
    """The {identity: key} map seed.py wrote, or an empty map when there is none."""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def resolve_credentials(args):
    """Pick the charge point to act as, returning (identity, authorization key)."""
    if args.authorization_key is not None:
        return args.cp_id or "CP001", args.authorization_key
    credentials = load_credentials(args.credentials)
    if not credentials:
        raise SystemExit(
            f"No credentials in {args.credentials}. Run 'python seed.py' first, "
            f"or pass --authorization-key."
        )
    if args.cp_id is None:
        # Any seeded charger will do; take the first one.
        return next(iter(credentials.items()))
    if args.cp_id not in credentials:
        raise SystemExit(
            f"{args.cp_id} is not in {args.credentials}. "
            f"Seeded chargers: {', '.join(credentials)}"
        )
    return args.cp_id, credentials[args.cp_id]


class SimulatedChargePoint(cp):
    """Client side of the connection.

    Mostly sends calls -- the interesting handlers live in main.py -- but it does answer the
    Central-System-initiated messages a few flows need: ChangeConfiguration for Route B
    onboarding (OCPP-J 1.6 s6.2.2), and RemoteStartTransaction/RemoteStopTransaction (OCPP 1.6
    s5.11/s5.12) for the remote-start flow. key_change_handled lets the boot step wait for the
    ChangeConfiguration exchange to finish before re-sending BootNotification, instead of
    guessing a delay.
    """

    def __init__(
        self,
        id,
        connection,
        authorization_key,
        reject_key_change=False,
        authorize_remote_tx_requests=True,
        reject_remote_start=False,
        default_connector_id=1,
        **kwargs,
    ):
        super().__init__(id, connection, **kwargs)
        self.authorization_key = authorization_key
        self.reject_key_change = reject_key_change
        self.key_change_handled = asyncio.Event()
        self.rotated_key = None  # set only once a new key has actually been accepted
        # OCPP 1.6 s5.11: this mirrors the charger config key AuthorizeRemoteTxRequests. True
        # means the charger authorizes the idTag itself before starting, as if a card had been
        # presented locally; False means it starts immediately and leaves authorization to the
        # Central System's own check when it processes the resulting StartTransaction.
        self.authorize_remote_tx_requests = authorize_remote_tx_requests
        self.reject_remote_start = reject_remote_start
        self.default_connector_id = default_connector_id
        # connector_id -> transaction_id, for the connector a RemoteStopTransaction names by
        # transaction_id alone (OCPP does not carry a connector in that message).
        self.open_transactions = {}

    @on(Action.change_configuration)
    def on_change_configuration(self, key, value, **kwargs):
        try:
            if key != "AuthorizationKey":
                print(f"CHANGE CONFIGURATION: unsupported key {key!r}")
                return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)
            if self.reject_key_change:
                print("CHANGE CONFIGURATION: rejecting AuthorizationKey (--reject-key-change)")
                return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
            normalized = value.strip().upper()
            if len(normalized) != 40:
                raise ValueError
            bytes.fromhex(normalized)  # raises ValueError if not hexadecimal
            self.authorization_key = normalized
            self.rotated_key = normalized
            print(f"CHANGE CONFIGURATION: AuthorizationKey updated to {normalized}")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
        except ValueError:
            print(f"CHANGE CONFIGURATION: malformed AuthorizationKey value {value!r}")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        finally:
            self.key_change_handled.set()

    @on(Action.remote_start_transaction)
    def on_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        if self.reject_remote_start:
            print(f"REMOTE START: rejecting request for {id_tag} (--reject-remote-start)")
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        connector_id = connector_id or self.default_connector_id
        print(
            f"REMOTE START: accepted for id_tag={id_tag} connector={connector_id} "
            f"(AuthorizeRemoteTxRequests={self.authorize_remote_tx_requests})"
        )
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @after(Action.remote_start_transaction)
    async def after_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        """Actually start the transaction, once RemoteStartTransaction.conf has been sent.

        Split from on_remote_start_transaction via the ocpp library's @after hook (see
        main.py's after_boot for why a bare asyncio.create_task cannot give the same ordering
        guarantee): the conf must reach the Central System before this sends anything else.
        """
        if self.reject_remote_start:
            return
        connector_id = connector_id or self.default_connector_id
        if self.authorize_remote_tx_requests:
            auth = await self.call(call.Authorize(id_tag=id_tag), suppress=False)
            if auth.id_tag_info["status"] != AuthorizationStatus.accepted:
                print(
                    f"REMOTE START: {id_tag} not authorized locally "
                    f"({auth.id_tag_info['status']}); not starting"
                )
                return
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.preparing,
                timestamp=now(),
            ),
            suppress=False,
        )
        start = await self.call(
            call.StartTransaction(
                connector_id=connector_id, id_tag=id_tag, meter_start=0, timestamp=now()
            ),
            suppress=False,
        )
        self.open_transactions[connector_id] = start.transaction_id
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.charging,
                timestamp=now(),
            ),
            suppress=False,
        )
        print(f"REMOTE START: transaction_id={start.transaction_id} now Charging")

    @on(Action.remote_stop_transaction)
    def on_remote_stop_transaction(self, transaction_id, **kwargs):
        if transaction_id not in self.open_transactions.values():
            print(f"REMOTE STOP: unknown transaction_id={transaction_id}; rejecting")
            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)
        print(f"REMOTE STOP: accepted for transaction_id={transaction_id}")
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    @after(Action.remote_stop_transaction)
    async def after_remote_stop_transaction(self, transaction_id, **kwargs):
        connector_id = next(
            (c for c, t in self.open_transactions.items() if t == transaction_id), None
        )
        if connector_id is None:
            return
        await self.call(
            call.StopTransaction(
                transaction_id=transaction_id,
                meter_stop=0,
                timestamp=now(),
                reason=Reason.remote,
            ),
            suppress=False,
        )
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=ChargePointStatus.finishing,
                timestamp=now(),
            ),
            suppress=False,
        )
        del self.open_transactions[connector_id]
        print(
            f"REMOTE STOP: transaction_id={transaction_id} stopped, "
            f"connector {connector_id} Finishing"
        )


def check(step, ok, detail):
    print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail}")
    return ok


async def perform_boot(charge_point, args):
    """Send BootNotification, re-sending once if the answer is Pending.

    OCPP-J 1.6 s6.2.2's Route B onboarding has the Central System push a fresh key over
    ChangeConfiguration after a Pending boot, then expects the charger to re-announce itself.
    key_change_handled is set by on_change_configuration the moment that exchange completes
    (accepted or not), so this waits for it instead of sleeping a guessed interval.
    """
    response = await charge_point.call(
        call.BootNotification(charge_point_model=args.model, charge_point_vendor=args.vendor),
        suppress=False,
    )
    if response.status != RegistrationStatus.pending:
        return response
    print("BOOT: Pending -- waiting for the Central System's ChangeConfiguration")
    try:
        await asyncio.wait_for(charge_point.key_change_handled.wait(), timeout=5)
    except asyncio.TimeoutError:
        print("BOOT: no ChangeConfiguration arrived within 5s; re-booting anyway")
    print("BOOT: re-sending BootNotification")
    return await charge_point.call(
        call.BootNotification(charge_point_model=args.model, charge_point_vendor=args.vendor),
        suppress=False,
    )


async def idle_for_remote_control(charge_point, args):
    """Boot, then wait for the Central System to send something, instead of running the
    scripted scenario.

    RemoteStartTransaction and RemoteStopTransaction (OCPP 1.6 s5.11/s5.12) are sent by the
    Central System on ITS OWN schedule, not this script's -- see operate.py, which is meant to
    be run in a second terminal while this one is idling here. Idles regardless of the boot
    outcome, deliberately: a Pending charger stays connected and reachable too (OCPP 1.6 s4.2),
    and testing that RemoteStartTransaction is refused while Pending needs exactly that -- a
    connected-but-not-yet-Accepted charger to aim operate.py at.
    """
    boot = await perform_boot(charge_point, args)
    print(f"BOOT: status={boot.status} interval={boot.interval}s")
    print(
        f"Idling for {args.idle_seconds}s as {charge_point.id}. In another terminal, try:\n"
        f"    python operate.py remote-start {charge_point.id} {args.id_tag}"
    )
    await asyncio.sleep(args.idle_seconds)
    print(f"Idle period over. Open transactions by connector: {charge_point.open_transactions}")
    return boot.status == RegistrationStatus.accepted


async def run_scenario(charge_point, args):
    """Walk the scripted session and return True when every step passed."""
    results = []

    # A charger must boot before the central system accepts anything else. perform_boot
    # handles Route B onboarding transparently: it re-sends once if the first answer is
    # Pending (see instructions/03-commissioning-flow.md).
    boot = await perform_boot(charge_point, args)
    results.append(
        check(
            "BootNotification",
            boot.status == RegistrationStatus.accepted,
            f"status={boot.status} interval={boot.interval}s",
        )
    )

    heartbeat = await charge_point.call(call.Heartbeat(), suppress=False)
    results.append(
        check("Heartbeat", bool(heartbeat.current_time), f"current_time={heartbeat.current_time}")
    )

    await charge_point.call(
        call.StatusNotification(
            connector_id=args.connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.preparing,
            timestamp=now(),
        ),
        suppress=False,
    )
    results.append(check("StatusNotification", True, "connector reported Preparing"))

    authorize = await charge_point.call(call.Authorize(id_tag=args.id_tag), suppress=False)
    authorized = authorize.id_tag_info["status"] == AuthorizationStatus.accepted
    results.append(
        check("Authorize", authorized, f"id_tag={args.id_tag} -> {authorize.id_tag_info}")
    )
    if not authorized:
        print("Authorization rejected, skipping the transaction.")
        return all(results)

    start = await charge_point.call(
        call.StartTransaction(
            connector_id=args.connector_id,
            id_tag=args.id_tag,
            meter_start=args.meter_start,
            timestamp=now(),
        ),
        suppress=False,
    )
    transaction_id = start.transaction_id
    results.append(
        check(
            "StartTransaction",
            start.id_tag_info["status"] == AuthorizationStatus.accepted and transaction_id > 0,
            f"transaction_id={transaction_id} meter_start={args.meter_start}Wh",
        )
    )

    await charge_point.call(
        call.StatusNotification(
            connector_id=args.connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=ChargePointStatus.charging,
            timestamp=now(),
        ),
        suppress=False,
    )
    results.append(check("StatusNotification", True, "connector reported Charging"))

    meter_stop = args.meter_start + args.energy
    await charge_point.call(
        call.MeterValues(
            connector_id=args.connector_id,
            transaction_id=transaction_id,
            meter_value=[
                datatypes.MeterValue(
                    timestamp=now(),
                    sampled_value=[
                        datatypes.SampledValue(
                            value=str(meter_stop),
                            measurand=Measurand.energy_active_import_register,
                            unit=UnitOfMeasure.wh,
                        )
                    ],
                )
            ],
        ),
        suppress=False,
    )
    results.append(check("MeterValues", True, f"reported {meter_stop}Wh on tx {transaction_id}"))

    if not args.stop:
        print(f"Transaction {transaction_id} left open (pass --stop to close it).")
        return all(results)

    stop = await charge_point.call(
        call.StopTransaction(
            transaction_id=transaction_id,
            meter_stop=meter_stop,
            timestamp=now(),
            id_tag=args.id_tag,
            reason=Reason.local,
        ),
        suppress=False,
    )
    results.append(
        check("StopTransaction", True, f"transaction_id={transaction_id} -> {stop.id_tag_info}")
    )

    return all(results)


async def main(args):
    identity, key = resolve_credentials(args)
    # The identity is percent-encoded as one path segment (OCPP-J 1.6 s3.1.1).
    uri = f"{args.url.rstrip('/')}/{quote(identity, safe='')}"
    print(f"Connecting to {uri} as {identity}")
    try:
        async with websockets.connect(
            uri,
            subprotocols=[Subprotocol("ocpp1.6")],
            additional_headers={
                "Authorization": basic_auth_header(identity, key, args.raw_key_password)
            },
        ) as websocket:
            charge_point = SimulatedChargePoint(
                identity,
                websocket,
                authorization_key=key,
                reject_key_change=args.reject_key_change,
                authorize_remote_tx_requests=args.authorize_remote_tx_requests,
                reject_remote_start=args.reject_remote_start,
                default_connector_id=args.connector_id,
            )
            # start() consumes the socket; call() needs it running to get responses.
            listener = asyncio.create_task(charge_point.start())
            try:
                if args.idle_seconds > 0:
                    passed = await idle_for_remote_control(charge_point, args)
                else:
                    passed = await run_scenario(charge_point, args)
            finally:
                listener.cancel()
    except InvalidStatus as exc:
        print(f"Central system refused the handshake: HTTP {exc.response.status_code}.")
        print("The identity or authorization key is wrong; re-run 'python seed.py'.")
        return 1
    except OSError as exc:
        print(f"Could not reach {uri}: {exc}. Is main.py running?")
        return 1

    # Route B rotated this charger's key mid-connection: write it back so the next run of
    # this simulator (a fresh connection) authenticates with it, standing in for a real
    # charger persisting its new key to flash storage (OCPP-J 1.6 s6.2.2).
    if charge_point.rotated_key is not None:
        credentials = load_credentials(args.credentials)
        credentials[identity] = charge_point.rotated_key
        pathlib.Path(args.credentials).write_text(
            json.dumps(credentials, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Persisted rotated key for {identity} to {args.credentials}.")

    print("Scenario passed." if passed else "Scenario failed.")
    return 0 if passed else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="ws://localhost:9000", help="central system websocket URL")
    parser.add_argument(
        "--cp-id", default=None, help="charge point identity (default: first one seeded)"
    )
    parser.add_argument(
        "--authorization-key", help="40-character hex key, instead of the seeded one"
    )
    parser.add_argument(
        "--credentials", default=CREDENTIALS_FILE, help="file holding the seeded keys"
    )
    parser.add_argument(
        "--raw-key-password",
        action="store_true",
        help="send the key as raw bytes, as the example in OCPP-J 1.6 s6.2.2 does",
    )
    parser.add_argument(
        "--reject-key-change",
        action="store_true",
        help="refuse a Route B ChangeConfiguration(AuthorizationKey) instead of accepting it",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=0,
        help="boot, then idle this many seconds waiting for a RemoteStartTransaction/"
        "RemoteStopTransaction (see operate.py) instead of running the scripted scenario",
    )
    parser.add_argument(
        "--authorize-remote-tx-requests",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="mirrors the charger config key of the same name (OCPP 1.6 s5.11): authorize a "
        "RemoteStartTransaction's idTag locally before starting (default), or start "
        "immediately and let the Central System check when it sees the StartTransaction",
    )
    parser.add_argument(
        "--reject-remote-start",
        action="store_true",
        help="answer Rejected to any RemoteStartTransaction instead of Accepted",
    )
    parser.add_argument("--id-tag", default="TAG001", help="RFID tag used to authorize")
    parser.add_argument("--connector-id", type=int, default=1, help="connector to charge on")
    parser.add_argument("--meter-start", type=int, default=0, help="meter reading in Wh at start")
    parser.add_argument("--energy", type=int, default=1500, help="Wh charged during the session")
    parser.add_argument("--vendor", default="PocVendor", help="charge point vendor")
    parser.add_argument("--model", default="PocModel", help="charge point model")
    parser.add_argument("--stop", action="store_true", help="also stop the transaction at the end")
    return parser.parse_args()


raise SystemExit(asyncio.run(main(parse_args())))
