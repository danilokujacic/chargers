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
from enum import StrEnum

from beanie import Document, PydanticObjectId, init_beanie
from ocpp.v16 import datatypes
from ocpp.v16.enums import (
    AuthorizationStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfilePurposeType,
    DiagnosticsStatus,
    FirmwareStatus,
    Reason,
)
from ocpp.v16.enums import RegistrationStatus
from pydantic import Field, field_validator
from pymongo import AsyncMongoClient, IndexModel, ReturnDocument
from pymongo.errors import DuplicateKeyError

from charging_profiles import TX
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
    # Where this charger is on the map (07-public-map-platform.md §A). site_id takes priority
    # over the charger's own latitude/longitude whenever both are set -- see map_location()'s
    # three-way fallback, the one place that ordering is allowed to matter.
    site_id: PydanticObjectId | None = None
    latitude: float | None = None
    longitude: float | None = None
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

    async def map_location(self):
        """This charger's effective map location (07-public-map-platform.md §A), or None if it
        has none yet.

        Three-way fallback, in order: a Site's coordinates, if this charger belongs to one
        (authoritative even when the charger also has its own latitude/longitude set); else the
        charger's own coordinates, if both are set (a standalone "site of one"); else None,
        meaning this charger simply does not appear on the map yet -- it still works over OCPP
        exactly as before.
        """
        if self.site_id is not None:
            site = await Site.get(self.site_id)
            if site is not None:
                return site.latitude, site.longitude
        if self.latitude is not None and self.longitude is not None:
            return self.latitude, self.longitude
        return None

    async def set_location(self, site_id=None, latitude=None, longitude=None):
        """Assign this charger to a Site, or give it its own standalone coordinates -- an
        operator action, not something OCPP itself ever reports (07-public-map-platform.md §B).
        Only touches the fields actually given, so setting one does not clobber the other.
        """
        if site_id is not None:
            self.site_id = site_id
        if latitude is not None:
            self.latitude = latitude
        if longitude is not None:
            self.longitude = longitude
        self.updated_at = _utcnow()
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


class SiteType(StrEnum):
    """What kind of physical place a Site is (07-public-map-platform.md §A). Not an OCPP
    concept -- OCPP has no notion of a location hosting several Charge Points, only Charge
    Points and their Connectors -- so this is defined here rather than reused from `ocpp`."""

    gas_station = "gas_station"
    hotel = "hotel"
    parking = "parking"
    other = "other"


class SiteSource(StrEnum):
    """Whether this Central System actually operates a Site, or merely knows it exists
    (07-public-map-platform.md §A's "sites we operate vs. sites we only know about")."""

    operator = "operator"
    external_reference = "external_reference"


