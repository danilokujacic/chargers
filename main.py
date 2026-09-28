import asyncio
import base64
import binascii
import json
import logging
import os
import pathlib
import time
from datetime import UTC, datetime
from decimal import Decimal
from http import HTTPStatus
from urllib.parse import parse_qs, unquote, urlsplit

import redis.asyncio as redis_asyncio
import websockets
from beanie import PydanticObjectId
from bson.errors import InvalidId
from ocpp.routing import after, on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result, datatypes
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    CancelReservationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ConfigurationStatus,
    DataTransferStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    ReservationStatus,
    MessageTrigger,
    ResetType,
    UpdateStatus,
    UpdateType,
)
from pymongo.errors import PyMongoError
from websockets.exceptions import ConnectionClosed
from websockets.headers import build_www_authenticate_basic
from websockets.typing import Subprotocol

from commissioning import (
    AUTHORIZATION_KEY_CONFIG_KEY,
    PendingChargerError,
    boot_status,
    ensure_commissioned,
    onboard,
)
from charging_profiles import TX, parse_profile, purpose_connector_problem
from connector_state_machine import IllegalTransition
from models import ChargePoint as ChargePointRecord
from models import ConfigurationEntry, ConnectorStatus, DiagnosticsRequest, FaultEvent
from models import FirmwareUpdate, IdTag, InstalledChargingProfile, LocalListState
from models import Reservation, Site, SiteType
from models import Transaction, authorization_key_from_basic_password, init_db, mongodb_url
from models import next_reservation_id

# OCPP-J 1.6 s6.2.2: a charger "should not give back the authorization key in response to a
# GetConfiguration request". Even if a charger's implementation ignores that and reports one
# anyway, this Central System must not be the thing that persists it in plain sight next to
# every other configuration value -- see send_get_configuration.
DATA_TRANSFER_KNOWN_VENDOR_IDS = frozenset()  # none registered; see on_data_transfer

# OCPP 1.6 s5.16: "the Central System must host or presign a URL" for UpdateFirmware -- this
# project's answer is to host the file itself, off this same port, from this local directory.
# Placed under the project root, not in the working directory a script happens to run from, so
# it resolves the same way regardless of where `python main.py` is invoked.
FIRMWARE_PATH_PREFIX = "/firmware/"
FIRMWARE_DIR = pathlib.Path(__file__).resolve().parent / "firmware_files"

REALM = "ocpp"

# Public-API seam (08-public-api-service.md §E): the Central System publishes one JSON envelope
# per notable event to this Redis channel, and the separate FastAPI process (api/) subscribes.
# Fire-and-forget: nothing here may ever break or slow down OCPP message handling.
EVENTS_CHANNEL = "charging-events"
_event_publisher = None


def redis_url():
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


async def connect_event_publisher():
    """Create the Redis client used by publish_event. The client connects lazily, so an
    unreachable Redis does not fail here -- each publish just logs and moves on."""
    global _event_publisher
    _event_publisher = redis_asyncio.Redis.from_url(redis_url())


