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
import time
from datetime import UTC, datetime
from urllib.parse import quote

import websockets
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
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearCacheStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    GetCompositeScheduleStatus,
    Measurand,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    ReservationStatus,
    MessageTrigger,
    ResetStatus,
    ResetType,
    UnitOfMeasure,
    TriggerMessageStatus,
    UnlockStatus,
    UpdateStatus,
    UpdateType,
)
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from charging_profiles import TX, ChargingProfileSet, installation_problem, parse_profile
from commissioning import AUTHORIZATION_KEY_CONFIG_KEY
from connector_state_machine import ConnectorState, IllegalTransition
from ocpp_client_auth import basic_auth_header

# Messages a TriggerMessage.req may ask this simulator to send (s5.16), and the subset that
# takes a connectorId.
TRIGGERABLE_MESSAGES = frozenset(m.value for m in MessageTrigger) - {
    MessageTrigger.log_status_notification.value,
    MessageTrigger.sign_charge_point_certificate.value,
}
CONNECTOR_TRIGGERS = frozenset({"MeterValues", "StatusNotification"})

# Configuration keys this simulator reports RebootRequired for instead of Accepted, to exercise
# that branch (OCPP 1.6 s5.6) -- a real charger's own choice per key, not something the spec
# fixes, so this project picks one plausible example rather than modelling every real key.
REBOOT_REQUIRED_CONFIGURATION_KEYS = frozenset({"HeartbeatInterval"})