class Site(Document):
    """A physical place that can host one or more ChargePoints -- a fuel station, a hotel car
    park, a public car park, whatever (07-public-map-platform.md §A). Deliberately generic
    rather than named after any one kind of venue: nothing about routing, display, or live
    status differs between them, only the label an operator sees.

    `source: operator` means this Central System genuinely manages at least one ChargePoint
    here (real live status is possible); `source: external_reference` means this system only
    knows the location exists -- e.g. imported from a third-party directory
    (`09-plugshare-import.md`) -- and has never connected to anything here, so its live status
    is always reported as "unknown", never guessed at.
    """

    name: str
    site_type: SiteType = SiteType.other
    latitude: float
    longitude: float
    address: str | None = None
    source: SiteSource = SiteSource.operator
    # The source platform's own id/permalink, kept only so a re-run import recognises "already
    # imported" and updates rather than duplicates -- never used as a foreign key into anything.
    external_id: str | None = None
    external_url: str | None = None
    # Physical plug standards available here (e.g. ["CCS2", "CHAdeMO", "Type 2"]), as plain
    # human-readable strings -- never a third-party's own numeric connector-type code.
    connector_types: list[str] | None = None
    # Only meaningful for source=external_reference: how many chargers a third-party source
    # reported here. There is no live ChargePoint to count for such a site, unlike an operator
    # site, where "how many chargers" is always counted live from real ChargePoint documents
    # instead of stored here (07-public-map-platform.md §A, "Two different meanings of 'how
    # many chargers'").
    external_charge_point_count: int | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "sites"
        # No index beyond the default _id: this project's expected fleet size (dozens to a few
        # hundred sites) needs no server-side geospatial query -- see
        # 07-public-map-platform.md §B for why a 2dsphere index is a deliberate future step, not
        # a gap.

    @classmethod
    async def create(cls, name, site_type, latitude, longitude, address=None, source=None):
        """Create a new Site. Defaults to source=operator -- the one-off PlugShare-style import
        (09-plugshare-import.md) is the only caller that ever passes source=external_reference.
        """
        site = cls(
            name=name,
            site_type=site_type,
            latitude=latitude,
            longitude=longitude,
            address=address,
            source=source if source is not None else SiteSource.operator,
        )
        await site.insert()
        return site


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
    # The operator's intent, set by ChangeAvailability (OCPP 1.6 s5.2), kept separate from
    # `status` above because the two can disagree for a while: a charger mid-transaction
    # answers Scheduled and only actually goes Unavailable once that transaction ends.
    # "Connector set to Unavailable shall persist a reboot" (s5.2) -- storing this here, rather
    # than only holding it in the live MyChargePoint object, is what survives both a Central
    # System restart and a charger reconnecting with no memory of its own (see
    # main.py's reapply_persisted_availability).
    desired_availability: AvailabilityType = AvailabilityType.operative
    # True from the moment a ChangeAvailability.conf comes back Scheduled until the deferred
    # change actually lands (cleared in apply() once status reflects desired_availability).
    availability_change_scheduled: bool = False
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
        """This connector's persisted status, creating a fresh Available record if none exists.

        Two callers can race here for the same connector on its very first document -- e.g.
        ChangeAvailability persisting desired_availability at the same moment the charger's own
        resulting StatusNotification is being handled -- so a lost race on the unique index is
        expected, not a bug: the winner's document is exactly what this call would have created,
        so it is fetched and returned instead of raising.
        """
        record = await cls.find_one(
            cls.charge_point_identity == charge_point_identity,
            cls.connector_id == connector_id,
        )
        if record is not None:
            return record
        record = cls(charge_point_identity=charge_point_identity, connector_id=connector_id)
        try:
            await record.insert()
        except DuplicateKeyError:
            record = await cls.find_one(
                cls.charge_point_identity == charge_point_identity,
                cls.connector_id == connector_id,
            )
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
        """Persist a ConnectorState's current status and the StatusNotification's metadata.

        Writes only these fields with an atomic $set (Document.set), not a mutate-then-save of
        the whole document: set_desired_availability can be updating this same document at the
        same moment (a ChangeAvailability persisting the operator's intent right as the
        charger's own resulting StatusNotification comes back for the very first time on a
        connector), and two racing whole-document saves would let whichever finishes last
        silently discard the other's fields.
        """
        scheduled = self.availability_change_scheduled
        if scheduled and (
            (self.desired_availability == AvailabilityType.inoperative)
            == (state.status == ChargePointStatus.unavailable)
        ):
            # The deferred ChangeAvailability this status update was waiting on has now
            # actually landed (s5.2): Unavailable for an Inoperative request, anything else for
            # an Operative one.
            scheduled = False
        await self.set({
            "status": state.status,
            "pre_fault_status": state.pre_fault_status,
            "error_code": error_code,
            "info": info,
            "vendor_id": vendor_id,
            "vendor_error_code": vendor_error_code,
            "availability_change_scheduled": scheduled,
            "updated_at": _utcnow(),
        })

    async def set_desired_availability(self, availability, scheduled=False):
        """Record a ChangeAvailability request's outcome (OCPP 1.6 s5.2).

        Called after the charger's conf comes back Accepted or Scheduled -- never Rejected,
        which leaves the previous desired_availability untouched. Uses the same atomic $set as
        apply(), for the same reason: this can race with it on the same document.
        """
        await self.set({
            "desired_availability": availability,
            "availability_change_scheduled": scheduled,
            "updated_at": _utcnow(),
        })

    async def record_meter_values(self, meter_value):
        """Store a standalone (no transactionId) MeterValues.req reading against the connector."""
        self.last_meter_values = meter_value
        self.updated_at = _utcnow()
        await self.save()


