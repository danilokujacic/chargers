import asyncio
import base64
import binascii
import json
import logging
import os
from datetime import UTC, datetime
from http import HTTPStatus
from urllib.parse import parse_qs, unquote, urlsplit

import websockets
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    RegistrationStatus,
)
from pymongo.errors import PyMongoError
from websockets.exceptions import ConnectionClosed
from websockets.headers import build_www_authenticate_basic
from websockets.typing import Subprotocol

from commissioning import PendingChargerError, boot_status, ensure_commissioned, onboard
from connector_state_machine import IllegalTransition
from models import ChargePoint as ChargePointRecord
from models import ConnectorStatus, IdTag, Transaction
from models import authorization_key_from_basic_password, init_db, mongodb_url

REALM = "ocpp"

# The operator API (remote-start/stop, see operate.py) is multiplexed onto this same port,
# since the websockets version in use only speaks HTTP/1.1 GET on its listening socket and
# adding a second port/dependency for one admin call is more machinery than this needs. There
# is no operator-identity model in this POC, so a single shared secret gates it; leaving it
# unset disables the admin API entirely (404 on every admin path) rather than allowing no-auth
# access by default.
ADMIN_PATH_PREFIX = "/admin/"
ADMIN_TOKEN_ENV_VAR = "ADMIN_TOKEN"

# OCPP 1.6 s4.2: on Accepted this is the heartbeat interval; on any other status it is "the
# minimum wait time before sending a next BootNotification request". One value serves both
# meanings here since the spec does not require them to differ.
HEARTBEAT_INTERVAL_SECONDS = 30

# Live connections, keyed by charge point identity, so a Central-System-initiated message (a
# remote start/stop, a commissioning push) can find the socket to send it on. Populated in
# on_connect and cleaned up when the connection closes.
CONNECTED_CHARGE_POINTS = {}

logging.basicConfig(level=logging.INFO)  # also shows raw OCPP messages


def now():
    return datetime.now(UTC).isoformat()


def parse_ocpp_timestamp(timestamp):
    """An OCPP dateTime string as an aware Python datetime.

    Timestamps in OCPP messages are the charger's own clock, not this Central System's: a
    charger that was offline delivers StartTransaction/StopTransaction/MeterValues late, with
    timestamps hours in the past, and billing must reflect when charging actually happened.
    Python 3.11+'s fromisoformat handles the "Z" suffix these commonly use.
    """
    return datetime.fromisoformat(timestamp)