async def publish_event(event_type, charge_point_identity, connector_id, data=None):
    """PUBLISH one event envelope (§E.2). A no-op until connect_event_publisher() has run, so
    the test suite -- which calls serve() directly -- is unaffected. Never raises."""
    if _event_publisher is None:
        return
    try:
        envelope = {
            "type": event_type,
            "charge_point_identity": charge_point_identity,
            "connector_id": connector_id,
            "at": now(),
            "data": data or {},
        }
        await asyncio.wait_for(
            _event_publisher.publish(EVENTS_CHANNEL, json.dumps(envelope, default=str)),
            timeout=0.5,
        )
    except Exception as exc:
        logging.warning("could not publish %s event to Redis: %r", event_type, exc)

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
        if self.record is None:
            return
        if self.record.registration_status != RegistrationStatus.pending:
            await reapply_persisted_availability(self)
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
        previous_status = state.status
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
        await publish_event(
            "connector_status", self.id, connector_id,
            {"status": status.value, "error_code": error_code.value},
        )
        await self._record_fault_history(
            connector_id, previous_status, state.status, error_code, kwargs
        )
        return call_result.StatusNotification()

    async def _record_fault_history(
        self, connector_id, previous_status, new_status, error_code, kwargs
    ):
        """Open or close a FaultEvent when this StatusNotification crosses into or out of
        Faulted (OCPP 1.6 s4.9's A9/B9/.../H9 -> Faulted, and I1-I8's recovery back out of it).

        A repeated Faulted report with the same status (e.g. an escalating error_code while
        still Faulted) does not open a second event -- only the transition itself is tracked,
        not every message received while already in that state.
        """
        was_faulted = previous_status == ChargePointStatus.faulted
        is_faulted = new_status == ChargePointStatus.faulted
        if is_faulted and not was_faulted:
            await FaultEvent.open_new(
                self.id, connector_id, error_code,
                info=kwargs.get("info"),
                vendor_id=kwargs.get("vendor_id"),
                vendor_error_code=kwargs.get("vendor_error_code"),
            )
            logging.warning(
                "FAULT: %s connector %s entered Faulted (%s)",
                self.id, connector_id, error_code.value,
            )
            await publish_event(
                "fault_opened", self.id, connector_id,
                {"error_code": error_code.value, "info": kwargs.get("info")},
            )
        elif was_faulted and not is_faulted:
            await FaultEvent.close_open(self.id, connector_id)
            print(f"FAULT: {self.id} connector {connector_id} recovered to {new_status.value}")
            await publish_event("fault_cleared", self.id, connector_id)

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
        await publish_event(
            "transaction_started", self.id, connector_id,
            {
                "transaction_id": transaction.transaction_id,
                "id_tag": id_tag,
                "meter_start": meter_start,
            },
        )
        await record_remote_start_profile(
            self.id, connector_id, id_tag, transaction.transaction_id
        )
        # OCPP 1.6 s3.11: a reservation ends when "the reserved idTag is used on the reserved
        # connector, or on any connector when connectorId was 0" -- covers both a charger that
        # correctly echoes the optional reservationId field and one that does not.
        consumed = await reservations_matching_start(
            self.id, connector_id, id_tag, kwargs.get("reservation_id")
        )
        await Reservation.release_for_start(
            self.id, connector_id, id_tag, kwargs.get("reservation_id")
        )
        await publish_reservations_released(consumed, "consumed")
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
                await publish_event(
                    "meter_value", self.id, connector_id,
                    {
                        "transaction_id": transaction_id,
                        "meter_value": meter_value[-1] if meter_value else None,
                    },
                )
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
            await publish_event(
                "transaction_stopped", self.id, transaction.connector_id,
                {"transaction_id": transaction_id, "meter_stop": meter_stop},
            )
            await InstalledChargingProfile.drop_transaction_profiles(
                self.id, connector_id=transaction.connector_id, transaction_id=transaction_id
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
            await publish_event(
                "transaction_stopped", self.id, transaction.connector_id,
                {"transaction_id": transaction_id, "meter_stop": meter_stop},
            )
            await InstalledChargingProfile.drop_transaction_profiles(
                self.id, connector_id=transaction.connector_id, transaction_id=transaction_id
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

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, **kwargs):
        """Answer a charger-initiated DataTransfer.req (OCPP 1.6 s4.15): the vendor-specific
        escape hatch outside the standard message set.

        This project registers no vendor, so every vendor_id is unknown -- logged and answered
        UnknownVendorId, never silently dropped, so an operator can see a charger tried to use
        it. A real vendor extension would check vendor_id against DATA_TRANSFER_KNOWN_VENDOR_IDS
        and dispatch from there instead of this one blanket answer.
        """
        message_id = kwargs.get("message_id")
        logging.info(
            "DATA TRANSFER: %s sent vendor_id=%s message_id=%s (no vendor registered)",
            self.id, vendor_id, message_id,
        )
        if vendor_id in DATA_TRANSFER_KNOWN_VENDOR_IDS:
            raise NotImplementedError(f"vendor {vendor_id} is registered but has no handler")
        return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)

    @on(Action.firmware_status_notification)
    async def on_firmware_status(self, status, **kwargs):
        """Progress on an UpdateFirmware request (OCPP 1.6 s4.13). Matched to this charger's
        most recently requested update -- the message carries no correlation id of its own.
        """
        status = FirmwareStatus(status)
        print(f"FIRMWARE STATUS: {self.id} -> {status.value}")
        await FirmwareUpdate.record_status(self.id, status)
        return call_result.FirmwareStatusNotification()

    @on(Action.diagnostics_status_notification)
    async def on_diagnostics_status(self, status, **kwargs):
        """Progress on a GetDiagnostics request (OCPP 1.6 s4.9). Matched the same way as
        firmware status: this charger's most recently requested diagnostics upload.
        """
        status = DiagnosticsStatus(status)
        print(f"DIAGNOSTICS STATUS: {self.id} -> {status.value}")
        await DiagnosticsRequest.record_status(self.id, status)
        return call_result.DiagnosticsStatusNotification()


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


def _json_default(value):
    """The ocpp library parses every JSON number it receives as a Decimal, so a value read off a
    charger's answer (a composite schedule's limits) reaches the admin API as one."""
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def admin_json_response(connection, status, payload):
    response = connection.respond(status, json.dumps(payload, default=_json_default) + "\n")
    # respond() already set a text/plain Content-Type; Headers.__setitem__ appends rather than
    # replacing, so the old value has to go first or the response would carry both.
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = "application/json"
    return response