class ConfigurationEntry(Document):
    """One configuration key this charger has reported (GetConfiguration, OCPP 1.6 s5.9) or that
    this Central System has pushed to it (ChangeConfiguration, s5.6) -- kept per charger so an
    operator can see what a specific unit supports without querying it live again.

    `value` is None for a key the charger reports knowing about but with nothing to show (a
    write-only key, or -- deliberately, see AUTHORIZATION_KEY_CONFIG_KEY in commissioning.py --
    "AuthorizationKey" itself: OCPP-J 1.6 s6.2.2 says a charger "should not give back the
    authorization key in response to a GetConfiguration request", so this project never stores
    a value for it even if a charger's simulator answers one).
    """

    charge_point_identity: str
    key: str
    value: str | None = None
    readonly: bool = False
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "configuration_entries"
        indexes = [
            IndexModel(
                [("charge_point_identity", 1), ("key", 1)],
                unique=True,
                name="charge_point_key_unique",
            )
        ]

    @classmethod
    async def upsert(cls, charge_point_identity, key, value, readonly=False):
        """Record one key's current value, creating or overwriting as needed.

        Written as a single atomic upsert on the raw collection, not a find-then-insert-or-save:
        a full GetConfiguration sweep calls this once per reported key, and ConnectorStatus's
        get_or_create/apply race (see instructions/06a-remaining-flows-progress.md) is exactly
        the failure mode an unprotected find-then-write would risk here too, if a second sweep
        or a ChangeConfiguration for the same key ever overlaps this one.
        """
        await get_database()["configuration_entries"].update_one(
            {"charge_point_identity": charge_point_identity, "key": key},
            {"$set": {"value": value, "readonly": readonly, "updated_at": _utcnow()}},
            upsert=True,
        )

    @classmethod
    async def all_for(cls, charge_point_identity):
        """Every configuration key on record for one charge point."""
        return await cls.find(cls.charge_point_identity == charge_point_identity).to_list()