class MyChargePoint(cp):
    def __init__(self, id, connection, record=None, connector_states=None, **kwargs):
        super().__init__(id, connection, **kwargs)
        self.record = record  # the registry document this connection authenticated as
        # ConnectorState objects for this charger's connectors, preloaded from ConnectorStatus
        # at connect time (see on_connect) so status is validated against what was last known
        # even across a Central System restart; a connector not seen before is added lazily.
        self.connector_states = {} if connector_states is None else connector_states
        # The Task running commissioning.onboard() after a Pending boot, if one is still in
        # flight. on_boot awaits it before answering a LATER BootNotification on this same
        # connection, so a fast re-boot (see simulate_charge_point.py's perform_boot) cannot
        # read registration_status before onboard() has actually updated it -- after_boot only
        # guarantees ChangeConfiguration goes out after the conf that triggered it, not that it
        # has finished before the charger's next message arrives.
        self._onboarding_task = None

    async def get_connector_state(self, connector_id):
        """This connector's live ConnectorState, loading it from persistence on first use."""
        state = self.connector_states.get(connector_id)
        if state is None:
            record = await ConnectorStatus.get_or_create(self.id, connector_id)
            state = record.to_connector_state()
            self.connector_states[connector_id] = state
        return state

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_vendor, charge_point_model, **kwargs):
        print("BOOT:", charge_point_vendor, charge_point_model, kwargs)
        if self._onboarding_task is not None:
            await self._onboarding_task
        status = RegistrationStatus.accepted
        if self.record is not None:
            await self.record.record_boot(
                charge_point_vendor=charge_point_vendor,
                charge_point_model=charge_point_model,
                **kwargs,
            )
            status = await boot_status(self.record)
        return call_result.BootNotification(
            current_time=now(), interval=HEARTBEAT_INTERVAL_SECONDS, status=status
        )

    @after(Action.boot_notification)
    async def after_boot(self, **kwargs):
        """Run Route B onboarding once BootNotification.conf has actually reached the charger.

        The ocpp library calls this only after the response is sent (see ocpp.routing.after),
        which is why commissioning.onboard's ChangeConfiguration cannot race ahead of the boot
        response on the wire -- a bare asyncio.create_task from inside on_boot could not give
        that guarantee, since it may start running at on_boot's first await, before the
        BootNotification.conf write has completed.

        The library runs this as a background task (asyncio.ensure_future) rather than
        awaiting it, so on_boot records a reference to it (via asyncio.current_task) for a
        later BootNotification on this same connection to wait on.
        """
        if self.record is None or self.record.registration_status != RegistrationStatus.pending:
            return
        self._onboarding_task = asyncio.current_task()
        try:
            await onboard(self, self.record)
        except Exception:
            logging.exception("commissioning %s failed", self.record.identity)
        finally:
            self._onboarding_task = None

    @on(Action.heartbeat)
    def on_heartbeat(self):
        return call_result.Heartbeat(current_time=now())

    @on(Action.status_notification)
    async def on_status(self, connector_id, error_code, status, **kwargs):
        # Incoming call payloads arrive as plain strings (e.g. "Charging"), not already-cast
        # enum instances -- coerce before touching connector_state_machine, which calls
        # .value on both sides of an illegal transition to build its error message.
        status = ChargePointStatus(status)
        error_code = ChargePointErrorCode(error_code)
        state = await self.get_connector_state(connector_id)
        try:
            state.change_to(status)
            print(
                f"STATUS: connector={connector_id} status={status.value} "
                f"error={error_code.value}"
            )
        except IllegalTransition as exc:
            # The charger is the authority on its own hardware: a firmware quirk or a replayed
            # message can report a transition OCPP 1.6 s4.9's table does not list. Record what
            # was actually reported and flag the anomaly rather than silently overwrite it or
            # reject the message -- StatusNotification.conf carries no status field, so there
            # is no way to tell a charger it was wrong.
            logging.warning(
                "ILLEGAL STATUS TRANSITION: %s connector %s: %s", self.id, connector_id, exc
            )
            state.force_status(status)
        record = await ConnectorStatus.get_or_create(self.id, connector_id)
        await record.apply(
            state,
            error_code=error_code,
            info=kwargs.get("info"),
            vendor_id=kwargs.get("vendor_id"),
            vendor_error_code=kwargs.get("vendor_error_code"),
        )
        return call_result.StatusNotification()

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        id_tag_info = await IdTag.authorize(id_tag)
        print(f"AUTHORIZE: {id_tag} -> {id_tag_info.status}")
        return call_result.Authorize(id_tag_info=id_tag_info)

    @on(Action.start_transaction)
    async def on_start(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        id_tag_info = await IdTag.authorize(id_tag)
        transaction = await Transaction.start(
            charge_point_identity=self.id,
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
            started_at=parse_ocpp_timestamp(timestamp),
        )
        print(
            f"START TX: connector={connector_id} id_tag={id_tag} meter_start={meter_start} "
            f"-> transaction_id={transaction.transaction_id} ({id_tag_info.status})"
        )
        if id_tag_info.status != AuthorizationStatus.accepted:
            # OCPP 1.6 s4.9 code C5: the charger is expected to move the connector to
            # SuspendedEVSE on its own next StatusNotification, since "transaction is
            # invalidated by the AuthorizationStatus in a StartTransaction.conf". The
            # transaction is recorded regardless -- the charger already started delivering (or
            # is about to), and the Central System's role here is to record reality, not to
            # pretend the session never happened.
            logging.warning(
                "START TX: %s connector %s started on a non-Accepted idTag %s (%s)",
                self.id, connector_id, id_tag, id_tag_info.status,
            )
        return call_result.StartTransaction(
            transaction_id=transaction.transaction_id, id_tag_info=id_tag_info
        )

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kwargs):
        print(f"METER: connector={connector_id} transaction_id={transaction_id} {meter_value}")
        if transaction_id is not None:
            transaction = await Transaction.find_one(Transaction.transaction_id == transaction_id)
            if transaction is not None:
                await transaction.add_meter_values(meter_value)
            else:
                logging.warning(
                    "METER: %s reported readings for unknown transaction_id %s",
                    self.id, transaction_id,
                )
        else:
            # A standalone, clock-driven reading not tied to any transaction (OCPP 1.6 s4.7
            # allows this) -- store it against the connector instead of discarding it.
            record = await ConnectorStatus.get_or_create(self.id, connector_id)
            await record.record_meter_values(meter_value)
        return call_result.MeterValues()

    @on(Action.stop_transaction)
    async def on_stop(self, meter_stop, timestamp, transaction_id, **kwargs):
        reason = kwargs.get("reason")
        id_tag = kwargs.get("id_tag")
        stopped_at = parse_ocpp_timestamp(timestamp)
        print(f"STOP TX: transaction_id={transaction_id} meter_stop={meter_stop} id_tag={id_tag}")
        transaction = await Transaction.find_one(Transaction.transaction_id == transaction_id)
        if transaction is None:
            # Can genuinely happen: a charger reconnecting after time offline may deliver a
            # queued Stop before this Central System ever saw the matching Start.
            logging.warning(
                "STOP TX: %s has no record of transaction_id %s; recording it as incomplete",
                self.id, transaction_id,
            )
            transaction = await Transaction.record_orphan_stop(
                transaction_id=transaction_id,
                charge_point_identity=self.id,
                meter_stop=meter_stop,
                stopped_at=stopped_at,
                stopped_by_id_tag=id_tag,
                stop_reason=reason,
            )
        elif not transaction.is_open:
            # A charger that reconnects mid-session can redeliver a StopTransaction it already
            # sent. The first delivery already closed the record; this one changes nothing.
            logging.info(
                "STOP TX: duplicate delivery for transaction_id %s on %s; ignoring",
                transaction_id, self.id,
            )
        else:
            await transaction.stop(
                meter_stop=meter_stop,
                stopped_at=stopped_at,
                stopped_by_id_tag=id_tag,
                stop_reason=reason,
            )
        id_tag_info = None
        if id_tag is not None:
            # OCPP 1.6 s4.10: idTagInfo is only meaningful when the request carried an idTag.
            # s3.10's parent-group rule decides whether THIS tag has standing to stop THIS
            # transaction; an orphaned record (transaction.id_tag is None) has no starting tag
            # to compare against, so the stopping tag is judged on its own status alone.
            starting_id_tag = transaction.id_tag if transaction.id_tag is not None else id_tag
            id_tag_info = await IdTag.authorize_stop(id_tag, starting_id_tag)
        return call_result.StopTransaction(id_tag_info=id_tag_info)


