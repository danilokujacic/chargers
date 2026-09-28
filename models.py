"""Charge point registry, stored in MongoDB through Beanie.

Charge point authentication follows OCPP-J 1.6 section 6.2.2: a Charge Point
authenticates with HTTP Basic auth over TLS, using its identity -- the string it
puts in the OCPP-J connection URL -- as the username, and a 20-byte
authorization key as the password. That key travels over OCPP as a 40-character
hexadecimal string, set with ChangeConfiguration on the "AuthorizationKey" key.

The same section RECOMMENDS the Central System store the key hashed with a
unique salt, so a leak of this database does not let an attacker authenticate as
the chargers. Only a salted scrypt hash is kept here; the plaintext key is
returned once, when it is generated, so it can be installed on the device.
"""

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime

from beanie import Document, init_beanie
from ocpp.v16 import datatypes
from ocpp.v16.enums import AuthorizationStatus, ChargePointErrorCode, ChargePointStatus, Reason
from ocpp.v16.enums import RegistrationStatus
from pydantic import Field, field_validator
from pymongo import AsyncMongoClient, IndexModel, ReturnDocument

from connector_state_machine import ConnectorState

# OCPP-J 1.6 s6.2.2: "The password is a 20-byte key that is stored on the charge
# point", carried over OCPP as "a 40-character hexadecimal representation".
AUTHORIZATION_KEY_BYTES = 20
AUTHORIZATION_KEY_HEX_LEN = AUTHORIZATION_KEY_BYTES * 2

# The identity is a single path segment of the connection URL (OCPP-J 1.6
# s3.1.1). The spec sets no length limit and allows spaces ("RDAM 123"), so only
# "/" and control characters are rejected. The cap below is a sanity bound.
IDENTITY_MAX_LEN = 255

# scrypt cost: 128 * N * r is 16 MiB per verification, the usual interactive
# setting. Chargers connect rarely, so this is cheap enough to run per handshake.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

# Every field BootNotification.req can carry (OCPP 1.6 edition 2 s6.3). Only
# chargePointVendor and chargePointModel are required; the rest are sent at the
# charger's discretion.
BOOT_NOTIFICATION_FIELDS = (
    "charge_point_vendor",
    "charge_point_model",
    "charge_point_serial_number",
    "charge_box_serial_number",
    "firmware_version",
    "iccid",
    "imsi",
    "meter_serial_number",
    "meter_type",
)

DEFAULT_MONGODB_URL = "mongodb://localhost:27017"
DEFAULT_DATABASE = "ocpp_poc"


def _utcnow():
    """Timezone-aware UTC timestamp. main.py's now() returns the OCPP wire format."""
    return datetime.now(UTC)


def generate_authorization_key():
    """Return a fresh 20-byte authorization key as 40 uppercase hex characters."""
    return secrets.token_bytes(AUTHORIZATION_KEY_BYTES).hex().upper()


def normalize_authorization_key(key):
    """Validate a key as OCPP carries it and return canonical uppercase hex."""
    candidate = key.strip().upper()
    if len(candidate) != AUTHORIZATION_KEY_HEX_LEN:
        raise ValueError(
            f"authorization key must be {AUTHORIZATION_KEY_HEX_LEN} hex characters "
            f"({AUTHORIZATION_KEY_BYTES} bytes), got {len(candidate)}"
        )
    try:
        bytes.fromhex(candidate)
    except ValueError as exc:
        raise ValueError("authorization key must be hexadecimal") from exc
    return candidate


def authorization_key_from_basic_password(password):
    """Return the canonical hex key for a password taken from an HTTP Basic header.

    Chargers are found sending either form. The example in OCPP-J 1.6 s6.2.2
    puts the raw 20 bytes in the header -- which is not necessarily valid UTF-8,
    their own example is not -- while ChangeConfiguration carries the same key as
    40 hexadecimal characters, and that ASCII-safe form is what most
    implementations send. Both are accepted here.
    """
    try:
        candidate = password.decode("ascii")
    except UnicodeDecodeError:
        candidate = None
    if candidate is not None and len(candidate) == AUTHORIZATION_KEY_HEX_LEN:
        return normalize_authorization_key(candidate)
    if len(password) == AUTHORIZATION_KEY_BYTES:
        return password.hex().upper()
    raise ValueError(
        f"authorization key must be {AUTHORIZATION_KEY_BYTES} raw bytes or "
        f"{AUTHORIZATION_KEY_HEX_LEN} hex characters, got {len(password)} bytes"
    )