class Reservation(Document):
    """A connector, or a whole charge point, held for one idTag until it is used, cancelled, or
    its expiry passes (OCPP 1.6 s3.11, ReserveNow s5.15, CancelReservation s5.3).

    `connector_id` follows ReserveNow.req: 0 means "not for a specific connector" -- the
    reservation is honoured on whichever connector the idTag is first presented on, and never
    itself moves a connector to the Reserved status (see connector_state_machine's
    MAIN_CONTROLLER_STATUSES, which does not include Reserved for connector 0 anyway).

    `is_active` plus `released_reason`/`released_at` is this project's usual open/closed-with-a-
    reason shape (compare Transaction.is_open/stop_reason): a reservation is active until
    exactly one of three things happens, per s3.11 -- "consumed" (the reserved idTag was used,
    on the reserved connector or, for a connectorId 0 reservation, any connector), "cancelled"
    (CancelReservation.req), or "expired" (past expiry_date; see sweep_expired).
    """

    reservation_id: int
    charge_point_identity: str
    connector_id: int
    id_tag: str
    parent_id_tag: str | None = None
    expiry_date: datetime
    is_active: bool = True
    released_reason: str | None = None  # "consumed" | "cancelled" | "expired"
    released_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "reservations"
        indexes = [
            IndexModel("reservation_id", unique=True, name="reservation_id_unique"),
            IndexModel(
                [("charge_point_identity", 1), ("connector_id", 1), ("is_active", 1)],
                name="active_reservation_lookup",
            ),
        ]

    @classmethod
    async def create(
        cls, reservation_id, charge_point_identity, connector_id, id_tag, expiry_date,
        parent_id_tag=None,
    ):
        """Record a reservation this Central System has just asked a charger to accept.

        Call only after ReserveNow.conf comes back Accepted -- Faulted/Occupied/Rejected/
        Unavailable mean nothing changed on the charger, so nothing should be recorded here.
        """
        reservation = cls(
            reservation_id=reservation_id,
            charge_point_identity=charge_point_identity,
            connector_id=connector_id,
            id_tag=id_tag,
            parent_id_tag=parent_id_tag,
            expiry_date=expiry_date,
        )
        await reservation.insert()
        return reservation

    @classmethod
    async def release_for_start(
        cls, charge_point_identity, connector_id, id_tag, reservation_id=None
    ):
        """Release whatever reservation(s) OCPP 1.6 s3.11 says this StartTransaction ends.

        Two independent ways a reservation can be the one this transaction consumes, covered
        together in one atomic update: the transaction names it directly (StartTransaction.req's
        optional reservationId), or -- s3.11's own wording, "the reserved idTag is used on the
        reserved connector, or on any connector when connectorId was 0/unspecified" -- the
        idTag matches a still-active reservation on this connector or on a connectorId-0 (any
        connector) reservation for this charge point, whether or not the charger echoed
        reservationId back.
        """
        id_tag_match = {
            "charge_point_identity": charge_point_identity,
            "connector_id": {"$in": [connector_id, 0]},
            "id_tag": id_tag,
            "is_active": True,
        }
        query = {"$or": [id_tag_match]}
        if reservation_id is not None:
            query["$or"].append(
                {
                    "charge_point_identity": charge_point_identity,
                    "reservation_id": reservation_id,
                    "is_active": True,
                }
            )
        await get_database()["reservations"].update_many(
            query,
            {"$set": {"is_active": False, "released_reason": "consumed", "released_at": _utcnow()}},
        )

    @classmethod
    async def release_by_cancel(cls, reservation_id):
        """Mark a reservation cancelled. Returns True if an active one was found and released.

        Call only after CancelReservation.conf comes back Accepted -- the charger is the
        authority on whether it actually knew about this reservation_id.
        """
        result = await get_database()["reservations"].update_one(
            {"reservation_id": reservation_id, "is_active": True},
            {
                "$set": {
                    "is_active": False, "released_reason": "cancelled", "released_at": _utcnow(),
                }
            },
        )
        return result.modified_count > 0

    @classmethod
    async def sweep_expired(cls):
        """Release every reservation whose expiry_date has passed (OCPP 1.6 s3.11).

        Pure time-based check with no side effect on the charger -- a real charger tracks its
        own expiry and reverts the connector's status on its own clock; this only keeps this
        Central System's own bookkeeping (what StartTransaction/CancelReservation match
        against) from calling a lapsed reservation active. Callable directly, so tests never
        need to wait on main.py's periodic background sweep to observe this.
        """
        result = await get_database()["reservations"].update_many(
            {"is_active": True, "expiry_date": {"$lte": _utcnow()}},
            {"$set": {"is_active": False, "released_reason": "expired", "released_at": _utcnow()}},
        )
        return result.modified_count

    @classmethod
    async def find_active(cls, charge_point_identity, reservation_id):
        """The active reservation with this id on this charger, or None."""
        return await cls.find_one(
            cls.charge_point_identity == charge_point_identity,
            cls.reservation_id == reservation_id,
            cls.is_active == True,  # noqa: E712
        )


class LocalListState(Document):
    """Per-charger bookkeeping for the Local Authorization List a Central System can push with
    SendLocalList (OCPP 1.6 s5.8, s3.6): the version number and the set of idTags this Central
    System believes it has told the charger to hold.

    The idTags themselves are not duplicated here -- IdTag is this project's one source of truth
    for a driver credential's status everywhere else (Authorize, StartTransaction, ...); this
    only tracks which of them have been pushed, and at what version, so GetLocalListVersion's
    answer has something to be compared against.
    """

    charge_point_identity: str
    list_version: int = 0
    id_tags: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "local_list_state"
        indexes = [
            IndexModel(
                "charge_point_identity", unique=True, name="charge_point_identity_unique"
            )
        ]

    @classmethod
    async def get_or_create(cls, charge_point_identity):
        """This charger's Local List bookkeeping, creating an empty, version-0 record if none
        exists yet (a charger with nothing pushed is implicitly at version 0)."""
        record = await cls.find_one(cls.charge_point_identity == charge_point_identity)
        if record is not None:
            return record
        record = cls(charge_point_identity=charge_point_identity)
        try:
            await record.insert()
        except DuplicateKeyError:
            record = await cls.find_one(cls.charge_point_identity == charge_point_identity)
        return record

    async def record_full_push(self, list_version, id_tags):
        """A Full SendLocalList replaces the entire list (OCPP 1.6 s3.6)."""
        await self.set(
            {"list_version": list_version, "id_tags": sorted(set(id_tags)), "updated_at": _utcnow()}
        )

    async def record_differential_push(self, list_version, added_id_tags, removed_id_tags):
        """A Differential SendLocalList merges into the existing list (OCPP 1.6 s3.6)."""
        updated = (set(self.id_tags) | set(added_id_tags)) - set(removed_id_tags)
        await self.set(
            {"list_version": list_version, "id_tags": sorted(updated), "updated_at": _utcnow()}
        )