async def handle_firmware_request(connection, request):
    """Serve a firmware image a charger was told to fetch via UpdateFirmware.req (OCPP 1.6
    s5.16) -- this project's answer to "the Central System must host or presign a URL". Files
    are served straight off this same port from FIRMWARE_DIR, no auth, since a charger fetching
    firmware has no OCPP-level credentials to present to a plain HTTP GET (this is a POC; a real
    deployment would put this behind TLS and a signed, expiring URL instead).

    Files are read and served as UTF-8 text (a version string or changelog stands in for a real
    firmware image here), not arbitrary binary: the websockets version in use only exposes a
    str-based response body (see ServerConnection.respond), and building a raw binary HTTP
    response is more machinery than a POC's stand-in firmware file needs.
    """
    split = urlsplit(request.path)
    filename = unquote(split.path.removeprefix(FIRMWARE_PATH_PREFIX))
    if not filename or "/" in filename or filename in (".", ".."):
        return connection.respond(HTTPStatus.BAD_REQUEST, "Invalid firmware filename\n")
    try:
        content = (FIRMWARE_DIR / filename).read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return connection.respond(HTTPStatus.NOT_FOUND, "No such firmware file\n")
    response = connection.respond(HTTPStatus.OK, content)
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = "application/octet-stream"
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
            profile = json.loads(params["profile"]) if "profile" in params else None
            response = await send_remote_start_transaction(
                params["identity"], params["id_tag"], connector_id, profile
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/remote-stop":
            response = await send_remote_stop_transaction(
                params["identity"], int(params["transaction_id"])
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/change-availability":
            response = await send_change_availability(
                params["identity"],
                int(params["connector_id"]),
                AvailabilityType(params["type"]),
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/reset":
            response = await send_reset(params["identity"], ResetType(params["type"]))
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/set-charging-profile":
            response = await send_set_charging_profile(
                params["identity"], int(params["connector_id"]), json.loads(params["profile"])
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/clear-charging-profile":
            response = await send_clear_charging_profile(
                params["identity"],
                profile_id=int(params["id"]) if "id" in params else None,
                connector_id=int(params["connector_id"]) if "connector_id" in params else None,
                purpose=params.get("purpose"),
                stack_level=int(params["stack_level"]) if "stack_level" in params else None,
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/get-composite-schedule":
            response = await send_get_composite_schedule(
                params["identity"],
                int(params["connector_id"]),
                int(params["duration"]),
                params.get("unit"),
            )
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {
                    "status": response.status,
                    "connector_id": response.connector_id,
                    "schedule_start": response.schedule_start,
                    "charging_schedule": response.charging_schedule,
                },
            )
        if split.path == "/admin/charging-profiles":
            connector_id = int(params["connector_id"]) if "connector_id" in params else None
            records = await get_installed_charging_profiles(params["identity"], connector_id)
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {
                    "charging_profiles": [
                        {
                            "connector_id": record.connector_id,
                            "charging_profile_id": record.charging_profile_id,
                            "stack_level": record.stack_level,
                            "purpose": record.purpose,
                            "transaction_id": record.transaction_id,
                            "installed_at": record.installed_at.isoformat(),
                            "profile": record.profile,
                        }
                        for record in records
                    ]
                },
            )
        if split.path == "/admin/trigger-message":
            connector_id = int(params["connector_id"]) if "connector_id" in params else None
            response = await send_trigger_message(
                params["identity"], MessageTrigger(params["message"]), connector_id
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/unlock-connector":
            response = await send_unlock_connector(
                params["identity"], int(params["connector_id"])
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/get-configuration":
            keys = params["keys"].split(",") if "keys" in params else None
            response = await send_get_configuration(params["identity"], keys)
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {
                    "configuration_key": response.configuration_key or [],
                    "unknown_key": response.unknown_key or [],
                },
            )
        if split.path == "/admin/change-configuration":
            response = await send_change_configuration(
                params["identity"], params["key"], params["value"]
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/clear-cache":
            response = await send_clear_cache(params["identity"])
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/fault-history":
            connector_id = int(params["connector_id"]) if "connector_id" in params else None
            events = await get_fault_history(params["identity"], connector_id)
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {
                    "events": [
                        {
                            "connector_id": event.connector_id,
                            "error_code": event.error_code,
                            "info": event.info,
                            "vendor_id": event.vendor_id,
                            "vendor_error_code": event.vendor_error_code,
                            "entered_at": event.entered_at.isoformat(),
                            "cleared_at": (
                                event.cleared_at.isoformat() if event.cleared_at else None
                            ),
                        }
                        for event in events
                    ]
                },
            )
        if split.path == "/admin/update-firmware":
            location = await send_update_firmware(
                params["identity"],
                params["filename"],
                params["retrieve_date"],
                retries=int(params["retries"]) if "retries" in params else None,
                retry_interval=(
                    int(params["retry_interval"]) if "retry_interval" in params else None
                ),
            )
            return admin_json_response(connection, HTTPStatus.OK, {"location": location})
        if split.path == "/admin/firmware-status":
            record = await get_firmware_status(params["identity"])
            return admin_json_response(connection, HTTPStatus.OK, _firmware_status_payload(record))
        if split.path == "/admin/get-diagnostics":
            response = await send_get_diagnostics(
                params["identity"],
                retries=int(params["retries"]) if "retries" in params else None,
                retry_interval=(
                    int(params["retry_interval"]) if "retry_interval" in params else None
                ),
                start_time=params.get("start_time"),
                stop_time=params.get("stop_time"),
            )
            return admin_json_response(
                connection, HTTPStatus.OK, {"file_name": response.file_name}
            )
        if split.path == "/admin/diagnostics-status":
            record = await get_diagnostics_status(params["identity"])
            return admin_json_response(
                connection, HTTPStatus.OK, _diagnostics_status_payload(record)
            )
        if split.path == "/admin/get-local-list-version":
            response = await send_get_local_list_version(params["identity"])
            return admin_json_response(
                connection, HTTPStatus.OK, {"list_version": response.list_version}
            )
        if split.path == "/admin/send-local-list":
            id_tags = params["id_tags"].split(",") if "id_tags" in params else None
            remove_id_tags = (
                params["remove_id_tags"].split(",") if "remove_id_tags" in params else None
            )
            update_type = UpdateType(params.get("update_type", UpdateType.full))
            new_version, response = await send_send_local_list(
                params["identity"], id_tags=id_tags, remove_id_tags=remove_id_tags,
                update_type=update_type,
            )
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {"status": response.status, "list_version": new_version},
            )
        if split.path == "/admin/reserve-now":
            reservation_id, response = await send_reserve_now(
                params["identity"],
                int(params["connector_id"]),
                params["id_tag"],
                params["expiry_date"],
                parent_id_tag=params.get("parent_id_tag"),
            )
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {"status": response.status, "reservation_id": reservation_id},
            )
        if split.path == "/admin/cancel-reservation":
            response = await send_cancel_reservation(
                params["identity"], int(params["reservation_id"])
            )
            return admin_json_response(connection, HTTPStatus.OK, {"status": response.status})
        if split.path == "/admin/create-site":
            site = await create_site(
                params["name"],
                SiteType(params["site_type"]),
                float(params["latitude"]),
                float(params["longitude"]),
                address=params.get("address"),
            )
            return admin_json_response(
                connection,
                HTTPStatus.CREATED,
                {
                    "id": str(site.id),
                    "name": site.name,
                    "site_type": site.site_type,
                    "latitude": site.latitude,
                    "longitude": site.longitude,
                },
            )
        if split.path == "/admin/set-charge-point-location":
            record = await set_charge_point_location(
                params["identity"],
                site_id=params.get("site_id"),
                latitude=float(params["latitude"]) if "latitude" in params else None,
                longitude=float(params["longitude"]) if "longitude" in params else None,
            )
            return admin_json_response(
                connection,
                HTTPStatus.OK,
                {
                    "identity": record.identity,
                    "site_id": str(record.site_id) if record.site_id else None,
                    "latitude": record.latitude,
                    "longitude": record.longitude,
                },
            )
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

    Requests under ADMIN_PATH_PREFIX or FIRMWARE_PATH_PREFIX are not charger connections at all
    -- see handle_admin_request / handle_firmware_request -- and are routed there before any of
    this applies.
    """
    if request.path.startswith(ADMIN_PATH_PREFIX):
        return await handle_admin_request(connection, request)
    if request.path.startswith(FIRMWARE_PATH_PREFIX):
        return await handle_firmware_request(connection, request)
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


# A charging profile sent with RemoteStartTransaction belongs to a transaction that does not exist
# yet: the charger only gets a transaction_id from the StartTransaction that follows. Held here,
# in memory, from an Accepted conf until that StartTransaction arrives, so it can be recorded
# against the right transaction (record_remote_start_profile). Keyed by charger, connector (None
# when the operator let the charger choose) and idTag; entries lapse after the TTL so a start that
# never happens (an idTag the charger then refuses) cannot attach itself to some later, unrelated
# transaction. Lost on a Central System restart, which only costs the bookkeeping for a session
# already in the middle of starting -- the charger still has the profile.
REMOTE_START_PROFILE_TTL_SECONDS = 300
_pending_remote_start_profiles = {}


async def record_remote_start_profile(identity, connector_id, id_tag, transaction_id):
    """Record the TxProfile that came with a RemoteStartTransaction, now that its transaction
    exists. A no-op for any transaction that was not started that way."""
    for key in ((identity, connector_id, id_tag), (identity, None, id_tag)):
        pending = _pending_remote_start_profiles.pop(key, None)
        if pending is not None:
            profile, deadline = pending
            if time.monotonic() <= deadline:
                await InstalledChargingProfile.install(
                    identity, connector_id,
                    profile.model_copy(update={"transaction_id": transaction_id}),
                )
            return


async def send_remote_start_transaction(identity, id_tag, connector_id=None, charging_profile=None):
    """Ask a connected charger to start a transaction for id_tag (OCPP 1.6 s5.11).

    `charging_profile` optionally limits the new transaction from its first moment (s5.16.2): it
    must be a TxProfile and must not carry a transactionId, since none exists yet. Recorded in
    InstalledChargingProfile once the transaction actually starts.

    A conf of Accepted means only that the charger will ATTEMPT to start -- the transaction
    itself still arrives separately as a normal StartTransaction, which is what actually
    creates the Transaction document (see on_start). Forbidden while the charger is Pending
    onboarding (s4.2); connector 0 is never valid here, since it addresses the main controller,
    never an actual charging connector (s3.8).
    """
    if connector_id == 0:
        raise ValueError("connector 0 is the main controller, not a valid connector to start on")
    profile = None
    if charging_profile is not None:
        profile = parse_profile(charging_profile)
        if profile.purpose != TX:
            raise ValueError(
                "a charging profile sent with RemoteStartTransaction must be a TxProfile"
            )
        if profile.transaction_id is not None:
            raise ValueError(
                "a RemoteStartTransaction charging profile must not set transactionId"
            )
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "RemoteStartTransaction")
    if charging_profile is None:
        return await charge_point.call(
            call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id), suppress=False
        )
    response = await charge_point.call(
        call.RemoteStartTransaction(
            id_tag=id_tag, connector_id=connector_id, charging_profile=profile.to_wire()
        ),
        suppress=False,
    )
    if response.status == RemoteStartStopStatus.accepted:
        _pending_remote_start_profiles[(identity, connector_id, id_tag)] = (
            profile, time.monotonic() + REMOTE_START_PROFILE_TTL_SECONDS
        )
    return response


async def send_remote_stop_transaction(identity, transaction_id):
    """Ask a connected charger to stop a transaction. Forbidden while Pending (OCPP 1.6 s4.2)."""
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "RemoteStopTransaction")
    return await charge_point.call(
        call.RemoteStopTransaction(transaction_id=transaction_id), suppress=False
    )


async def send_change_availability(identity, connector_id, availability_type):
    """Ask a connected charger to make a connector (or, with connector_id=0, itself and every
    connector) Operative or Inoperative (OCPP 1.6 s5.2).

    The conf status only says what will happen: Accepted takes effect right away, Scheduled
    means the charger is deferring it until a transaction in progress finishes, Rejected means
    nothing changes. Only the first two are persisted as this charger's desired_availability --
    a Rejected request must not overwrite what is still actually in effect. Forbidden while
    Pending, like the other Central-System-initiated messages (s4.2); ChangeAvailability is not
    the configuration exchange that stays open during onboarding.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "ChangeAvailability")
    response = await charge_point.call(
        call.ChangeAvailability(connector_id=connector_id, type=availability_type),
        suppress=False,
    )
    if response.status != AvailabilityStatus.rejected:
        targets = [connector_id]
        if connector_id == 0:
            # OCPP 1.6 s5.2: connectorId 0 "means the Charge Point and all Connectors".
            known = await ConnectorStatus.all_for(identity)
            targets = sorted({0, *(record.connector_id for record in known)})
        scheduled = response.status == AvailabilityStatus.scheduled
        for target in targets:
            record = await ConnectorStatus.get_or_create(identity, target)
            await record.set_desired_availability(availability_type, scheduled=scheduled)
    return response


async def send_reset(identity, reset_type):
    """Ask a connected charger to reset itself: Soft finishes gracefully, Hard power-cycles
    (OCPP 1.6 s5.14).

    Accepted only means the charger will attempt it -- the reboot and the fresh
    BootNotification that follows happen on the charger's own schedule, not synchronously with
    this call. Either type re-runs the whole commissioning flow from BootNotification once the
    charger reconnects (s4.2.1); on_boot/after_boot already do that unconditionally for every
    boot, so nothing extra is needed here for it. A transaction open at reset time is not force-
    closed here: expect its StopTransaction after the reboot (possibly late, possibly never, if
    the charger truly lost power before it could send one) rather than assuming it happened.
    Forbidden while Pending, like the other Central-System-initiated messages (s4.2).
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "Reset")
    return await charge_point.call(call.Reset(type=reset_type), suppress=False)


async def send_set_charging_profile(identity, connector_id, profile):
    """Install a charging profile on a connected charger (OCPP 1.6 s5.16): a limit on the power
    or current it may deliver, over time. `profile` is a ChargingProfile as a dict, camelCase as
    in the spec or snake_case (charging_profiles.parse_profile).

    Refused before anything is sent when the profile is malformed, or breaks s3.13.1's rules for
    where each purpose may go (ChargePointMaxProfile only on connector 0, TxProfile only above
    it). A TxProfile also needs a transaction actually open on that connector -- the charger
    would discard it otherwise -- and, per s5.16 ("the Central System SHALL include the
    transactionId ... if the profile applies to a specific transaction"), this fills in the
    transactionId when the caller left it out, and refuses one that names a different
    transaction.

    Recorded in InstalledChargingProfile only when the charger answers Accepted; Rejected and
    NotSupported are returned as-is and leave no record. Forbidden while Pending, like the other
    Central-System-initiated messages here.
    """
    profile = parse_profile(profile)
    problem = purpose_connector_problem(profile, connector_id)
    if problem:
        raise ValueError(problem)
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "SetChargingProfile")
    if profile.purpose == TX:
        transaction = await Transaction.find_open(identity, connector_id)
        if transaction is None:
            raise ValueError(
                f"{identity} has no open transaction on connector {connector_id}: "
                f"a TxProfile applies to one (s3.13.1)"
            )
        if profile.transaction_id is None:
            profile = profile.model_copy(update={"transaction_id": transaction.transaction_id})
        elif profile.transaction_id != transaction.transaction_id:
            raise ValueError(
                f"transactionId {profile.transaction_id} is not the open transaction on "
                f"connector {connector_id} ({transaction.transaction_id})"
            )
    response = await charge_point.call(
        call.SetChargingProfile(connector_id=connector_id, cs_charging_profiles=profile.to_wire()),
        suppress=False,
    )
    if response.status == ChargingProfileStatus.accepted:
        await InstalledChargingProfile.install(identity, connector_id, profile)
    return response


async def send_clear_charging_profile(
    identity, profile_id=None, connector_id=None, purpose=None, stack_level=None
):
    """Remove charging profiles from a connected charger (OCPP 1.6 s5.5): the one with this
    `profile_id`, or every one matching the other criteria given -- all of them, read together --
    or, with no criteria at all, every profile on the charger.

    The Central System's own records are brought into line with the charger's answer either way:
    Accepted means they were removed there, and Unknown ("No Charging Profile(s) were found
    matching the request") means the charger never held what this side thought it did, so a
    record matching the same criteria is stale and goes too. Forbidden while Pending.
    """
    if purpose is not None:
        purpose = ChargingProfilePurposeType(purpose)
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "ClearChargingProfile")
    response = await charge_point.call(
        call.ClearChargingProfile(
            id=profile_id, connector_id=connector_id, charging_profile_purpose=purpose,
            stack_level=stack_level,
        ),
        suppress=False,
    )
    await InstalledChargingProfile.clear(identity, profile_id, connector_id, purpose, stack_level)
    return response


async def send_get_composite_schedule(identity, connector_id, duration, charging_rate_unit=None):
    """Ask a connected charger what it will actually do over the next `duration` seconds
    (OCPP 1.6 s5.7): every installed profile, and its own local limits, merged into one schedule.
    connector_id 0 asks for the whole charger's expected draw from the grid.

    The charger works this out, not this Central System, because it alone knows its local limits
    -- and the answer is only "indicative for that point in time" (s5.7). Returned as-is:
    Rejected (e.g. an unknown connector) carries no schedule. Forbidden while Pending.
    """
    if duration <= 0:
        raise ValueError("duration must be a positive number of seconds")
    if charging_rate_unit is not None:
        charging_rate_unit = ChargingRateUnitType(charging_rate_unit)
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "GetCompositeSchedule")
    return await charge_point.call(
        call.GetCompositeSchedule(
            connector_id=connector_id, duration=duration, charging_rate_unit=charging_rate_unit
        ),
        suppress=False,
    )


async def get_installed_charging_profiles(identity, connector_id=None):
    """The charging profiles this Central System has installed on a charger, from the database
    (no charger involvement: works while it is offline). Ordered by connector, purpose, level."""
    return await InstalledChargingProfile.for_charge_point(identity, connector_id)


CONNECTOR_TRIGGERS = frozenset({MessageTrigger.meter_values, MessageTrigger.status_notification})


async def send_trigger_message(identity, requested_message, connector_id=None):
    """Ask a connected charger to send one of its own messages right now (OCPP 1.6 s5.16):
    a status refresh for a charger whose state is in doubt, or -- s4.2 -- the way to make a
    Pending charger announce itself again.

    Accepted only means the charger will send it: the message itself arrives afterwards, through
    the usual handler (on_status, on_meter_values, on_boot, ...), exactly like any other. Rejected
    and NotImplemented are the charger's own answers, returned as-is.

    Deliberately NOT forbidden while Pending, unlike the other send_* functions here: s4.2 only
    bars RemoteStart/RemoteStopTransaction while Pending, and this is the sanctioned way to get
    anything out of a charger in that state. connector_id is only meaningful for MeterValues and
    StatusNotification (omit it for all connectors); passing it for any other message is a
    caller error and is refused before anything is sent.
    """
    requested_message = MessageTrigger(requested_message)
    if connector_id is not None and requested_message not in CONNECTOR_TRIGGERS:
        raise ValueError("connector_id only applies to MeterValues and StatusNotification")
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    return await charge_point.call(
        call.TriggerMessage(requested_message=requested_message, connector_id=connector_id),
        suppress=False,
    )


async def send_unlock_connector(identity, connector_id):
    """Ask a connected charger to physically unlock a connector -- for a cable stuck in a
    socket (OCPP 1.6 s5.17).

    Never valid for connector 0: it addresses the main controller, not an actual socket (s3.8).
    Does not stop a transaction by itself. Forbidden while Pending.
    """
    if connector_id == 0:
        raise ValueError("connector 0 is the main controller, not a valid connector to unlock")
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "UnlockConnector")
    return await charge_point.call(
        call.UnlockConnector(connector_id=connector_id), suppress=False
    )


async def send_get_configuration(identity, keys=None):
    """Ask a connected charger to report its configuration (OCPP 1.6 s5.9), and persist what it
    reports so an operator can see what this specific unit supports without asking it live
    again. Omitting `keys` asks for every key the charger has.

    Allowed while Pending (s4.2 exempts "configuration reads and writes"), unlike every other
    Central-System-initiated message in this project.

    Never persists a value for "AuthorizationKey": OCPP-J 1.6 s6.2.2 says a charger "should not
    give back the authorization key" here, and even a charger that ignores that must not turn
    this Central System into the place that then displays it next to every other config value.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    response = await charge_point.call(call.GetConfiguration(key=keys), suppress=False)
    for entry in response.configuration_key or []:
        key = entry["key"]
        value = None if key == AUTHORIZATION_KEY_CONFIG_KEY else entry.get("value")
        await ConfigurationEntry.upsert(
            identity, key, value, readonly=entry.get("readonly", False)
        )
    if response.unknown_key:
        logging.info(
            "GET CONFIGURATION: %s does not recognize %s", identity, response.unknown_key
        )
    return response


async def send_change_configuration(identity, key, value):
    """Ask a connected charger to change one configuration key (OCPP 1.6 s5.6).

    Generalises what commissioning.onboard() already does for "AuthorizationKey" alone (see
    AUTHORIZATION_KEY_CONFIG_KEY there) to any key an operator wants to set. Persisted only on
    Accepted or RebootRequired -- a Rejected or NotSupported answer means the charger's actual
    value never changed, so recording the requested one would lie about current state. Allowed
    while Pending, like GetConfiguration (s4.2).
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    response = await charge_point.call(
        call.ChangeConfiguration(key=key, value=value), suppress=False
    )
    if response.status in (ConfigurationStatus.accepted, ConfigurationStatus.reboot_required):
        persisted_value = None if key == AUTHORIZATION_KEY_CONFIG_KEY else value
        await ConfigurationEntry.upsert(identity, key, persisted_value, readonly=False)
    return response


async def send_clear_cache(identity):
    """Ask a connected charger to empty its Authorization Cache (OCPP 1.6 s5.4).

    The cache itself lives on the charger, not here, so there is nothing for this Central
    System to update on Accepted -- only the request is sent. Forbidden while Pending: unlike
    GetConfiguration/ChangeConfiguration, this is not "reading or writing configuration", so it
    follows the same default as ChangeAvailability/Reset/UnlockConnector.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "ClearCache")
    return await charge_point.call(call.ClearCache(), suppress=False)


async def get_fault_history(identity, connector_id=None):
    """This charger's recorded faults (OCPP 1.6 s4.9), newest first -- all connectors, or one.

    A pure read of this Central System's own records, not a message to the charger: works
    whether or not it is currently connected, and regardless of Pending/Accepted/Rejected
    status, since a fault that already happened is history either way.
    """
    return await FaultEvent.history_for(identity, connector_id)


def firmware_base_url():
    """The base URL this Central System tells a charger to fetch firmware images from."""
    return os.environ.get("FIRMWARE_BASE_URL", "http://localhost:9000")


async def send_update_firmware(
    identity, filename, retrieve_date, retries=None, retry_interval=None
):
    """Ask a connected charger to fetch and install a firmware image (OCPP 1.6 s5.16).

    filename must already exist in FIRMWARE_DIR -- this Central System hosts it itself (see
    handle_firmware_request); location is built from that directly, so an operator never
    constructs the URL by hand. UpdateFirmware.conf carries no accept/reject status (the
    charger always attempts it), so a FirmwareUpdate record is created unconditionally once the
    request is sent; its progress fills in later from FirmwareStatusNotification. Forbidden
    while Pending, like every other Central-System-initiated message that is not
    GetConfiguration/ChangeConfiguration.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "UpdateFirmware")
    if not (FIRMWARE_DIR / filename).is_file():
        raise ValueError(f"{filename!r} not found in {FIRMWARE_DIR} -- place it there first")
    location = f"{firmware_base_url()}{FIRMWARE_PATH_PREFIX}{filename}"
    await charge_point.call(
        call.UpdateFirmware(
            location=location, retrieve_date=retrieve_date, retries=retries,
            retry_interval=retry_interval,
        ),
        suppress=False,
    )
    await FirmwareUpdate.create(
        identity, location, parse_ocpp_timestamp(retrieve_date), retries, retry_interval
    )
    return location


async def get_firmware_status(identity):
    """This charger's most recently requested firmware update and its progress, or None.

    A pure read of this Central System's own records -- works with no live connection.
    """
    return await FirmwareUpdate.latest_for(identity)


def _firmware_status_payload(record):
    if record is None:
        return {"status": None}
    return {
        "status": record.status,
        "location": record.location,
        "history": [
            {"status": entry["status"], "at": entry["at"].isoformat()}
            for entry in record.history
        ],
    }


def diagnostics_upload_base_url():
    """The base URL this Central System tells a charger to upload its diagnostics archive to.

    No receiver actually listens here: the websockets version in use only parses HTTP/1.1 GET
    on this port (see handle_admin_request's own note), so an upload could not land regardless.
    Real deployments typically point this at FTP or a presigned object-store URL instead; this
    project just records the destination it told the charger, for the same audit purpose
    GetConfiguration's stored values serve.
    """
    return os.environ.get("DIAGNOSTICS_UPLOAD_URL", "http://localhost:9000/diagnostics")


async def send_get_diagnostics(
    identity, retries=None, retry_interval=None, start_time=None, stop_time=None
):
    """Ask a connected charger to upload a diagnostics log archive (OCPP 1.6 s5.1).

    A DiagnosticsRequest record is created only once the charger's conf actually names a
    file_name: an absent one means it declined, so there is nothing meaningful to track.
    Forbidden while Pending.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "GetDiagnostics")
    location = f"{diagnostics_upload_base_url()}/{identity}"
    response = await charge_point.call(
        call.GetDiagnostics(
            location=location, retries=retries, retry_interval=retry_interval,
            start_time=start_time, stop_time=stop_time,
        ),
        suppress=False,
    )
    if response.file_name is not None:
        await DiagnosticsRequest.create(identity, location, response.file_name)
    return response


async def get_diagnostics_status(identity):
    """This charger's most recently requested diagnostics upload and its progress, or None.

    A pure read of this Central System's own records -- works with no live connection.
    """
    return await DiagnosticsRequest.latest_for(identity)


def _diagnostics_status_payload(record):
    if record is None:
        return {"status": None}
    return {
        "status": record.status,
        "file_name": record.file_name,
        "history": [
            {"status": entry["status"], "at": entry["at"].isoformat()}
            for entry in record.history
        ],
    }


async def send_get_local_list_version(identity):
    """Ask a connected charger what Local Authorization List version it currently holds (OCPP
    1.6 s5.7), so an operator can tell whether a SendLocalList is even needed. Forbidden while
    Pending: like ClearCache, this is not "reading or writing configuration" in the s4.2 sense
    that GetConfiguration/ChangeConfiguration are exempted for.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "GetLocalListVersion")
    return await charge_point.call(call.GetLocalListVersion(), suppress=False)


def _authorization_data_for(id_tag_document):
    """The AuthorizationData entry SendLocalList carries for one IdTag document (OCPP 1.6 s3.6):
    IdTag is this project's one source of truth for a driver credential's status everywhere
    else (Authorize, StartTransaction, ...), so a pushed list entry is built from it directly
    rather than kept as a separate copy.
    """
    return datatypes.AuthorizationData(
        id_tag=id_tag_document.id_tag,
        id_tag_info=datatypes.IdTagInfo(
            status=id_tag_document.status,
            parent_id_tag=id_tag_document.parent_id_tag,
            expiry_date=(
                id_tag_document.expiry_date.isoformat() if id_tag_document.expiry_date else None
            ),
        ),
    )


async def send_send_local_list(
    identity, id_tags=None, remove_id_tags=None, update_type=UpdateType.full
):
    """Push a Local Authorization List update to a connected charger (OCPP 1.6 s5.8, s3.6).

    id_tags names which IdTag documents to include, looked up fresh so the charger gets each
    one's current status; omitting it on a Full update means "every idTag on record". Full
    "replaces the entire Local Authorization List" (s3.6), so remove_id_tags is only meaningful
    on a Differential update -- an entry with no idTagInfo there means "delete this one".

    list_version is allocated here, one higher than this Central System's own last recorded
    push, and persisted only when the charger answers Accepted; VersionMismatch/Failed/
    NotSupported mean the charger's list never changed, so recording a new version would lie
    about what it actually holds. Forbidden while Pending, like ClearCache.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "SendLocalList")
    state = await LocalListState.get_or_create(identity)
    new_version = state.list_version + 1
    entries = []
    added = []
    if update_type == UpdateType.full:
        if id_tags is not None:
            names = id_tags
        else:
            names = [doc.id_tag for doc in await IdTag.find().to_list()]
        for name in names:
            doc = await IdTag.find_one(IdTag.id_tag == name)
            if doc is not None:
                entries.append(_authorization_data_for(doc))
                added.append(name)
    else:
        for name in id_tags or []:
            doc = await IdTag.find_one(IdTag.id_tag == name)
            if doc is not None:
                entries.append(_authorization_data_for(doc))
                added.append(name)
        for name in remove_id_tags or []:
            entries.append(datatypes.AuthorizationData(id_tag=name))
    response = await charge_point.call(
        call.SendLocalList(
            list_version=new_version, update_type=update_type, local_authorization_list=entries
        ),
        suppress=False,
    )
    if response.status == UpdateStatus.accepted:
        if update_type == UpdateType.full:
            await state.record_full_push(new_version, added)
        else:
            await state.record_differential_push(new_version, added, remove_id_tags or [])
    return new_version, response


async def send_reserve_now(identity, connector_id, id_tag, expiry_date, parent_id_tag=None):
    """Ask a connected charger to hold a connector -- or, with connector_id=0, itself, honouring
    the reservation on whichever connector the idTag first shows up on -- for one idTag until it
    is used, cancelled, or expiry_date passes (OCPP 1.6 s5.15, s3.11).

    reservation_id is generated here, not by the caller: the Central System is the party that
    must guarantee it is unique, the same reason transaction_id is allocated by next_transaction_id
    rather than trusted from outside. Persisted only on Accepted; Faulted/Occupied/Rejected/
    Unavailable mean nothing changed on the charger. Forbidden while Pending.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "ReserveNow")
    reservation_id = await next_reservation_id()
    response = await charge_point.call(
        call.ReserveNow(
            connector_id=connector_id,
            expiry_date=expiry_date,
            id_tag=id_tag,
            reservation_id=reservation_id,
            parent_id_tag=parent_id_tag,
        ),
        suppress=False,
    )
    if response.status == ReservationStatus.accepted:
        await Reservation.create(
            reservation_id, identity, connector_id, id_tag, parse_ocpp_timestamp(expiry_date),
            parent_id_tag=parent_id_tag,
        )
        await publish_event(
            "reservation_created", identity, connector_id,
            {
                "reservation_id": reservation_id,
                "id_tag": id_tag,
                "expiry_date": str(expiry_date),
            },
        )
    return reservation_id, response


async def send_cancel_reservation(identity, reservation_id):
    """Ask a connected charger to give up a reservation before it would otherwise end (OCPP 1.6
    s5.3). Only marks this Central System's own record released on Accepted: the charger is the
    authority on whether it actually knew about reservation_id. Forbidden while Pending.
    """
    charge_point = CONNECTED_CHARGE_POINTS.get(identity)
    if charge_point is None:
        raise RuntimeError(f"{identity} is not currently connected")
    ensure_commissioned(charge_point.record, "CancelReservation")
    response = await charge_point.call(
        call.CancelReservation(reservation_id=reservation_id), suppress=False
    )
    if response.status == CancelReservationStatus.accepted:
        active = await Reservation.find_active(identity, reservation_id)
        if await Reservation.release_by_cancel(reservation_id) and active is not None:
            await publish_reservations_released([active], "cancelled")
    return response


async def create_site(name, site_type, latitude, longitude, address=None):
    """Create a new operator-managed Site (07-public-map-platform.md §A) -- a physical place a
    ChargePoint can later be assigned to via set_charge_point_location.

    Unlike every send_* function above, this touches no charger and needs no connection: a
    Site's existence and its coordinates are pure map metadata, not something OCPP reports or
    commands. There is nothing to be "Pending" about.
    """
    return await Site.create(name, site_type, latitude, longitude, address=address)


async def set_charge_point_location(identity, site_id=None, latitude=None, longitude=None):
    """Assign a registered charger to a Site, or give it its own standalone coordinates
    (07-public-map-platform.md §A's three-way map-location fallback; §B's "one-line operator
    command, not a manual MongoDB shell edit").

    Also touches no charger: a physical location does not depend on whether the charger happens
    to be connected right now, so -- unlike every send_* function above -- this works for a
    charger that has never come online at all.
    """
    record = await ChargePointRecord.find_one(ChargePointRecord.identity == identity)
    if record is None:
        raise ValueError(f"{identity} is not registered")
    resolved_site_id = None
    if site_id is not None:
        try:
            resolved_site_id = PydanticObjectId(site_id)
        except InvalidId as exc:
            raise ValueError(f"{site_id!r} is not a valid site id") from exc
        if await Site.get(resolved_site_id) is None:
            raise ValueError(f"no such site: {site_id}")
    await record.set_location(site_id=resolved_site_id, latitude=latitude, longitude=longitude)
    return record


async def reservations_matching_start(identity, connector_id, id_tag, reservation_id=None):
    """The active reservations Reservation.release_for_start is about to consume -- the same
    two-way match it uses, read first because release_for_start reports nothing about which
    ones it released."""
    clauses = [
        {
            "charge_point_identity": identity,
            "connector_id": {"$in": [connector_id, 0]},
            "id_tag": id_tag,
            "is_active": True,
        }
    ]
    if reservation_id is not None:
        clauses.append(
            {"charge_point_identity": identity, "reservation_id": reservation_id,
             "is_active": True}
        )
    return await Reservation.find({"$or": clauses}).to_list()


async def publish_reservations_released(reservations, reason):
    for reservation in reservations:
        await publish_event(
            "reservation_released", reservation.charge_point_identity,
            reservation.connector_id,
            {"reservation_id": reservation.reservation_id, "reason": reason},
        )


RESERVATION_SWEEP_INTERVAL_SECONDS = 60


async def sweep_expired_reservations_forever():
    """Background task: periodically release reservations past their expiry_date (s3.11).

    Runs for the lifetime of the process (see main()); a test never needs to wait on this --
    Reservation.sweep_expired() is called directly instead, since it is a plain, immediate
    database operation with no timing dependency of its own.
    """
    while True:
        await asyncio.sleep(RESERVATION_SWEEP_INTERVAL_SECONDS)
        try:
            lapsing = await Reservation.find(
                {"is_active": True, "expiry_date": {"$lte": datetime.now(UTC)}}
            ).to_list()
            released = await Reservation.sweep_expired()
            await publish_reservations_released(lapsing, "expired")
            if released:
                print(f"RESERVATION SWEEP: released {released} expired reservation(s)")
        except Exception:
            logging.exception("reservation expiry sweep failed")


async def reapply_persisted_availability(charge_point):
    """Re-push a stored Inoperative desired_availability after every boot.

    OCPP 1.6 s5.2: "Connector set to Unavailable shall persist a reboot." A real charger keeps
    that setting in its own non-volatile memory; a fresh connection under this identity (a real
    reboot, or this project's simulator restarting as a separate process) does not, so only this
    Central System's own record of the operator's intent survives -- and only this record can
    put it back. Nothing to do for Operative: Available is a charger's own power-on default.

    Sent unconditionally, even when this Central System's own last-known status for the
    connector already reads Unavailable: that value describes what THIS SYSTEM last heard, not
    what the charger that just reconnected actually remembers, and there is no way to tell the
    two apart from here. ChangeAvailability is idempotent for a charger that does remember
    (its own conf comes back Accepted for a no-op), so resending costs nothing.
    """
    for record in await ConnectorStatus.all_for(charge_point.id):
        if record.desired_availability != AvailabilityType.inoperative:
            continue
        try:
            await charge_point.call(
                call.ChangeAvailability(
                    connector_id=record.connector_id, type=AvailabilityType.inoperative
                ),
                suppress=False,
            )
        except Exception:
            logging.exception(
                "reapplying Inoperative availability failed for %s connector %s",
                charge_point.id, record.connector_id,
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
    await connect_event_publisher()
    server = await serve()
    # Not started from serve(): the test suite calls serve() directly, on a session-scoped
    # server shared by many tests, and has no use for a real-time sweep -- tests call
    # Reservation.sweep_expired() themselves instead (see sweep_expired_reservations_forever).
    asyncio.create_task(sweep_expired_reservations_forever())
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