# Connector statuses ReserveNow answers Occupied for (OCPP 1.6 s5.15): mid-session, or already
# reserved by someone else. Available and Faulted are handled separately (A7 accepts; Faulted's
# own status name doubles as the ReservationStatus answer); Unavailable likewise.
RESERVE_NOW_OCCUPIED_STATUSES = frozenset({
    ChargePointStatus.preparing,
    ChargePointStatus.charging,
    ChargePointStatus.suspended_ev,
    ChargePointStatus.suspended_evse,
    ChargePointStatus.finishing,
    ChargePointStatus.reserved,
})

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
        reject_change_availability=False,
        reject_reset=False,
        reject_trigger_message=False,
        reject_charging_profile=False,
        no_smart_charging=False,
        reject_unlock=False,
        reject_change_configuration=False,
        reject_clear_cache=False,
        reject_reserve_now=False,
        reject_send_local_list=False,
        fail_firmware_download=False,
        decline_diagnostics=False,
        fail_diagnostics_upload=False,
        default_connector_id=1,
        boot_vendor="PocVendor",
        boot_model="PocModel",
        number_of_connectors=1,
        **kwargs,
    ):
        super().__init__(id, connection, **kwargs)
        self.number_of_connectors = number_of_connectors
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
        self.reject_change_availability = reject_change_availability
        self.reject_reset = reject_reset
        self.reject_trigger_message = reject_trigger_message
        self.reject_charging_profile = reject_charging_profile
        self.no_smart_charging = no_smart_charging
        # OCPP 1.6 s3.13: the profiles installed here, what a Central System's SetChargingProfile
        # puts in place; and the bookkeeping to act on them. transaction_started_at anchors
        # Relative profiles (which count from the start of the transaction).
        self.charging_profiles = ChargingProfileSet()
        self.transaction_started_at = {}
        # Connectors this charger itself moved to SuspendedEVSE because a profile cut their limit
        # to zero -- only those are resumed when the limit lifts (s4.9's C5 and E3).
        self.suspended_by_profile = set()
        self._limit_watch = None
        # What a triggered BootNotification (s5.16) re-sends, same as the very first boot.
        self.boot_vendor = boot_vendor
        self.boot_model = boot_model
        # Last status sent by each *_status_notification: a TriggerMessage for one of them
        # re-reports this, and before any update/upload has happened that is Idle.
        self.last_firmware_status = FirmwareStatus.idle
        self.last_diagnostics_status = DiagnosticsStatus.idle
        # connector_id -> last meter reading (Wh) this simulator knows of, for a triggered
        # MeterValues.
        self.meter_readings = {}
        self.reject_unlock = reject_unlock
        self.reject_change_configuration = reject_change_configuration
        self.reject_clear_cache = reject_clear_cache
        self.reject_reserve_now = reject_reserve_now
        self.default_connector_id = default_connector_id
        # reservation_id -> connector_id, or -> None for a connectorId=0 (any connector)
        # reservation that never moved a specific connector to Reserved (OCPP 1.6 s5.15).
        self.reservations = {}
        # key -> (value, readonly). OCPP 1.6 s5.9's GetConfiguration/s5.6's ChangeConfiguration
        # read and write this. "AuthorizationKey" is deliberately readonly with no value here --
        # OCPP-J 1.6 s6.2.2: a charger "should not give back the authorization key in response
        # to a GetConfiguration request" -- it is changed only through on_change_configuration's
        # own dedicated branch below, never through this generic dict.
        self.configuration = {
            AUTHORIZATION_KEY_CONFIG_KEY: (None, True),
            "HeartbeatInterval": ("30", False),
            "NumberOfConnectors": (str(number_of_connectors), True),
            # OCPP 1.6 s3.5/s3.6: govern stand-alone operation. This simulator keeps a real
            # Authorization Cache and Local Authorization List either way (see
            # authorization_cache/local_list below) and honours LocalPreAuthorize (see
            # after_remote_start_transaction); it does not simulate an actual network outage,
            # so LocalAuthorizeOffline has nothing to switch on here -- see
            # instructions/06a-remaining-flows-progress.md for why that is this project's
            # stated scope boundary for this key.
            "LocalAuthorizeOffline": ("true", False),
            "LocalPreAuthorize": ("false", False),
            # OCPP 1.6 s3.13.5: a Smart Charging charger SHALL report these four, so a Central
            # System can find out what it will accept before sending a profile it would refuse.
            "ChargeProfileMaxStackLevel": ("5", True),
            "ChargingScheduleAllowedChargingRateUnit": ("Current,Power", True),
            "ChargingScheduleMaxPeriods": ("10", True),
            "MaxChargingProfilesInstalled": ("10", True),
        }
        # id_tag -> IdTagInfo dict, refreshed from every Authorize/StartTransaction/
        # StopTransaction conf (OCPP 1.6 s3.5.1); emptied by ClearCache.
        self.authorization_cache = {}
        # id_tag -> IdTagInfo dict, replaced/merged by SendLocalList (OCPP 1.6 s3.6/s5.8).
        self.local_list = {}
        self.local_list_version = 0
        self.reject_send_local_list = reject_send_local_list
        self.fail_firmware_download = fail_firmware_download
        self.decline_diagnostics = decline_diagnostics
        self.fail_diagnostics_upload = fail_diagnostics_upload
        # Set by after_reset or after_update_firmware once the reboot-triggering conf/exchange
        # has actually completed; main() reads this after the connection closes to decide
        # whether to reconnect and re-run BootNotification, standing in for a real charger's own
        # reboot (OCPP 1.6 s4.2.1). Holds a short reason string ("Soft"/"Hard"/"Firmware") for
        # the reconnect log line, not just a bool.
        self.reboot_reason = None
        # connector_id -> transaction_id, for the connector a RemoteStopTransaction names by
        # transaction_id alone (OCPP does not carry a connector in that message).
        self.open_transactions = {}
        # A local mirror of each connector's status, kept in sync by send_status() every time
        # this simulator actually sends a StatusNotification. This is what lets
        # ChangeAvailability answer Scheduled correctly (OCPP 1.6 s5.2): it needs to know
        # whether the connector could legally reach Unavailable/Available right now, same as
        # the Central System checks in main.py's on_status.
        self.connector_states = {}
        # Connector ids this charger has actually reported on (never includes 0, the main
        # controller), so a ChangeAvailability(connectorId=0) -- "the Charge Point and all
        # Connectors" (s5.2) -- knows which connectors that "all" covers.
        self.known_connectors = set()
        # connector_id -> time.monotonic() when its reported status last changed, so a caller
        # can tell how long a connector has been in its current state (the demo fleet's
        # unplug-after-Finishing timer).
        self.status_since = {}
        # connector_id -> AvailabilityType, for an Inoperative request answered Scheduled
        # because a transaction was in progress: applied once that transaction ends (s5.2).
        self.deferred_availability = {}
        # id_tag -> connector_id chosen by on_remote_start_transaction, so the @after half
        # starts on the same connector, and so busy_connectors() holds it until then.
        self.pending_remote_starts = {}
        # Connectors in the middle of start_local_session, between Authorize and the
        # StartTransaction.conf, when they are not yet in open_transactions.
        self.pending_local_starts = set()

    @on(Action.change_configuration)
    def on_change_configuration(self, key, value, **kwargs):
        if key == AUTHORIZATION_KEY_CONFIG_KEY:
            try:
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

        if self.reject_change_configuration:
            print(f"CHANGE CONFIGURATION: rejecting {key!r} (--reject-change-configuration)")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        if key not in self.configuration:
            print(f"CHANGE CONFIGURATION: unsupported key {key!r}")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)
        _old_value, readonly = self.configuration[key]
        if readonly:
            print(f"CHANGE CONFIGURATION: {key!r} is read-only")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        self.configuration[key] = (value, readonly)
        if key in REBOOT_REQUIRED_CONFIGURATION_KEYS:
            print(f"CHANGE CONFIGURATION: {key}={value!r} (takes effect after a reboot)")
            return call_result.ChangeConfiguration(status=ConfigurationStatus.reboot_required)
        print(f"CHANGE CONFIGURATION: {key}={value!r}")
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.get_configuration)
    def on_get_configuration(self, key=None, **kwargs):
        """Report configuration keys (OCPP 1.6 s5.9). Omitting `key` reports every key known."""
        requested = key if key else list(self.configuration)
        configuration_key = []
        unknown_key = []
        for name in requested:
            if name not in self.configuration:
                unknown_key.append(name)
                continue
            value, readonly = self.configuration[name]
            configuration_key.append(datatypes.KeyValue(key=name, readonly=readonly, value=value))
        print(f"GET CONFIGURATION: reporting {len(configuration_key)}, unknown {unknown_key}")
        return call_result.GetConfiguration(
            configuration_key=configuration_key, unknown_key=unknown_key
        )

    @on(Action.clear_cache)
    def on_clear_cache(self, **kwargs):
        if self.reject_clear_cache:
            print("CLEAR CACHE: rejecting (--reject-clear-cache)")
            return call_result.ClearCache(status=ClearCacheStatus.rejected)
        cleared = len(self.authorization_cache)
        self.authorization_cache.clear()
        print(f"CLEAR CACHE: accepted, cleared {cleared} cached idTag(s)")
        return call_result.ClearCache(status=ClearCacheStatus.accepted)

    async def authorize(self, id_tag):
        """Send Authorize.req and update the Authorization Cache from its IdTagInfo (s3.5.1)."""
        response = await self.call(call.Authorize(id_tag=id_tag), suppress=False)
        self._cache_id_tag_info(id_tag, response.id_tag_info)
        return response

    def _cache_id_tag_info(self, id_tag, id_tag_info):
        """Authorization Cache update (OCPP 1.6 s3.5.1): refreshed from every IdTagInfo this
        simulator receives in Authorize.conf, StartTransaction.conf and StopTransaction.conf --
        "all the latest received identifiers (i.e. valid and NOT-valid)", so a Rejected/Invalid
        answer is cached too, not just an Accepted one.
        """
        if id_tag_info is not None:
            self.authorization_cache[id_tag] = dict(id_tag_info)

    def local_authorize(self, id_tag):
        """Resolve id_tag from this charger's own stand-alone-operation records (OCPP 1.6
        s3.5/s3.5.1/s3.6): the Local Authorization List first, then the Authorization Cache.
        Returns the IdTagInfo dict found, or None if this charger has no local record at all.
        """
        if id_tag in self.local_list:
            return self.local_list[id_tag]
        return self.authorization_cache.get(id_tag)

    @on(Action.get_local_list_version)
    def on_get_local_list_version(self, **kwargs):
        print(f"GET LOCAL LIST VERSION: {self.local_list_version}")
        return call_result.GetLocalListVersion(list_version=self.local_list_version)

    @on(Action.send_local_list)
    def on_send_local_list(
        self, list_version, update_type, local_authorization_list=None, **kwargs
    ):
        if self.reject_send_local_list:
            print("SEND LOCAL LIST: rejecting (--reject-send-local-list)")
            return call_result.SendLocalList(status=UpdateStatus.failed)
        if list_version <= self.local_list_version:
            print(
                f"SEND LOCAL LIST: version {list_version} <= current "
                f"{self.local_list_version}; mismatch"
            )
            return call_result.SendLocalList(status=UpdateStatus.version_mismatch)
        entries = local_authorization_list or []
        if UpdateType(update_type) == UpdateType.full:
            self.local_list = {entry["id_tag"]: entry.get("id_tag_info") for entry in entries}
        else:
            for entry in entries:
                id_tag = entry["id_tag"]
                id_tag_info = entry.get("id_tag_info")
                if id_tag_info is None:
                    self.local_list.pop(id_tag, None)
                else:
                    self.local_list[id_tag] = id_tag_info
        self.local_list_version = list_version
        print(
            f"SEND LOCAL LIST: now at version {list_version}, {len(self.local_list)} entries"
        )
        return call_result.SendLocalList(status=UpdateStatus.accepted)

    def get_connector_state(self, connector_id):
        """This connector's local ConnectorState, creating a fresh Available one on first use."""
        state = self.connector_states.get(connector_id)
        if state is None:
            state = ConnectorState(connector_id)
            self.connector_states[connector_id] = state
        return state

    async def send_status(
        self, connector_id, status, error_code=ChargePointErrorCode.no_error, info=None
    ):
        """Send StatusNotification and keep get_connector_state's mirror in sync.

        Every status this simulator reports goes through here, rather than a bare
        call.StatusNotification, so ChangeAvailability's legality/transaction checks are always
        working from what was actually sent, exactly as main.py's on_status does on the Central
        System side.

        `info` is StatusNotification's free-text field, a CiString50 in OCPP 1.6 s6.47. None is
        left out of the payload entirely: the ocpp library strips None fields before sending.
        """
        if info is not None and len(info) > 50:
            raise ValueError(f"StatusNotification info is at most 50 characters: {info!r}")
        if connector_id != 0:
            self.known_connectors.add(connector_id)
        state = self.get_connector_state(connector_id)
        if state.status != status or connector_id not in self.status_since:
            self.status_since[connector_id] = time.monotonic()
        try:
            state.change_to(status)
        except IllegalTransition:
            # Mirrors main.py's on_status: record what this simulator is actually about to
            # report rather than silently refuse to send it.
            state.force_status(status)
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=error_code,
                status=status,
                info=info,
                timestamp=now(),
            ),
            suppress=False,
        )

    async def send_boot_statuses(self, faulted=frozenset()):
        """Report every physical connector after an Accepted boot, in ascending order:
        Available, or Faulted for the ids in `faulted`. A real charger reports each of its
        connectors after booting, which is what lets a map show all of them.

        A connector already reported on this connection is skipped. The Central System re-sends
        a persisted ChangeAvailability(Inoperative) straight after the boot conf (main.py's
        reapply_persisted_availability), and that Unavailable must not be overwritten by a late
        Available from here. send_status marks a connector known before its first await, so
        this check cannot race with it.
        """
        for connector_id in sorted(self.physical_connectors()):
            if connector_id in self.known_connectors:
                continue
            if connector_id in faulted:
                await self.send_status(
                    connector_id,
                    ChargePointStatus.faulted,
                    error_code=ChargePointErrorCode.other_error,
                    info="Out of order (PlugShare report)",
                )
            else:
                await self.send_status(connector_id, ChargePointStatus.available)

    async def simulate_fault_and_recover(self, connector_id, error_code, fault_duration_seconds=2):
        """Report a fault, then recover from it (OCPP 1.6 s4.9's A9/.../H9 -> Faulted, then
        I1-I8 back out). There is no OCPP message for a Central System to induce a fault --
        real hardware detects its own -- so this is this simulator's own way of exercising that
        half of the flow for a manual demo or an idle-mode session (see --simulate-fault).
        """
        await self.send_status(connector_id, ChargePointStatus.faulted, error_code=error_code)
        print(f"FAULT: connector {connector_id} reporting {error_code.value}")
        await asyncio.sleep(fault_duration_seconds)
        await self.recover_from_fault(connector_id)

    async def recover_from_fault(self, connector_id, error_code=ChargePointErrorCode.no_error):
        """Report recovery from a fault, reporting exactly the pre-fault status -- never a
        guessed target -- by asking ConnectorState.recover_from_fault() for it (OCPP 1.6 s4.9's
        I1-I8: "Fault is resolved and status returns to the pre-fault state").
        """
        state = self.get_connector_state(connector_id)
        target = state.pre_fault_status
        state.recover_from_fault()  # raises if not currently Faulted, or nothing was recorded
        self.status_since[connector_id] = time.monotonic()
        if connector_id != 0:
            self.known_connectors.add(connector_id)
        await self.call(
            call.StatusNotification(
                connector_id=connector_id, error_code=error_code, status=target, timestamp=now()
            ),
            suppress=False,
        )
        print(f"FAULT: connector {connector_id} recovered to {target.value}")

    def _availability_targets(self, connector_id):
        """Which connector ids a ChangeAvailability.req actually addresses (OCPP 1.6 s5.2)."""
        return ({0} | self.known_connectors) if connector_id == 0 else {connector_id}

    def _availability_blocked(self, connector_id, avail_type):
        """True when connector_id cannot take avail_type immediately.

        Two reasons, both from OCPP 1.6 s5.2/s4.9: a transaction is in progress on it (the
        change must wait for that transaction to finish, not cut it short), or its current
        status has no direct transition to the target at all -- e.g. Preparing has no listed
        transition to Unavailable, unlike Available/Charging/SuspendedEV/SuspendedEVSE/
        Finishing/Reserved, which all do.
        """
        if avail_type == AvailabilityType.inoperative and connector_id in self.open_transactions:
            return True
        target_status = (
            ChargePointStatus.unavailable
            if avail_type == AvailabilityType.inoperative
            else ChargePointStatus.available
        )
        state = self.get_connector_state(connector_id)
        return state.status != target_status and not state.can_change_to(target_status)

    async def _apply_availability(self, connector_id, avail_type):
        """Actually move a connector to the availability it was asked for, and report it."""
        self.deferred_availability.pop(connector_id, None)
        target_status = (
            ChargePointStatus.unavailable
            if avail_type == AvailabilityType.inoperative
            else ChargePointStatus.available
        )
        if self.get_connector_state(connector_id).status == target_status:
            return
        await self.send_status(connector_id, target_status)

    @on(Action.change_availability)
    def on_change_availability(self, connector_id, type, **kwargs):
        if self.reject_change_availability:
            print(f"CHANGE AVAILABILITY: rejecting connector {connector_id} -> {type}")
            return call_result.ChangeAvailability(status=AvailabilityStatus.rejected)
        avail_type = AvailabilityType(type)
        targets = self._availability_targets(connector_id)
        scheduled = any(self._availability_blocked(c, avail_type) for c in targets)
        status = AvailabilityStatus.scheduled if scheduled else AvailabilityStatus.accepted
        print(f"CHANGE AVAILABILITY: connector {connector_id} -> {type} ({status})")
        return call_result.ChangeAvailability(status=status)

    @after(Action.change_availability)
    async def after_change_availability(self, connector_id, type, **kwargs):
        """Actually apply the change (or defer it) once ChangeAvailability.conf has been sent."""
        if self.reject_change_availability:
            return
        avail_type = AvailabilityType(type)
        for target in self._availability_targets(connector_id):
            if self._availability_blocked(target, avail_type):
                self.deferred_availability[target] = avail_type
                print(f"CHANGE AVAILABILITY: connector {target} deferred until transaction ends")
            else:
                await self._apply_availability(target, avail_type)

    @on(Action.reserve_now)
    def on_reserve_now(self, connector_id, expiry_date, id_tag, reservation_id, **kwargs):
        if self.reject_reserve_now:
            print(f"RESERVE NOW: rejecting reservation {reservation_id} (--reject-reserve-now)")
            return call_result.ReserveNow(status=ReservationStatus.rejected)
        if connector_id == 0:
            # OCPP 1.6 s5.15: connectorId 0 means "not for a specific connector" -- honoured
            # wherever id_tag is first presented, so nothing here becomes Reserved yet.
            self.reservations[reservation_id] = None
            print(f"RESERVE NOW: reservation {reservation_id} accepted for any connector")
            return call_result.ReserveNow(status=ReservationStatus.accepted)
        state = self.get_connector_state(connector_id)
        if state.status in RESERVE_NOW_OCCUPIED_STATUSES:
            print(f"RESERVE NOW: connector {connector_id} is Occupied; rejecting")
            return call_result.ReserveNow(status=ReservationStatus.occupied)
        if state.status == ChargePointStatus.faulted:
            print(f"RESERVE NOW: connector {connector_id} is Faulted; rejecting")
            return call_result.ReserveNow(status=ReservationStatus.faulted)
        if state.status == ChargePointStatus.unavailable:
            print(f"RESERVE NOW: connector {connector_id} is Unavailable; rejecting")
            return call_result.ReserveNow(status=ReservationStatus.unavailable)
        self.reservations[reservation_id] = connector_id
        print(f"RESERVE NOW: reservation {reservation_id} accepted for connector {connector_id}")
        return call_result.ReserveNow(status=ReservationStatus.accepted)

    @after(Action.reserve_now)
    async def after_reserve_now(self, connector_id, expiry_date, id_tag, reservation_id, **kwargs):
        """Move the connector to Reserved (A7) once ReserveNow.conf has actually been sent."""
        if self.reject_reserve_now or self.reservations.get(reservation_id) is None:
            return
        await self.send_status(self.reservations[reservation_id], ChargePointStatus.reserved)

    @on(Action.cancel_reservation)
    def on_cancel_reservation(self, reservation_id, **kwargs):
        if reservation_id not in self.reservations:
            print(f"CANCEL RESERVATION: unknown reservation_id={reservation_id}; rejecting")
            return call_result.CancelReservation(status=CancelReservationStatus.rejected)
        print(f"CANCEL RESERVATION: {reservation_id} accepted")
        return call_result.CancelReservation(status=CancelReservationStatus.accepted)

    @after(Action.cancel_reservation)
    async def after_cancel_reservation(self, reservation_id, **kwargs):
        """Move the connector back to Available (G1) once CancelReservation.conf has been sent."""
        connector_id = self.reservations.pop(reservation_id, None)
        if connector_id is None:
            return
        if self.get_connector_state(connector_id).status == ChargePointStatus.reserved:
            await self.send_status(connector_id, ChargePointStatus.available)

    @on(Action.unlock_connector)
    def on_unlock_connector(self, connector_id, **kwargs):
        if self.reject_unlock:
            print(f"UNLOCK CONNECTOR: connector {connector_id} unlock failed (--reject-unlock)")
            return call_result.UnlockConnector(status=UnlockStatus.unlock_failed)
        print(f"UNLOCK CONNECTOR: connector {connector_id} unlocked")
        return call_result.UnlockConnector(status=UnlockStatus.unlocked)

    # --- Smart charging (OCPP 1.6 s3.13, s5.5, s5.7, s5.16) ---------------------------------

    def _smart_charging_limits(self):
        cfg = self.configuration
        units = {"Current": ChargingRateUnitType.amps, "Power": ChargingRateUnitType.watts}
        allowed = cfg["ChargingScheduleAllowedChargingRateUnit"][0].split(",")
        return {
            "max_stack_level": int(cfg["ChargeProfileMaxStackLevel"][0]),
            "max_periods": int(cfg["ChargingScheduleMaxPeriods"][0]),
            "max_installed": int(cfg["MaxChargingProfilesInstalled"][0]),
            "allowed_units": {units[name] for name in allowed},
        }

    def _profile_problem(self, profile, connector_id, transaction_pending=False):
        """Why this charger refuses `profile` on `connector_id`, or None if it will take it."""
        return installation_problem(
            profile,
            connector_id,
            known_connectors=self.physical_connectors(),
            open_transaction_id=self.open_transactions.get(connector_id),
            transaction_pending=transaction_pending,
            installed_after=self.charging_profiles.count_after_install(connector_id, profile),
            **self._smart_charging_limits(),
        )

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, connector_id, cs_charging_profiles, **kwargs):
        if self.no_smart_charging:
            print("SET CHARGING PROFILE: not supported (--no-smart-charging)")
            return call_result.SetChargingProfile(status=ChargingProfileStatus.not_supported)
        if self.reject_charging_profile:
            print("SET CHARGING PROFILE: rejecting (--reject-charging-profile)")
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        try:
            profile = parse_profile(cs_charging_profiles)
        except ValueError as exc:
            print(f"SET CHARGING PROFILE: rejecting, {exc}")
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        problem = self._profile_problem(profile, connector_id)
        if problem:
            print(f"SET CHARGING PROFILE: rejecting, {problem}")
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        replaced = self.charging_profiles.install(connector_id, profile, datetime.now(UTC))
        print(
            f"SET CHARGING PROFILE: installed profile {profile.charging_profile_id} "
            f"({profile.purpose.value}, stack {profile.stack_level}) on connector {connector_id}"
            + (f", replacing {len(replaced)}" if replaced else "")
        )
        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    @after(Action.set_charging_profile)
    async def after_set_charging_profile(self, connector_id, cs_charging_profiles, **kwargs):
        """Re-evaluate once the conf is out (s5.16.3: "The Charge Point SHALL then re-evaluate its
        collection of charge profiles to determine which charging profile will become active")."""
        if self.no_smart_charging or self.reject_charging_profile:
            return
        await self.refresh_charging_limits()

    @on(Action.clear_charging_profile)
    def on_clear_charging_profile(self, **kwargs):
        criteria = {
            "profile_id": kwargs.get("id"),
            "connector_id": kwargs.get("connector_id"),
            "purpose": kwargs.get("charging_profile_purpose"),
            "stack_level": kwargs.get("stack_level"),
        }
        removed = 0 if self.no_smart_charging else self.charging_profiles.clear(**criteria)
        print(f"CLEAR CHARGING PROFILE: {criteria} removed {removed}")
        status = (
            ClearChargingProfileStatus.accepted if removed else ClearChargingProfileStatus.unknown
        )
        return call_result.ClearChargingProfile(status=status)

    @after(Action.clear_charging_profile)
    async def after_clear_charging_profile(self, **kwargs):
        if not self.no_smart_charging:
            await self.refresh_charging_limits()

    @on(Action.get_composite_schedule)
    def on_get_composite_schedule(self, connector_id, duration, charging_rate_unit=None, **kwargs):
        rejected = call_result.GetCompositeSchedule(status=GetCompositeScheduleStatus.rejected)
        if self.no_smart_charging:
            return rejected
        # s5.7: "If the Charge Point is not able to report the requested schedule, for instance
        # if the connectorId is unknown, it SHALL respond with a status Rejected."
        if connector_id != 0 and connector_id not in self.physical_connectors():
            print(f"GET COMPOSITE SCHEDULE: unknown connector {connector_id}; rejecting")
            return rejected
        start = datetime.now(UTC)
        try:
            schedule = self.charging_profiles.composite(
                connector_id,
                start,
                duration,
                ChargingRateUnitType(charging_rate_unit or ChargingRateUnitType.amps),
                tx_starts=self.transaction_started_at,
                connectors=sorted(self.physical_connectors()),
            )
        except ValueError as exc:
            print(f"GET COMPOSITE SCHEDULE: rejecting, {exc}")
            return rejected
        return call_result.GetCompositeSchedule(
            status=GetCompositeScheduleStatus.accepted,
            connector_id=connector_id,
            schedule_start=start.isoformat(),
            charging_schedule=schedule.to_wire(),
        )

    async def apply_charging_limits(self):
        """Act on the limits in force right now: a transaction whose limit has dropped to zero is
        suspended by the charger (Charging -> SuspendedEVSE, s4.9's C5), and one it suspended
        itself resumes when the limit lifts (SuspendedEVSE -> Charging, E3). Any other limit only
        changes how much power flows, which this simulator does not model."""
        moment = datetime.now(UTC)
        connectors = sorted(self.physical_connectors())
        for connector_id in list(self.open_transactions):
            limit = self.charging_profiles.limit_at(
                connector_id, moment, self.transaction_started_at, connectors
            )
            status = self.get_connector_state(connector_id).status
            if limit.watts <= 0 and status == ChargePointStatus.charging:
                print(f"SMART CHARGING: limit is 0 on connector {connector_id}; suspending")
                self.suspended_by_profile.add(connector_id)
                await self.send_status(connector_id, ChargePointStatus.suspended_evse)
            elif limit.watts > 0 and connector_id in self.suspended_by_profile:
                self.suspended_by_profile.discard(connector_id)
                if status == ChargePointStatus.suspended_evse:
                    print(f"SMART CHARGING: limit lifted on connector {connector_id}; resuming")
                    await self.send_status(connector_id, ChargePointStatus.charging)

    async def refresh_charging_limits(self):
        """Apply the limits now, then (re)start the watcher that applies them again at the next
        moment any schedule changes -- so a profile with time-based periods takes effect on
        schedule without anyone sending anything."""
        await self.apply_charging_limits()
        self.cancel_limit_watch()
        self._limit_watch = asyncio.create_task(self._watch_limits())

    def cancel_limit_watch(self):
        if self._limit_watch is not None:
            self._limit_watch.cancel()
            self._limit_watch = None

    async def _watch_limits(self):
        try:
            while True:
                moment = datetime.now(UTC)
                upcoming = self.charging_profiles.next_change_after(
                    moment, self.transaction_started_at, sorted(self.physical_connectors())
                )
                if upcoming is None:
                    return
                # A small overshoot so the change has definitely happened by the time we look;
                # capped so a far-off change is still re-checked now and then.
                delay = (upcoming - moment).total_seconds() + 0.05
                await asyncio.sleep(min(delay, 3600))
                await self.apply_charging_limits()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the connection may have closed under us
            print(f"SMART CHARGING: limit watcher stopped: {exc!r}")

    @on(Action.trigger_message)
    def on_trigger_message(self, requested_message, connector_id=None, **kwargs):
        """OCPP 1.6 s5.16: answer first, send the requested message afterwards (after_trigger_
        message). Rejected for a connector this charger has never reported on; NotImplemented
        for a message this simulator does not know how to send."""
        if self.reject_trigger_message:
            print(f"TRIGGER MESSAGE: rejecting {requested_message} (--reject-trigger-message)")
            return call_result.TriggerMessage(status=TriggerMessageStatus.rejected)
        if requested_message not in TRIGGERABLE_MESSAGES:
            print(f"TRIGGER MESSAGE: {requested_message} is not implemented")
            return call_result.TriggerMessage(status=TriggerMessageStatus.not_implemented)
        if (
            connector_id is not None
            and requested_message in CONNECTOR_TRIGGERS
            and connector_id != 0
            and connector_id not in self.physical_connectors()
        ):
            print(f"TRIGGER MESSAGE: unknown connector {connector_id}; rejecting")
            return call_result.TriggerMessage(status=TriggerMessageStatus.rejected)
        print(f"TRIGGER MESSAGE: will send {requested_message} connector={connector_id}")
        return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)

    @after(Action.trigger_message)
    async def after_trigger_message(self, requested_message, connector_id=None, **kwargs):
        """Send the requested message now that TriggerMessage.conf is on the wire. A trigger
        only re-reports current state -- it never changes it."""
        if self.reject_trigger_message or requested_message not in TRIGGERABLE_MESSAGES:
            return
        message = MessageTrigger(requested_message)
        if message == MessageTrigger.boot_notification:
            await self.call(
                call.BootNotification(
                    charge_point_model=self.boot_model, charge_point_vendor=self.boot_vendor
                ),
                suppress=False,
            )
        elif message == MessageTrigger.heartbeat:
            await self.call(call.Heartbeat(), suppress=False)
        elif message == MessageTrigger.firmware_status_notification:
            await self._report_firmware_status(self.last_firmware_status)
        elif message == MessageTrigger.diagnostics_status_notification:
            await self._report_diagnostics_status(self.last_diagnostics_status)
        elif message == MessageTrigger.status_notification:
            if connector_id is not None and connector_id not in self.physical_connectors() | {0}:
                return
            # No connectorId means every connector -- including 0, the main controller.
            targets = [connector_id] if connector_id is not None else sorted(
                {0} | self.physical_connectors()
            )
            for target in targets:
                await self.send_status(target, self.get_connector_state(target).status)
        elif message == MessageTrigger.meter_values:
            targets = [connector_id] if connector_id is not None else sorted(
                self.physical_connectors()
            )
            for target in targets:
                await self.send_meter_values(target)

    def physical_connectors(self):
        """Connectors this charger physically has: 1..NumberOfConnectors, every one it has
        reported on, and its default one -- so an idle charger that has not sent anything yet
        still owns all of its connectors.
        """
        return (
            set(range(1, self.number_of_connectors + 1))
            | self.known_connectors
            | {self.default_connector_id}
        )

    def busy_connectors(self):
        """Connectors that already have a session, or are about to: an open transaction, a
        remote start accepted but not yet started, or a local start in progress. Nothing may
        start a second session on one of these."""
        return (
            set(self.open_transactions)
            | set(self.pending_remote_starts.values())
            | self.pending_local_starts
        )

    async def send_meter_values(self, connector_id):
        """Send this connector's current energy register (meter_readings) as MeterValues, tied
        to its open transaction if it has one."""
        transaction_id = self.open_transactions.get(connector_id)
        await self.call(
            call.MeterValues(
                connector_id=connector_id,
                transaction_id=transaction_id,
                meter_value=[
                    datatypes.MeterValue(
                        timestamp=now(),
                        sampled_value=[
                            datatypes.SampledValue(
                                value=str(self.meter_readings.get(connector_id, 0)),
                                measurand=Measurand.energy_active_import_register,
                                unit=UnitOfMeasure.wh,
                            )
                        ],
                    )
                ],
            ),
            suppress=False,
        )

    @on(Action.reset)
    def on_reset(self, type, **kwargs):
        if self.reject_reset:
            print(f"RESET: rejecting {type} reset (--reject-reset)")
            return call_result.Reset(status=ResetStatus.rejected)
        print(f"RESET: accepted a {type} reset")
        return call_result.Reset(status=ResetStatus.accepted)

    @after(Action.reset)
    async def after_reset(self, type, **kwargs):
        """Actually perform the reset once Reset.conf has been sent (OCPP 1.6 s5.14).

        Soft finishes gracefully: any open transaction is stopped and reported Finishing first,
        as a driver unplugging would be. Hard is a power cycle: it disconnects immediately, as a
        real power loss would, so any open transaction here is simply abandoned -- this Central
        System is expected to see its StopTransaction arrive late on reconnection, or never.
        Either way the connection is then closed: main() notices reboot_reason and reconnects
        with a fresh BootNotification, since OCPP 1.6 s4.2.1 requires the whole commissioning
        flow to run again after any reset.
        """
        if self.reject_reset:
            return
        reset_type = ResetType(type)
        if reset_type == ResetType.soft:
            for connector_id, transaction_id in list(self.open_transactions.items()):
                await self.finish_transaction(
                    connector_id,
                    transaction_id,
                    meter_stop=self.meter_readings.get(connector_id, 0),
                    reason=Reason.soft_reset,
                )
        self.reboot_reason = reset_type
        print(f"RESET: closing the connection to simulate a {reset_type} reboot")
        await self._connection.close()

    @on(Action.update_firmware)
    def on_update_firmware(self, location, retrieve_date, **kwargs):
        # OCPP 1.6 s5.16: UpdateFirmware.conf carries no status -- the charger always attempts
        # it; there is no protocol-level way to reject the request itself.
        print(f"UPDATE FIRMWARE: will fetch {location} at {retrieve_date}")
        return call_result.UpdateFirmware()

    @after(Action.update_firmware)
    async def after_update_firmware(self, location, retrieve_date, **kwargs):
        """Simulate the download/install lifecycle (OCPP 1.6 s4.13's FirmwareStatusNotification
        values), then reboot -- a real install always does (s4.2.1's commissioning flow runs
        again), so this reuses the same close-the-connection mechanism as after_reset.
        """
        await self._report_firmware_status(FirmwareStatus.downloading)
        await asyncio.sleep(0.5)
        if self.fail_firmware_download:
            await self._report_firmware_status(FirmwareStatus.download_failed)
            return
        await self._report_firmware_status(FirmwareStatus.downloaded)
        await asyncio.sleep(0.5)
        await self._report_firmware_status(FirmwareStatus.installing)
        await asyncio.sleep(0.5)
        await self._report_firmware_status(FirmwareStatus.installed)
        self.reboot_reason = "a firmware install"
        print("UPDATE FIRMWARE: closing the connection to simulate the post-install reboot")
        await self._connection.close()

    async def _report_firmware_status(self, status):
        print(f"FIRMWARE STATUS: {status.value}")
        self.last_firmware_status = status
        await self.call(call.FirmwareStatusNotification(status=status), suppress=False)

    @on(Action.get_diagnostics)
    def on_get_diagnostics(self, location, **kwargs):
        if self.decline_diagnostics:
            print("GET DIAGNOSTICS: declining (--decline-diagnostics): no file to upload")
            return call_result.GetDiagnostics()
        file_name = f"{self.id}-diagnostics.zip"
        print(f"GET DIAGNOSTICS: will upload {file_name} to {location}")
        return call_result.GetDiagnostics(file_name=file_name)

    @after(Action.get_diagnostics)
    async def after_get_diagnostics(self, location, **kwargs):
        """Simulate the upload lifecycle (OCPP 1.6 s4.9's DiagnosticsStatusNotification
        values). No bytes actually move -- location typically points at plain HTTP in this
        project (see main.py's diagnostics_upload_base_url), and the websockets version in use
        cannot receive an upload on that same port even if this did try (see
        handle_admin_request's own note: GET only).
        """
        if self.decline_diagnostics:
            return
        await self._report_diagnostics_status(DiagnosticsStatus.uploading)
        await asyncio.sleep(0.5)
        if self.fail_diagnostics_upload:
            await self._report_diagnostics_status(DiagnosticsStatus.upload_failed)
            return
        await self._report_diagnostics_status(DiagnosticsStatus.uploaded)

    async def _report_diagnostics_status(self, status):
        print(f"DIAGNOSTICS STATUS: {status.value}")
        self.last_diagnostics_status = status
        await self.call(call.DiagnosticsStatusNotification(status=status), suppress=False)

    @on(Action.remote_start_transaction)
    def on_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        if self.reject_remote_start:
            print(f"REMOTE START: rejecting request for {id_tag} (--reject-remote-start)")
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        connector_id = connector_id or self._free_connector()
        if connector_id in self.busy_connectors():
            # A second transaction on a connector that already has one would leave the first
            # open on the Central System with nobody to stop it. OCPP 1.6 s5.11: the conf says
            # "whether it has accepted the request and will attempt to start a transaction".
            print(f"REMOTE START: connector {connector_id} already has a session; rejecting")
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        profile_data = kwargs.get("charging_profile")
        if profile_data is not None and not self.no_smart_charging:
            # s5.16.2: it must be a TxProfile without a transactionId; the charger applies it to
            # the transaction that this start is about to create. A charger without smart
            # charging ignores it (s5.11).
            try:
                profile = parse_profile(profile_data)
            except ValueError as exc:
                print(f"REMOTE START: rejecting, bad charging profile: {exc}")
                return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
            problem = (
                "a RemoteStartTransaction profile must be a TxProfile without a transactionId"
                if profile.purpose != TX or profile.transaction_id is not None
                else self._profile_problem(profile, connector_id, transaction_pending=True)
            )
            if problem:
                print(f"REMOTE START: rejecting, {problem}")
                return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        print(
            f"REMOTE START: accepted for id_tag={id_tag} connector={connector_id} "
            f"(AuthorizeRemoteTxRequests={self.authorize_remote_tx_requests})"
        )
        self.pending_remote_starts[id_tag] = connector_id
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    def _free_connector(self):
        """The connector a RemoteStartTransaction without a connectorId starts on: the
        lowest-numbered one that is Available and not busy, else the default connector."""
        busy = self.busy_connectors()
        for connector_id in sorted(self.physical_connectors()):
            if (
                connector_id not in busy
                and self.get_connector_state(connector_id).status == ChargePointStatus.available
            ):
                return connector_id
        return self.default_connector_id

    @after(Action.remote_start_transaction)
    async def after_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        """Actually start the transaction, once RemoteStartTransaction.conf has been sent.

        Split from on_remote_start_transaction via the ocpp library's @after hook (see
        main.py's after_boot for why a bare asyncio.create_task cannot give the same ordering
        guarantee): the conf must reach the Central System before this sends anything else.

        The connector is the one on_remote_start_transaction chose and recorded. No record means
        the conf was Rejected, and nothing is started. The record is dropped only once this
        finishes, so busy_connectors() covers the connector for the whole start.
        """
        if id_tag not in self.pending_remote_starts:
            return
        try:
            await self._start_remote_transaction(
                id_tag, self.pending_remote_starts[id_tag], **kwargs
            )
        finally:
            self.pending_remote_starts.pop(id_tag, None)

    async def _start_remote_transaction(self, id_tag, connector_id, **kwargs):
        """The body of after_remote_start_transaction, on the connector already chosen."""
        if self.authorize_remote_tx_requests:
            # OCPP 1.6 s3.6: LocalPreAuthorize lets a charger "start a transaction without
            # waiting for a response from the Central System" -- even while online -- by
            # deciding from its own Local Authorization List / Authorization Cache first.
            local_info = None
            if self.configuration["LocalPreAuthorize"][0] == "true":
                local_info = self.local_authorize(id_tag)
            if local_info is not None:
                print(f"REMOTE START: {id_tag} pre-authorized locally ({local_info['status']})")
                if local_info["status"] != AuthorizationStatus.accepted:
                    return
            else:
                auth = await self.authorize(id_tag)
                if auth.id_tag_info["status"] != AuthorizationStatus.accepted:
                    print(
                        f"REMOTE START: {id_tag} not authorized locally "
                        f"({auth.id_tag_info['status']}); not starting"
                    )
                    return
        await self.send_status(connector_id, ChargePointStatus.preparing)
        # The connector's energy register carries on from wherever it stands: a meter never
        # resets to 0 for a new session, and stopping at the register keeps energy positive.
        meter_start = self.meter_readings.get(connector_id, 0)
        start = await self.call(
            call.StartTransaction(
                connector_id=connector_id, id_tag=id_tag, meter_start=meter_start,
                timestamp=now(),
            ),
            suppress=False,
        )
        self._cache_id_tag_info(id_tag, start.id_tag_info)
        self.open_transactions[connector_id] = start.transaction_id
        self.transaction_started_at[connector_id] = datetime.now(UTC)
        self.meter_readings[connector_id] = meter_start
        profile_data = kwargs.get("charging_profile")
        if profile_data is not None and not self.no_smart_charging:
            profile = parse_profile(profile_data).model_copy(
                update={"transaction_id": start.transaction_id}
            )
            self.charging_profiles.install(connector_id, profile, datetime.now(UTC))
            print(f"REMOTE START: applied the request's TxProfile {profile.charging_profile_id}")
        await self.send_status(connector_id, ChargePointStatus.charging)
        print(f"REMOTE START: transaction_id={start.transaction_id} now Charging")
        await self.refresh_charging_limits()

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
        await self.finish_transaction(
            connector_id,
            transaction_id,
            meter_stop=self.meter_readings.get(connector_id, 0),
            reason=Reason.remote,
        )
        print(
            f"REMOTE STOP: transaction_id={transaction_id} stopped, "
            f"connector {connector_id} Finishing"
        )

    async def finish_transaction(
        self, connector_id, transaction_id, meter_stop, id_tag=None, reason=None
    ):
        """Send StopTransaction, report Finishing, and apply any availability change that was
        waiting on this transaction to end (see _availability_blocked / deferred_availability).

        Shared by run_scenario's scripted stop, after_remote_stop_transaction and the demo
        fleet (run_demo_fleet.py), so a ChangeAvailability(Inoperative) deferred by any of them
        gets applied the same way.
        """
        kwargs = {}
        if id_tag is not None:
            kwargs["id_tag"] = id_tag
        if reason is not None:
            kwargs["reason"] = reason
        stop = await self.call(
            call.StopTransaction(
                transaction_id=transaction_id, meter_stop=meter_stop, timestamp=now(), **kwargs
            ),
            suppress=False,
        )
        if id_tag is not None:
            self._cache_id_tag_info(id_tag, stop.id_tag_info)
        await self.send_status(connector_id, ChargePointStatus.finishing)
        self.open_transactions.pop(connector_id, None)
        self.transaction_started_at.pop(connector_id, None)
        self.suspended_by_profile.discard(connector_id)
        # s3.13.1: a TxProfile ceases to be valid when its transaction terminates.
        self.charging_profiles.drop_tx_profiles(connector_id)
        deferred = self.deferred_availability.pop(connector_id, None)
        if deferred is not None:
            await self._apply_availability(connector_id, deferred)
        return stop

    async def start_local_session(self, connector_id, id_tag):
        """A driver presents id_tag at connector_id and plugs in: authorize, report Preparing,
        start the transaction, report Charging. Returns the transaction id, or None when the
        session did not start.

        Authorize comes first, so a refused tag never touches the connector. If
        StartTransaction.conf then refuses the tag after all, the transaction already exists
        on the Central System and is ended at once with DeAuthorized -- OCPP 1.6 s7.36: "stopped
        because of the authorization status in a StartTransaction.conf" -- rather than left open.
        """
        self.pending_local_starts.add(connector_id)
        try:
            auth = await self.authorize(id_tag)
            if auth.id_tag_info["status"] != AuthorizationStatus.accepted:
                return None
            await self.send_status(connector_id, ChargePointStatus.preparing)
            meter_start = self.meter_readings.get(connector_id, 0)
            start = await self.call(
                call.StartTransaction(
                    connector_id=connector_id, id_tag=id_tag, meter_start=meter_start,
                    timestamp=now(),
                ),
                suppress=False,
            )
            self._cache_id_tag_info(id_tag, start.id_tag_info)
            self.open_transactions[connector_id] = start.transaction_id
            self.transaction_started_at[connector_id] = datetime.now(UTC)
            self.meter_readings[connector_id] = meter_start
        finally:
            self.pending_local_starts.discard(connector_id)
        if start.id_tag_info["status"] != AuthorizationStatus.accepted:
            await self.finish_transaction(
                connector_id, start.transaction_id, meter_stop=meter_start,
                reason=Reason.de_authorized,
            )
            return None
        await self.send_status(connector_id, ChargePointStatus.charging)
        return start.transaction_id


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
    if boot.status == RegistrationStatus.accepted:
        # A real charger reports each of its connectors after booting, so a multi-connector
        # charger run by hand shows all of them on the map, not just the one it charges on.
        await charge_point.send_boot_statuses()
    if args.simulate_fault is not None:
        await charge_point.simulate_fault_and_recover(
            args.connector_id, ChargePointErrorCode(args.simulate_fault)
        )
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

    await charge_point.send_status(args.connector_id, ChargePointStatus.preparing)
    results.append(check("StatusNotification", True, "connector reported Preparing"))

    authorize = await charge_point.authorize(args.id_tag)
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
    charge_point._cache_id_tag_info(args.id_tag, start.id_tag_info)
    transaction_id = start.transaction_id
    charge_point.open_transactions[args.connector_id] = transaction_id
    charge_point.transaction_started_at[args.connector_id] = datetime.now(UTC)
    charge_point.meter_readings[args.connector_id] = args.meter_start
    results.append(
        check(
            "StartTransaction",
            start.id_tag_info["status"] == AuthorizationStatus.accepted and transaction_id > 0,
            f"transaction_id={transaction_id} meter_start={args.meter_start}Wh",
        )
    )

    await charge_point.send_status(args.connector_id, ChargePointStatus.charging)
    results.append(check("StatusNotification", True, "connector reported Charging"))

    meter_stop = args.meter_start + args.energy
    charge_point.meter_readings[args.connector_id] = meter_stop
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

    stop = await charge_point.finish_transaction(
        args.connector_id, transaction_id, meter_stop, id_tag=args.id_tag, reason=Reason.local
    )
    results.append(
        check("StopTransaction", True, f"transaction_id={transaction_id} -> {stop.id_tag_info}")
    )

    return all(results)