class FaultEvent(Document):
    """One fault occurrence on a connector, or (connector_id 0) the whole Charge Point (OCPP 1.6
    s4.9, s3.8).

    StatusNotification's error_code only ever describes the CURRENT status: the next status
    report overwrites it with no memory of what came before, so without a separate record, a
    fault that has already cleared by the time anyone looks is invisible. cleared_at is None
    while the fault is still active; exactly one open (cleared_at is None) event ever exists per
    connector at a time, closed the moment that connector's status next leaves Faulted.
    """

    charge_point_identity: str
    connector_id: int
    error_code: ChargePointErrorCode
    info: str | None = None
    vendor_id: str | None = None
    vendor_error_code: str | None = None
    entered_at: datetime = Field(default_factory=_utcnow)
    cleared_at: datetime | None = None

    class Settings:
        name = "fault_events"
        indexes = [
            IndexModel(
                [("charge_point_identity", 1), ("connector_id", 1), ("cleared_at", 1)],
                name="open_fault_lookup",
            )
        ]

    @classmethod
    async def open_new(
        cls, charge_point_identity, connector_id, error_code, info=None, vendor_id=None,
        vendor_error_code=None,
    ):
        """Record a connector's transition into Faulted. Call only when it was not already
        Faulted -- see main.py's on_status, which tracks the status just before this one for
        exactly that check."""
        event = cls(
            charge_point_identity=charge_point_identity,
            connector_id=connector_id,
            error_code=error_code,
            info=info,
            vendor_id=vendor_id,
            vendor_error_code=vendor_error_code,
        )
        await event.insert()
        return event

    @classmethod
    async def close_open(cls, charge_point_identity, connector_id):
        """Mark this connector's currently open fault (if any) cleared. Returns True if one was
        found -- False is not an error: a connector leaving Faulted always has exactly one open
        event to close, but this stays safe to call even if bookkeeping ever gets out of step.
        """
        result = await get_database()["fault_events"].update_one(
            {
                "charge_point_identity": charge_point_identity,
                "connector_id": connector_id,
                "cleared_at": None,
            },
            {"$set": {"cleared_at": _utcnow()}},
        )
        return result.modified_count > 0

    @classmethod
    async def history_for(cls, charge_point_identity, connector_id=None):
        """This charger's fault history, newest first. All connectors, or just one."""
        clauses = [cls.charge_point_identity == charge_point_identity]
        if connector_id is not None:
            clauses.append(cls.connector_id == connector_id)
        return await cls.find(*clauses).sort(-cls.entered_at).to_list()