def identity_from_path(path):
    """The charge point identity carried by the connection URL (OCPP-J 1.6 s3.1.1)."""
    return unquote(path.strip("/"))  # e.g. "/CP001" -> "CP001", "/RDAM%20123" -> "RDAM 123"


def parse_basic_credentials(header):
    """Split an HTTP Basic header into an identity and the raw password bytes."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        raise ValueError(f"unsupported authorization scheme: {scheme}")
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("credentials are not valid base64") from exc
    identity, separator, password = decoded.partition(b":")
    if not separator:
        raise ValueError("credentials are not identity:key")
    # Only the identity is decoded as text: the key may be raw bytes that are
    # not valid UTF-8, as the example in OCPP-J 1.6 s6.2.2 shows.
    return identity.decode("utf-8"), password


def unauthorized(connection, message):
    response = connection.respond(HTTPStatus.UNAUTHORIZED, f"{message}\n")
    response.headers["WWW-Authenticate"] = build_www_authenticate_basic(REALM)
    return response


def admin_json_response(connection, status, payload):
    response = connection.respond(status, json.dumps(payload) + "\n")
    # respond() already set a text/plain Content-Type; Headers.__setitem__ appends rather than
    # replacing, so the old value has to go first or the response would carry both.
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = "application/json"
    return response


async def handle_admin_request(connection, request):
    """Serve the operator API (see operate.py) multiplexed on this same port.

    GET only, with parameters in the query string: the websockets version in use rejects any
    other HTTP method while parsing the request line, before process_request ever runs, so a
    request body was never an option here. Every admin path requires ?token=<ADMIN_TOKEN env
    var>; with that variable unset, every admin path answers 404, as if it did not exist.
    """
    token = os.environ.get(ADMIN_TOKEN_ENV_VAR)
    if token is None:
        return admin_json_response(
            connection, HTTPStatus.NOT_FOUND, {"error": "admin API disabled"}
        )

    split = urlsplit(request.path)
    params = {key: values[-1] for key, values in parse_qs(split.query).items()}
    if params.get("token") != token:
        return admin_json_response(connection, HTTPStatus.UNAUTHORIZED, {"error": "invalid token"})

    try:
        if split.path == "/admin/remote-start":
            connector_id = int(params["connector_id"]) if "connector_id" in params else None
            response = await send_remote_start_transaction(
                params["identity"], params["id_tag"], connector_id
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/remote-stop":
            response = await send_remote_stop_transaction(
                params["identity"], int(params["transaction_id"])
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
    except KeyError as exc:
        return admin_json_response(
            connection, HTTPStatus.BAD_REQUEST, {"error": f"missing parameter {exc}"}
        )
    except PendingChargerError as exc:
        return admin_json_response(connection, HTTPStatus.CONFLICT, {"error": str(exc)})
    except RuntimeError as exc:
        return admin_json_response(connection, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
    except ValueError as exc:
        return admin_json_response(connection, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return admin_json_response(
        connection, HTTPStatus.NOT_FOUND, {"error": f"no such admin endpoint: {split.path}"}
    )


async def authorize(connection, request):
    """Authenticate the handshake with HTTP Basic auth, per OCPP-J 1.6 s6.2.2.

    The username is the charge point identity and the password is its 20-byte
    authorization key. Returning None accepts the connection; returning a
    response rejects it before any OCPP message is exchanged.

    Requests under ADMIN_PATH_PREFIX are not charger connections at all -- see
    handle_admin_request -- and are routed there before any of this applies.
    """
    if request.path.startswith(ADMIN_PATH_PREFIX):
        return await handle_admin_request(connection, request)
    identity = identity_from_path(request.path)
    header = request.headers.get("Authorization")
    if header is None:
        print(f"AUTH FAILED: no credentials offered for {identity!r}")
        return unauthorized(connection, "Missing credentials")
    try:
        username, password = parse_basic_credentials(header)
        key = authorization_key_from_basic_password(password)
    except ValueError as exc:
        print(f"AUTH FAILED: malformed credentials for {identity!r}: {exc}")
        return unauthorized(connection, "Malformed credentials")
    if username != identity:
        print(f"AUTH FAILED: credentials for {username!r} used on the URL of {identity!r}")
        return connection.respond(
            HTTPStatus.FORBIDDEN, "Credentials do not match the URL identity\n"
        )
    if await ChargePointRecord.authenticate(identity, key) is None:
        print(f"AUTH FAILED: unknown charger or wrong key for {identity!r}")
        return unauthorized(connection, "Invalid credentials")
    print(f"AUTH OK: {identity}")
    return None


async def on_connect(websocket):
    cp_id = identity_from_path(websocket.request.path)  # e.g. "CP001"
    record = await ChargePointRecord.find_one(ChargePointRecord.identity == cp_id)
    # Preload every connector this charger has reported before, so status is validated
    # against what was last known even across a Central System restart.
    known_statuses = await ConnectorStatus.all_for(cp_id)
    connector_states = {cs.connector_id: cs.to_connector_state() for cs in known_statuses}
    print("Charger connected:", cp_id)
    charge_point = MyChargePoint(
        cp_id, websocket, record=record, connector_states=connector_states
    )
    CONNECTED_CHARGE_POINTS[cp_id] = charge_point
    try:
        await charge_point.start()
    except ConnectionClosed:
        print("Charger disconnected:", cp_id)
    finally:
        # Only drop the entry if it is still this connection: a fast reconnect under the
        # same identity may already have replaced it by the time this one unwinds.
        if CONNECTED_CHARGE_POINTS.get(cp_id) is charge_point:
            del CONNECTED_CHARGE_POINTS[cp_id]


async def send_remote_start_transaction(identity, id_tag, connector_id=None):
    """Ask a connected charger to start a transaction for id_tag (OCPP 1.6 s5.11).

    A conf of Accepted means only that the charger will ATTEMPT to start -- the transaction
    itself still arrives separately as a normal StartTransaction, which is what actually
    creates the Transaction document (see on_start). Forbidden while the charger is Pending
    onboarding (s4.2); connector 0 is never valid here, since it addresses the main controller,
    never an actual charging connector (s3.8).
    """
    if connector_id == 0:
        raise ValueError("connector 0 is the main controller, not a valid connector to start on")
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "RemoteStartTransaction")
    return await charge_point.call(
        call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id), suppress=False
    )


async def send_remote_stop_transaction(identity, transaction_id):
    """Ask a connected charger to stop a transaction. Forbidden while Pending (OCPP 1.6 s4.2)."""
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "RemoteStopTransaction")
    return await charge_point.call(
        call.RemoteStopTransaction(transaction_id=transaction_id), suppress=False
    )


async def serve(host="0.0.0.0", port=9000):
    """Start the WebSocket server and return it, already listening.

    Does not touch MongoDB -- call init_db() first. Split out from main() so tests can start a
    real server in-process, on an ephemeral port (port=0), instead of assuming 9000 is free.
    """
    server = await websockets.serve(
        on_connect,
        host,
        port,
        subprotocols=[Subprotocol("ocpp1.6")],
        process_request=authorize,
    )
    bound_port = server.sockets[0].getsockname()[1]
    print(f"Listening on ws://{host}:{bound_port} (HTTP Basic auth required)")
    return server


async def main():
    try:
        await init_db()
    except PyMongoError as exc:
        print(f"Could not reach MongoDB at {mongodb_url()}: {type(exc).__name__}")
        print("Start MongoDB, or set MONGODB_URL to point elsewhere.")
        return 1
    print(f"Charge point registry ready at {mongodb_url()}")
    server = await serve()
    await server.wait_closed()
    return 0


# Deliberately guarded, unlike this project's other entry-point scripts (see README.md's
# conventions): the test suite in tests/ imports this module to call serve() directly, on an
# ephemeral port, without starting the real server. An unguarded module-level call would run
# main() -- binding port 9000 -- on every `import main`, which breaks under pytest and fails
# outright whenever port 9000 is already taken, exactly the situation these tests exist to
# tolerate. `python main.py` is unaffected: __name__ is "__main__" only when run directly.
if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