async def run_connection(args, identity, key, uri, reconnect_after_reboot=False):
    """Open one WebSocket connection and run the requested scenario on it.

    Split out of main() so a Reset (OCPP 1.6 s5.14) or an UpdateFirmware install (s5.16) can be
    followed by a second call here: the charger's own after_reset/after_update_firmware handler
    closes the connection to simulate a reboot, and reconnect_after_reboot=True tells this
    second connection to just boot rather than repeat the original scripted scenario, standing
    in for OCPP 1.6 s4.2.1's "the whole commissioning flow runs again from BootNotification".

    Returns (passed, reboot_reason, rotated_key).
    """
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
            reject_change_availability=args.reject_change_availability,
            reject_reset=args.reject_reset,
            reject_trigger_message=args.reject_trigger_message,
            reject_charging_profile=args.reject_charging_profile,
            no_smart_charging=args.no_smart_charging,
            boot_vendor=args.vendor,
            boot_model=args.model,
            reject_unlock=args.reject_unlock,
            reject_change_configuration=args.reject_change_configuration,
            reject_clear_cache=args.reject_clear_cache,
            reject_reserve_now=args.reject_reserve_now,
            reject_send_local_list=args.reject_send_local_list,
            fail_firmware_download=args.fail_firmware_download,
            decline_diagnostics=args.decline_diagnostics,
            fail_diagnostics_upload=args.fail_diagnostics_upload,
            default_connector_id=args.connector_id,
            number_of_connectors=args.number_of_connectors,
        )
        # start() consumes the socket; call() needs it running to get responses.
        listener = asyncio.create_task(charge_point.start())
        try:
            if reconnect_after_reboot:
                boot = await perform_boot(charge_point, args)
                print(f"BOOT: status={boot.status} interval={boot.interval}s")
                passed = boot.status == RegistrationStatus.accepted
            elif args.idle_seconds > 0:
                passed = await idle_for_remote_control(charge_point, args)
            else:
                passed = await run_scenario(charge_point, args)
        finally:
            charge_point.cancel_limit_watch()
            listener.cancel()
        return passed, charge_point.reboot_reason, charge_point.rotated_key