class FirmwareUpdate(Document):
    """One UpdateFirmware request and its progress (OCPP 1.6 s5.16, s4.13's
    FirmwareStatusNotification).

    `location` is where this Central System told the charger to fetch the image from -- see
    main.py's handle_firmware_request, which is what actually serves it from this project's own
    `firmware_files/` directory. UpdateFirmware.conf carries no accept/reject status (the
    charger always attempts it), so a record is created unconditionally once the request is
    sent; `status`/`history` are filled in later, as FirmwareStatusNotification messages arrive.
    A successful install reboots the charger (s4.2.1's commissioning flow runs again), which
    every boot in this project already handles unconditionally -- nothing extra is needed here
    for that.
    """

    charge_point_identity: str
    location: str
    retrieve_date: datetime
    retries: int | None = None
    retry_interval: int | None = None
    status: FirmwareStatus = FirmwareStatus.idle
    requested_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    history: list[dict] = Field(default_factory=list)

    class Settings:
        name = "firmware_updates"
        indexes = [
            IndexModel(
                [("charge_point_identity", 1), ("requested_at", -1)],
                name="charge_point_requested_at",
            )
        ]

    @classmethod
    async def create(
        cls, charge_point_identity, location, retrieve_date, retries=None, retry_interval=None
    ):
        record = cls(
            charge_point_identity=charge_point_identity,
            location=location,
            retrieve_date=retrieve_date,
            retries=retries,
            retry_interval=retry_interval,
        )
        await record.insert()
        return record

    @classmethod
    async def record_status(cls, charge_point_identity, status):
        """Apply a FirmwareStatusNotification to this charger's most recently requested update.

        The message carries no correlation id of its own, so "most recently requested" is the
        only reasonable match when more than one update has ever been sent to this charger --
        the same reasoning DiagnosticsRequest.record_status below uses.
        """
        await get_database()["firmware_updates"].find_one_and_update(
            {"charge_point_identity": charge_point_identity},
            {
                "$set": {"status": status, "updated_at": _utcnow()},
                "$push": {"history": {"status": status, "at": _utcnow()}},
            },
            sort=[("requested_at", -1)],
        )

    @classmethod
    async def latest_for(cls, charge_point_identity):
        """The most recently requested firmware update for this charger, or None."""
        return await cls.find(cls.charge_point_identity == charge_point_identity).sort(
            -cls.requested_at
        ).first_or_none()


class DiagnosticsRequest(Document):
    """One GetDiagnostics request and its progress (OCPP 1.6 s5.1, s4.9's
    DiagnosticsStatusNotification).

    Created only once the charger's conf actually names a `file_name` -- an absent one means it
    declined (nothing to upload), so there would be nothing meaningful to track.
    """

    charge_point_identity: str
    location: str
    file_name: str
    status: DiagnosticsStatus = DiagnosticsStatus.idle
    requested_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    history: list[dict] = Field(default_factory=list)

    class Settings:
        name = "diagnostics_requests"
        indexes = [
            IndexModel(
                [("charge_point_identity", 1), ("requested_at", -1)],
                name="charge_point_requested_at",
            )
        ]

    @classmethod
    async def create(cls, charge_point_identity, location, file_name):
        record = cls(
            charge_point_identity=charge_point_identity, location=location, file_name=file_name
        )
        await record.insert()
        return record

    @classmethod
    async def record_status(cls, charge_point_identity, status):
        """Apply a DiagnosticsStatusNotification to this charger's most recent request."""
        await get_database()["diagnostics_requests"].find_one_and_update(
            {"charge_point_identity": charge_point_identity},
            {
                "$set": {"status": status, "updated_at": _utcnow()},
                "$push": {"history": {"status": status, "at": _utcnow()}},
            },
            sort=[("requested_at", -1)],
        )

    @classmethod
    async def latest_for(cls, charge_point_identity):
        """The most recently requested diagnostics upload for this charger, or None."""
        return await cls.find(cls.charge_point_identity == charge_point_identity).sort(
            -cls.requested_at
        ).first_or_none()