def hash_authorization_key(key, salt=None):
    """Hash a key with scrypt and a unique salt, as OCPP-J 1.6 s6.2.2 recommends."""
    salt = secrets.token_bytes(_SALT_BYTES) if salt is None else salt
    digest = hashlib.scrypt(
        normalize_authorization_key(key).encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_authorization_key(key, stored_hash):
    """Constant-time check of a plaintext key against a stored hash."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored_hash.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            normalize_authorization_key(key).encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
    except ValueError:
        # Malformed hash, or a key that is not 40 hex characters.
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


class ChargePoint(Document):
    """A charge point registered with this central system."""

    identity: str
    authorization_key_hash: str
    key_updated_at: datetime = Field(default_factory=_utcnow)
    # Onboarding per OCPP-J 1.6 s6.2.2: a charger stays pending until its unique
    # key is set, then BootNotification.conf may answer Accepted.
    registration_status: RegistrationStatus = RegistrationStatus.pending
    # Route B onboarding (see commissioning.py): a ChangeConfiguration pushing a new key is
    # in flight. The candidate's hash lives separately so a rejected, unsupported or
    # unanswered change can be discarded without ever touching the key that still works.
    key_rotation_pending: bool = False
    pending_authorization_key_hash: str | None = None
    # Everything BootNotification reports, rather than supplied at registration.
    charge_point_vendor: str | None = None
    charge_point_model: str | None = None
    charge_point_serial_number: str | None = None
    # Deprecated in OCPP 1.6 and to be removed in a future version, but older
    # chargers still send it, so it is kept rather than dropped on the floor.
    charge_box_serial_number: str | None = None
    firmware_version: str | None = None
    # Identifiers of the SIM in the charger's modem, on cellular units only.
    iccid: str | None = None
    imsi: str | None = None
    meter_serial_number: str | None = None
    meter_type: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "charge_points"
        indexes = [IndexModel("identity", unique=True, name="identity_unique")]
        validate_on_save = True

    @field_validator("identity")
    @classmethod
    def _check_identity(cls, value):
        identity = value.strip()
        if not identity:
            raise ValueError("identity must not be empty")
        if len(identity) > IDENTITY_MAX_LEN:
            raise ValueError(f"identity must be at most {IDENTITY_MAX_LEN} characters")
        if "/" in identity:
            raise ValueError("identity must not contain '/': it is one URL path segment")
        if any(ord(char) < 32 or ord(char) == 127 for char in identity):
            raise ValueError("identity must not contain control characters")
        return identity

    @classmethod
    async def register(cls, identity, authorization_key=None, **fields):
        """Register a charge point and return it with its plaintext key.

        The key is returned only here. It has to be installed on the device --
        in the factory, or over OCPP with ChangeConfiguration on
        "AuthorizationKey" -- because only its hash is stored.
        """
        key = (
            generate_authorization_key()
            if authorization_key is None
            else normalize_authorization_key(authorization_key)
        )
        charge_point = cls(
            identity=identity, authorization_key_hash=hash_authorization_key(key), **fields
        )
        await charge_point.insert()
        return charge_point, key

    async def rotate_authorization_key(self, authorization_key=None):
        """Store a hash of a new key and return that key in plaintext."""
        key = (
            generate_authorization_key()
            if authorization_key is None
            else normalize_authorization_key(authorization_key)
        )
        self.authorization_key_hash = hash_authorization_key(key)
        self.key_updated_at = _utcnow()
        self.updated_at = self.key_updated_at
        await self.save()
        return key

    def verify_authorization_key(self, key):
        """True when key matches the hash stored for this charge point."""
        return verify_authorization_key(key, self.authorization_key_hash)

    async def begin_key_rotation(self, authorization_key=None):
        """Stage a candidate key for Route B onboarding and return it in plaintext.

        The CURRENT key keeps authenticating until confirm_key_rotation promotes this
        candidate: OCPP-J 1.6 s6.2.2 requires the old credentials to keep working until the
        charger has confirmed the new ones, precisely so a lost or unanswered
        ChangeConfiguration can never lock a charger out.
        """
        key = (
            generate_authorization_key()
            if authorization_key is None
            else normalize_authorization_key(authorization_key)
        )
        self.pending_authorization_key_hash = hash_authorization_key(key)
        self.key_rotation_pending = True
        self.updated_at = _utcnow()
        await self.save()
        return key

    async def confirm_key_rotation(self):
        """Promote the candidate key to the active one and accept the charger.

        Call only after the charger has answered its ChangeConfiguration with Accepted.
        OCPP-J 1.6 s6.2.2: "Only when this ChangeConfiguration.req has been responded to
        with a ChangeConfiguration.conf with a status of Accepted, will the Central System
        respond to a boot notification with an Accepted registration status."
        """
        if self.pending_authorization_key_hash is None:
            raise ValueError(f"{self.identity} has no pending key to confirm")
        self.authorization_key_hash = self.pending_authorization_key_hash
        self.pending_authorization_key_hash = None
        self.key_rotation_pending = False
        self.key_updated_at = _utcnow()
        self.registration_status = RegistrationStatus.accepted
        self.updated_at = self.key_updated_at
        await self.save()

    async def cancel_key_rotation(self):
        """Discard the candidate key and keep the current one working.

        OCPP-J 1.6 s6.2.2: if the ChangeConfiguration comes back Rejected or NotSupported --
        or never comes back at all -- "the Central System SHALL keep accepting the old
        credentials." registration_status is left untouched: a charger mid Route-B onboarding
        simply stays Pending and may be retried on its next BootNotification.
        """
        self.pending_authorization_key_hash = None
        self.key_rotation_pending = False
        self.updated_at = _utcnow()
        await self.save()

    async def record_boot(self, **reported):
        """Store what a BootNotification reported and stamp the charger as seen.

        Only fields the charger actually sent are written. The optional ones are
        absent rather than null, so a later boot that leaves one out must not
        erase what an earlier boot reported. Anything that is not a
        BootNotification field is ignored.
        """
        for name in BOOT_NOTIFICATION_FIELDS:
            if name in reported:
                setattr(self, name, reported[name])
        self.last_seen_at = _utcnow()
        self.updated_at = self.last_seen_at
        await self.save()

    @classmethod
    async def authenticate(cls, identity, authorization_key):
        """Resolve the HTTP Basic credentials from an OCPP-J handshake.

        Returns the charge point, or None when the identity is unknown or the
        key does not match. Registration status is deliberately not checked: a
        pending charger still connects, that is how it gets onboarded.
        """
        charge_point = await cls.find_one(cls.identity == identity)
        if charge_point is None:
            return None
        if not charge_point.verify_authorization_key(authorization_key):
            return None
        return charge_point


class IdTag(Document):
    """A driver's credential -- the thing Authorize.req carries (OCPP 1.6 s3.9).

    This is distinct from ChargePoint.authorization_key_hash: that authenticates a CHARGER
    (a machine) to the Central System over the WebSocket handshake. This authorizes a DRIVER
    to draw energy from a charger, once per charging session, over the OCPP Authorize message.
    """

    id_tag: str
    # s3.9: an idTag "MAY contain any data ... meaningful to a Central System", commonly an
    # RFID UID (8 or 14 hex chars) but sometimes a virtual, single-use app token. Its shape is
    # deliberately not validated here.
    status: AuthorizationStatus = AuthorizationStatus.accepted
    # s3.10: idTags sharing a parent_id_tag are one group; any one of them may stop a
    # transaction another one started. Never compare a presented tag against a parent value.
    parent_id_tag: str | None = None
    expiry_date: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "id_tags"
        indexes = [IndexModel("id_tag", unique=True, name="id_tag_unique")]

    @classmethod
    async def _resolve(cls, id_tag):
        """The IdTagInfo for id_tag from its own stored status and expiry alone.

        Shared by authorize() and authorize_stop(), which each layer a further rule (Authorize
        checks for a concurrent transaction; authorize_stop checks group membership) on top of
        this. Also returns the stored record itself (or None), since callers need it too.
        """
        record = await cls.find_one(cls.id_tag == id_tag)
        if record is None:
            return datatypes.IdTagInfo(status=AuthorizationStatus.invalid), None
        if record.expiry_date is not None and record.expiry_date <= _utcnow():
            # A lapsed credential must not keep authorizing charges just because nobody got
            # around to changing its stored status.
            info = datatypes.IdTagInfo(
                status=AuthorizationStatus.expired, parent_id_tag=record.parent_id_tag
            )
            return info, record
        info = datatypes.IdTagInfo(
            status=record.status,
            parent_id_tag=record.parent_id_tag,
            expiry_date=record.expiry_date.isoformat() if record.expiry_date else None,
        )
        return info, record

    @classmethod
    async def authorize(cls, id_tag):
        """Resolve an idTag to the IdTagInfo OCPP wants in Authorize.conf (OCPP 1.6 s3.9).

        Unknown tags are Invalid. A tag past its expiry_date is Expired regardless of what
        status is stored. A tag that already has an open transaction elsewhere is ConcurrentTx:
        one RFID card cannot run two chargers at once.
        """
        info, _record = await cls._resolve(id_tag)
        if info.status == AuthorizationStatus.accepted:
            if await Transaction.find_one(
                Transaction.id_tag == id_tag, Transaction.is_open == True  # noqa: E712
            ):
                return datatypes.IdTagInfo(
                    status=AuthorizationStatus.concurrent_tx, parent_id_tag=info.parent_id_tag
                )
        return info

    @classmethod
    async def authorize_stop(cls, id_tag, starting_id_tag):
        """Resolve the IdTagInfo for a StopTransaction.req that carried an idTag (s4.10).

        The tag's own status (blocked, expired, ...) applies as in authorize(), but the
        concurrent-transaction check does not -- stopping one transaction while another is
        open elsewhere is normal and not a conflict. On top of that, s3.10 lets any tag in the
        same parent group as the one that started the transaction stop it; an unrelated tag,
        even a perfectly valid one, has no standing over someone else's transaction and is
        reported Invalid.
        """
        info, record = await cls._resolve(id_tag)
        if info.status != AuthorizationStatus.accepted or id_tag == starting_id_tag:
            return info
        _starting_info, starting_record = await cls._resolve(starting_id_tag)
        same_group = (
            record is not None
            and starting_record is not None
            and record.parent_id_tag is not None
            and record.parent_id_tag == starting_record.parent_id_tag
        )
        return info if same_group else datatypes.IdTagInfo(status=AuthorizationStatus.invalid)


class Transaction(Document):
    """A billable charging transaction (OCPP 1.6 s2.2, s4.8, s4.10).

    Starts once preconditions are met (authorization, a plug inserted) and ends when the
    charger reports it has "irrevocably left this state". Narrower than a Charging Session,
    which can begin with just a card swipe or a cable being plugged in, before any transaction
    -- and so before any transaction_id -- exists.
    """

    transaction_id: int
    charge_point_identity: str
    # A real transaction is always on a connector >= 1 (OCPP 1.6 s3.8: connector 0 is the main
    # controller, never a charging point). record_orphan_stop uses 0 as an explicit "unknown"
    # sentinel for the one case where the real connector is genuinely not known.
    connector_id: int
    id_tag: str | None = None  # None only for an orphaned stop; see record_orphan_stop
    stopped_by_id_tag: str | None = None
    meter_start: int
    meter_stop: int | None = None
    # As reported by the charger, not server time: a charger that was offline delivers these
    # messages late, hours after the fact, and the timestamps must reflect when charging
    # actually happened for billing to be correct.
    started_at: datetime
    stopped_at: datetime | None = None
    stop_reason: Reason | None = None
    meter_values: list[dict] = Field(default_factory=list)
    is_open: bool = True
    # True when a StopTransaction arrived with no matching StartTransaction on file -- can
    # happen for a charger reconnecting after being offline. meter_start, connector_id and
    # id_tag are then unknown and must not be mistaken for a normally metered session.
    incomplete: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "transactions"
        indexes = [
            IndexModel("transaction_id", unique=True, name="transaction_id_unique"),
            IndexModel(
                [("charge_point_identity", 1), ("connector_id", 1), ("is_open", 1)],
                name="open_transaction_lookup",
            ),
        ]

    @classmethod
    async def find_open(cls, charge_point_identity, connector_id):
        """The currently open transaction on a connector, or None."""
        return await cls.find_one(
            cls.charge_point_identity == charge_point_identity,
            cls.connector_id == connector_id,
            cls.is_open == True,  # noqa: E712
        )

    @classmethod
    async def start(cls, charge_point_identity, connector_id, id_tag, meter_start, started_at):
        """Allocate a transaction_id and create the transaction record."""
        transaction = cls(
            transaction_id=await next_transaction_id(),
            charge_point_identity=charge_point_identity,
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
            started_at=started_at,
        )
        await transaction.insert()
        return transaction

    @classmethod
    async def record_orphan_stop(
        cls,
        transaction_id,
        charge_point_identity,
        meter_stop,
        stopped_at,
        stopped_by_id_tag=None,
        stop_reason=None,
    ):
        """Create a transaction record for a StopTransaction whose Start was never seen.

        This genuinely happens: a charger reconnecting after time offline can deliver a queued
        Stop before this Central System ever saw the matching Start. meter_start and the
        connector are unknown, so connector_id uses 0 (otherwise never a real transaction's
        connector) and incomplete=True marks this as never fully metered.
        """
        transaction = cls(
            transaction_id=transaction_id,
            charge_point_identity=charge_point_identity,
            connector_id=0,
            meter_start=0,
            meter_stop=meter_stop,
            started_at=stopped_at,
            stopped_at=stopped_at,
            stopped_by_id_tag=stopped_by_id_tag,
            stop_reason=stop_reason,
            is_open=False,
            incomplete=True,
        )
        await transaction.insert()
        return transaction

    async def add_meter_values(self, meter_value):
        """Append MeterValues.req readings, as received, to this transaction's history."""
        self.meter_values.extend(meter_value)
        self.updated_at = _utcnow()
        await self.save()

    async def stop(self, meter_stop, stopped_at, stopped_by_id_tag=None, stop_reason=None):
        """Close the transaction. A no-op if it is already closed.

        Chargers redeliver StopTransaction after a reconnect; the FIRST delivery wins and later
        identical ones must not be able to corrupt the stored record.
        """
        if not self.is_open:
            return
        self.meter_stop = meter_stop
        self.stopped_at = stopped_at
        self.stopped_by_id_tag = stopped_by_id_tag
        self.stop_reason = stop_reason
        self.is_open = False
        self.updated_at = _utcnow()
        await self.save()


class ConnectorStatus(Document):
    """The last reported status of one connector, persisted so it survives a Central System
    restart. connector_state_machine.py holds the transition RULES; this is only where the
    current VALUE lives between connections.
    """

    charge_point_identity: str
    connector_id: int
    status: ChargePointStatus = ChargePointStatus.available
    error_code: ChargePointErrorCode = ChargePointErrorCode.no_error
    info: str | None = None
    vendor_id: str | None = None
    vendor_error_code: str | None = None
    # Mirrors ConnectorState.pre_fault_status, so recover_from_fault still works correctly for
    # a connector that was already Faulted before the Central System last restarted.
    pre_fault_status: ChargePointStatus | None = None
    # The most recent MeterValues.req reading NOT tied to a transaction (no transactionId in
    # the request) -- a standalone, clock-driven sample. Overwritten each time, not
    # accumulated: a running history of these is outside this task's scope.
    last_meter_values: list[dict] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "connector_statuses"
        indexes = [
            IndexModel(
                [("charge_point_identity", 1), ("connector_id", 1)],
                unique=True,
                name="charge_point_connector_unique",
            )
        ]

    @classmethod
    async def get_or_create(cls, charge_point_identity, connector_id):
        """This connector's persisted status, creating a fresh Available record if none exists."""
        record = await cls.find_one(
            cls.charge_point_identity == charge_point_identity,
            cls.connector_id == connector_id,
        )
        if record is None:
            record = cls(charge_point_identity=charge_point_identity, connector_id=connector_id)
            await record.insert()
        return record

    @classmethod
    async def all_for(cls, charge_point_identity):
        """Every persisted connector status for one charge point."""
        return await cls.find(cls.charge_point_identity == charge_point_identity).to_list()

    def to_connector_state(self):
        """A fresh connector_state_machine.ConnectorState seeded with this document."""
        return ConnectorState(
            self.connector_id, status=self.status, pre_fault_status=self.pre_fault_status
        )

    async def apply(
        self, state, error_code=ChargePointErrorCode.no_error, info=None,
        vendor_id=None, vendor_error_code=None,
    ):
        """Persist a ConnectorState's current status and the StatusNotification's metadata."""
        self.status = state.status
        self.pre_fault_status = state.pre_fault_status
        self.error_code = error_code
        self.info = info
        self.vendor_id = vendor_id
        self.vendor_error_code = vendor_error_code
        self.updated_at = _utcnow()
        await self.save()

    async def record_meter_values(self, meter_value):
        """Store a standalone (no transactionId) MeterValues.req reading against the connector."""
        self.last_meter_values = meter_value
        self.updated_at = _utcnow()
        await self.save()


async def next_transaction_id():
    """Atomically allocate the next transaction_id.

    OCPP 1.6 s4.8 requires a positive integer, unique across the Central System and stable --
    a charger echoes it back in every MeterValues and in StopTransaction. An ObjectId, a random
    number, or a count of documents cannot serve as this: two chargers can start a transaction
    in the same millisecond, and only an atomic increment on one counter document guarantees no
    two ever collide.
    """
    result = await get_database()["counters"].find_one_and_update(
        {"_id": "transaction_id"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result["value"]


def mongodb_url():
    """The configured MongoDB URL."""
    return os.environ.get("MONGODB_URL", DEFAULT_MONGODB_URL)


_database = None  # set by init_db(); backs get_database()


def get_database():
    """The database init_db() connected to, for code that needs raw collection access
    (next_transaction_id's counter document has no beanie Document of its own).

    Raises RuntimeError if init_db() has not run yet.
    """
    if _database is None:
        raise RuntimeError("init_db() has not been called yet")
    return _database


async def init_db(url=None, database=None, server_selection_timeout_ms=5000):
    """Connect to MongoDB, register the document models, and return the client.

    The default server selection timeout is shorter than pymongo's 30s so that a
    missing database fails fast instead of hanging.
    """
    global _database
    client = AsyncMongoClient(
        url or mongodb_url(),
        serverSelectionTimeoutMS=server_selection_timeout_ms,
        # BSON datetimes carry no timezone; pymongo decodes them as naive UTC by default. Every
        # datetime this module writes comes from _utcnow() (timezone-aware), so without this a
        # value read back from MongoDB cannot be compared against a fresh _utcnow() call --
        # e.g. IdTag.expiry_date <= _utcnow() -- without raising TypeError.
        tz_aware=True,
    )
    _database = client[database or os.environ.get("MONGODB_DB", DEFAULT_DATABASE)]
    await init_beanie(
        database=_database,
        document_models=[ChargePoint, IdTag, Transaction, ConnectorStatus],
    )
    return client