async def main(args):
    identity, key = resolve_credentials(args)
    # The identity is percent-encoded as one path segment (OCPP-J 1.6 s3.1.1).
    uri = f"{args.url.rstrip('/')}/{quote(identity, safe='')}"
    reconnect_after_reboot = False
    try:
        while True:
            print(f"Connecting to {uri} as {identity}")
            passed, reboot_reason, rotated_key = await run_connection(
                args, identity, key, uri, reconnect_after_reboot=reconnect_after_reboot
            )
            # Route B rotated this charger's key mid-connection: write it back so the next
            # connection (this reconnect, or a future run of this simulator) authenticates with
            # it, standing in for a real charger persisting its new key to flash storage
            # (OCPP-J 1.6 s6.2.2).
            if rotated_key is not None:
                credentials = load_credentials(args.credentials)
                credentials[identity] = rotated_key
                pathlib.Path(args.credentials).write_text(
                    json.dumps(credentials, indent=2) + "\n", encoding="utf-8"
                )
                print(f"Persisted rotated key for {identity} to {args.credentials}.")
                key = rotated_key
            if reboot_reason is None:
                break
            print(f"REBOOT: reconnecting after {reboot_reason} (OCPP 1.6 s4.2.1)")
            reconnect_after_reboot = True
    except InvalidStatus as exc:
        print(f"Central system refused the handshake: HTTP {exc.response.status_code}.")
        print("The identity or authorization key is wrong; re-run 'python seed.py'.")
        return 1
    except OSError as exc:
        print(f"Could not reach {uri}: {exc}. Is main.py running?")
        return 1

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
    parser.add_argument(
        "--reject-change-availability",
        action="store_true",
        help="answer Rejected to any ChangeAvailability instead of Accepted/Scheduled",
    )
    parser.add_argument(
        "--reject-reset",
        action="store_true",
        help="answer Rejected to any Reset instead of Accepted, and never reboot",
    )
    parser.add_argument(
        "--reject-trigger-message",
        action="store_true",
        help="answer Rejected to any TriggerMessage instead of Accepted",
    )
    parser.add_argument(
        "--reject-charging-profile",
        action="store_true",
        help="answer Rejected to any SetChargingProfile instead of installing it",
    )
    parser.add_argument(
        "--no-smart-charging",
        action="store_true",
        help="behave as a charger without the Smart Charging profile: NotSupported / Unknown / "
        "Rejected to SetChargingProfile / ClearChargingProfile / GetCompositeSchedule",
    )
    parser.add_argument(
        "--reject-unlock",
        action="store_true",
        help="answer UnlockFailed to any UnlockConnector instead of Unlocked",
    )
    parser.add_argument(
        "--reject-change-configuration",
        action="store_true",
        help="answer Rejected to any ChangeConfiguration for a non-AuthorizationKey key",
    )
    parser.add_argument(
        "--reject-clear-cache",
        action="store_true",
        help="answer Rejected to any ClearCache instead of Accepted",
    )
    parser.add_argument(
        "--reject-reserve-now",
        action="store_true",
        help="answer Rejected to any ReserveNow instead of Accepted/Occupied/Faulted/Unavailable",
    )
    parser.add_argument(
        "--reject-send-local-list",
        action="store_true",
        help="answer Failed to any SendLocalList instead of Accepted/VersionMismatch",
    )
    parser.add_argument(
        "--simulate-fault",
        choices=[
            code.value for code in ChargePointErrorCode if code != ChargePointErrorCode.no_error
        ],
        default=None,
        help="in --idle-seconds mode, report this fault on --connector-id shortly after boot, "
        "then recover to the exact pre-fault status (OCPP 1.6 s4.9 I1-I8)",
    )
    parser.add_argument(
        "--fail-firmware-download",
        action="store_true",
        help="report DownloadFailed instead of completing an UpdateFirmware install",
    )
    parser.add_argument(
        "--decline-diagnostics",
        action="store_true",
        help="answer GetDiagnostics with no file_name, meaning nothing will be uploaded",
    )
    parser.add_argument(
        "--fail-diagnostics-upload",
        action="store_true",
        help="report UploadFailed instead of completing a GetDiagnostics upload",
    )
    parser.add_argument("--id-tag", default="TAG001", help="RFID tag used to authorize")
    parser.add_argument("--connector-id", type=int, default=1, help="connector to charge on")
    parser.add_argument(
        "--number-of-connectors",
        type=int,
        default=1,
        help="how many connectors this charger has (its NumberOfConnectors configuration key)",
    )
    parser.add_argument("--meter-start", type=int, default=0, help="meter reading in Wh at start")
    parser.add_argument("--energy", type=int, default=1500, help="Wh charged during the session")
    parser.add_argument("--vendor", default="PocVendor", help="charge point vendor")
    parser.add_argument("--model", default="PocModel", help="charge point model")
    parser.add_argument("--stop", action="store_true", help="also stop the transaction at the end")
    return parser.parse_args()


# Guarded, like main.py and for the same reason: run_demo_fleet.py and the tests import
# SimulatedChargePoint from this module, and an unguarded call would parse their command line
# and run a scripted session on every import. `python simulate_charge_point.py ...` is
# unaffected: __name__ is "__main__" only when run directly.
if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