class InstalledChargingProfile(Document):
    """A charging profile this Central System has successfully installed on a charger
    (SetChargingProfile answered Accepted; OCPP 1.6 s3.13, s5.16) -- the operator's record of
    what limits are in force there, since nothing else remembers it once the request is gone.

    Written only on Accepted, like Reservation: the charger is the authority on what it actually
    holds, so Rejected/NotSupported leave no record. `profile` is the whole ChargingProfile as
    sent (snake_case, as the ocpp library carries it). Replacement follows s3.13.2 exactly as a
    charger applies it -- same chargingProfileId, or same stackLevel and purpose on the same
    connector -- see install(). The charger's *composite* schedule (what actually applies right
    now, after merging all of these with its own local limits) is deliberately not derived here:
    GetCompositeSchedule asks the charger for it.
    """

    charge_point_identity: str
    connector_id: int
    charging_profile_id: int
    stack_level: int
    purpose: ChargingProfilePurposeType
    transaction_id: int | None = None
    profile: dict
    installed_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "charging_profiles"
        indexes = [
            IndexModel(
                [("charge_point_identity", 1), ("charging_profile_id", 1)],
                unique=True,
                name="charge_point_profile_id_unique",
            ),
            IndexModel(
                [
                    ("charge_point_identity", 1), ("connector_id", 1), ("purpose", 1),
                    ("stack_level", 1),
                ],
                unique=True,
                name="charge_point_connector_purpose_stack_unique",
            ),
        ]

    @classmethod
    async def install(cls, charge_point_identity, connector_id, profile):
        """Record `profile` (a charging_profiles.ChargingProfile) as installed, replacing
        whatever it conflicts with (s3.13.2, s5.16.3). Returns the stored record."""
        await get_database()["charging_profiles"].delete_many(
            {
                "charge_point_identity": charge_point_identity,
                "$or": [
                    {"charging_profile_id": profile.charging_profile_id},
                    {
                        "connector_id": connector_id,
                        "purpose": profile.purpose.value,
                        "stack_level": profile.stack_level,
                    },
                ],
            }
        )
        record = cls(
            charge_point_identity=charge_point_identity,
            connector_id=connector_id,
            charging_profile_id=profile.charging_profile_id,
            stack_level=profile.stack_level,
            purpose=profile.purpose,
            transaction_id=profile.transaction_id,
            profile=profile.to_wire(),
        )
        await record.insert()
        return record

    @classmethod
    async def clear(
        cls, charge_point_identity, profile_id=None, connector_id=None, purpose=None,
        stack_level=None,
    ):
        """Forget every record matching *all* the criteria given (none = every profile on this
        charger), the same reading ClearChargingProfile.req gets on the charger side. Returns how
        many were removed."""
        query = {"charge_point_identity": charge_point_identity}
        if profile_id is not None:
            query["charging_profile_id"] = profile_id
        if connector_id is not None:
            query["connector_id"] = connector_id
        if purpose is not None:
            query["purpose"] = ChargingProfilePurposeType(purpose).value
        if stack_level is not None:
            query["stack_level"] = stack_level
        result = await get_database()["charging_profiles"].delete_many(query)
        return result.deleted_count

    @classmethod
    async def drop_transaction_profiles(
        cls, charge_point_identity, connector_id=None, transaction_id=None
    ):
        """Forget the TxProfile(s) of a transaction that has just ended: s3.13.1, "After the
        transaction is stopped, the profile SHOULD be deleted", and s7.10 has one "cease to be
        valid when the transaction terminates". Matches by transaction_id, or by connector when
        it is a real one (an orphaned StopTransaction has neither a known connector nor, for a
        transaction never started here, any record to drop). Returns how many were removed."""
        matches = []
        if transaction_id is not None:
            matches.append({"transaction_id": transaction_id})
        if connector_id:
            matches.append({"connector_id": connector_id})
        if not matches:
            return 0
        result = await get_database()["charging_profiles"].delete_many(
            {
                "charge_point_identity": charge_point_identity,
                "purpose": TX.value,
                "$or": matches,
            }
        )
        return result.deleted_count

    @classmethod
    async def for_charge_point(cls, charge_point_identity, connector_id=None):
        """This charger's recorded profiles, ordered by connector, purpose, then stack level."""
        clauses = [cls.charge_point_identity == charge_point_identity]
        if connector_id is not None:
            clauses.append(cls.connector_id == connector_id)
        records = await cls.find(*clauses).to_list()
        return sorted(records, key=lambda r: (r.connector_id, r.purpose.value, r.stack_level))


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


async def next_reservation_id():
    """Atomically allocate the next reservation_id.

    Unlike transaction_id (allocated by the CS but only ever reported back TO it, in
    StartTransaction.conf), reservation_id is generated by the CS and sent OUT in
    ReserveNow.req itself -- but it needs the same guarantee: unique across the Central System,
    so the same atomic-counter approach applies.
    """
    result = await get_database()["counters"].find_one_and_update(
        {"_id": "reservation_id"},
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
        document_models=[
            ChargePoint, IdTag, Transaction, ConnectorStatus, ConfigurationEntry, Reservation,
            LocalListState, FaultEvent, FirmwareUpdate, DiagnosticsRequest, Site,
            InstalledChargingProfile,
        ],
    )
    return client
