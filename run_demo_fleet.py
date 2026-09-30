"""Run the demo fleet: every mock charger in the manifest, live, against the Central System.

    python run_demo_fleet.py
    python run_demo_fleet.py --limit 10 --time-scale 0.1
    python run_demo_fleet.py --duration-seconds 600 --seed 1

Demo data (11-demo-fleet.md). One process and one asyncio loop, with one OCPP-J connection per
charger that seed_demo_fleet.py wrote into the manifest. Each charger boots, reports its
connectors (Faulted where PlugShare reported the outlet out of order), and then drives sessions
of its own: a driver authorizes, charges for a few minutes with meter values flowing, and
unplugs. Status reaches the map only the real way: OCPP to main.py, then Redis, then the API.

A charger simulator knows only its own configuration and flash storage, so this never touches
MongoDB. The manifest is the configuration; demo_fleet_state.json is the flash storage (energy
registers and any transaction still open), which is how the next run closes sessions a crash
or a dropped connection interrupted.

Ctrl+C (or SIGTERM, or --duration-seconds running out) stops every session, reports each
working connector Unavailable and exits within 20 s. A second Ctrl+C exits at once.
"""

import argparse
import asyncio
import json
import logging
import os
import pathlib
import random
import signal
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import websockets
from ocpp.v16 import call
from ocpp.v16.enums import ChargePointStatus, Reason, RegistrationStatus
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.typing import Subprotocol

from ocpp_client_auth import basic_auth_header
from simulate_charge_point import SimulatedChargePoint, now

# simulate_charge_point configures INFO logging, under which the ocpp library logs every frame
# it sends or receives -- thousands a minute from 195 chargers.
logging.getLogger("ocpp").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

DEFAULT_MANIFEST = "demo_fleet_manifest.json"
DEFAULT_STATE_FILE = "demo_fleet_state.json"

# What BootNotification reports; both are CiString20 (OCPP 1.6 s6.3).
BOOT_VENDOR = "DemoFleet"
BOOT_MODEL = "PlugShareMock"

# Charging power assumed when PlugShare states no rating. It only shapes the Wh readings, and is
# never shown as the connector's rating.
DEFAULT_RATE_KW = {"AC": 7.4, "DC": 50.0}
RATE_FACTOR = (0.6, 1.0)

# Durations in (unscaled) seconds; --time-scale multiplies each of them.
INITIAL_START_WINDOW = 30
REFUSED_RETRY = 60
FINISHING_HOLD = (5, 15)

# Reconnect backoff: 2 s doubling to 30 s, +-20% jitter so a restarted Central System is not
# hit by every charger in the same instant.
BACKOFF_INITIAL = 2
BACKOFF_MAX = 30
BACKOFF_JITTER = 0.2
OPEN_TIMEOUT = 30
CLOSE_TIMEOUT = 5

SUMMARY_INTERVAL = 30
STATE_WRITE_MIN_INTERVAL = 1.0
ALIVE_REFRESH_INTERVAL = 10
# The whole graceful shutdown has 20 s (§E.6); this is how much of it the chargers get, leaving
# the rest for cancelling stragglers and writing the final state file.
SHUTDOWN_BUDGET = 17
SESSION_DRAIN_TIMEOUT = 5


class ManifestError(Exception):
    """The manifest is missing or unusable."""


def load_manifest(path):
    """The manifest's charger entries, in manifest order. Raises ManifestError."""
    try:
        manifest = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"{path} not found. Run 'python seed_demo_fleet.py' first.") from exc
    except (OSError, ValueError) as exc:
        raise ManifestError(f"could not read {path}: {exc}") from exc
    chargers = manifest.get("chargers") if isinstance(manifest, dict) else None
    if not chargers:
        raise ManifestError(f"{path} lists no chargers. Run 'python seed_demo_fleet.py' first.")
    for entry in chargers:
        if not entry.get("identity") or not entry.get("authorization_key"):
            raise ManifestError(f"{path} has a charger without an identity or key: {entry!r}")
    return chargers


def _write_json_atomically(path, payload):
    """Write JSON to a temp file in the same directory, then os.replace it into place, so a
    reader never sees a half-written file, even after a hard kill."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


class FleetState:
    """demo_fleet_state.json: every charger's flash storage (§E.5).

    Holds, per identity, its energy registers and the transactions it still has open. Written
    whole, atomically, at most once a second across the fleet, and read once at start. Entries
    for chargers not running this time (--limit) are kept, not dropped.
    """

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.chargers = {}  # identity -> {"registers": {int: int}, "open_transactions": {...}}
        self.alive_at = None
        self._dirty = False
        self._last_write = float("-inf")
        self._last_alive = float("-inf")

    def load(self):
        """Read the file. Missing or unreadable means an empty state, never a crash."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.chargers = {
                identity: {
                    "registers": _int_map(entry.get("registers")),
                    "open_transactions": _int_map(entry.get("open_transactions")),
                }
                for identity, entry in (data.get("chargers") or {}).items()
            }
        except FileNotFoundError:
            logging.warning("no state file at %s yet: starting with empty registers", self.path)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logging.warning("state file %s is unreadable (%s): starting empty", self.path, exc)
            self.chargers = {}

    def get(self, identity):
        return self.chargers.get(identity, {"registers": {}, "open_transactions": {}})

    def update(self, identity, snapshot):
        """Record a charger's current snapshot; marks the file for writing if it changed."""
        if self.chargers.get(identity) != snapshot:
            self.chargers[identity] = snapshot
            self._dirty = True

    def write(self, alive=True):
        """Write the file now. alive=True stamps fleet_alive_at with the current time, False
        clears it (a graceful exit), None leaves the last stamp (what a hard kill leaves)."""
        if alive:
            self.alive_at = datetime.now(UTC).isoformat()
            self._last_alive = time.monotonic()
        elif alive is False:
            self.alive_at = None
        payload = {
            "fleet_alive_at": self.alive_at,
            "chargers": {
                identity: {
                    "registers": {str(k): v for k, v in sorted(entry["registers"].items())},
                    "open_transactions": {
                        str(k): v for k, v in sorted(entry["open_transactions"].items())
                    },
                }
                for identity, entry in sorted(self.chargers.items())
            },
        }
        _write_json_atomically(self.path, payload)
        self._dirty = False
        self._last_write = time.monotonic()

    async def keep_written(self):
        """Write whenever something changed, and at least every 10 s so fleet_alive_at stays
        fresh -- but never more than once a second. The first change after a quiet second is
        written straight away, which keeps the window a hard kill can lose as short as the
        once-a-second limit allows."""
        while True:
            moment = time.monotonic()
            due = self._dirty or moment - self._last_alive >= ALIVE_REFRESH_INTERVAL
            if due and moment - self._last_write >= STATE_WRITE_MIN_INTERVAL:
                try:
                    self.write()
                except OSError as exc:
                    logging.warning("could not write %s: %s", self.path, exc)
            await asyncio.sleep(0.1)


def _int_map(raw):
    return {int(key): int(value) for key, value in (raw or {}).items()}


@dataclass
class Meter:
    """Energy accounting for the session open on one connector."""

    transaction_id: int
    rate_kw: float
    energy_wh: float
    last_at: float


class FleetCharger:
    """One mock charger: its manifest entry, and the lifecycle of its connection (§E.3)."""

    def __init__(self, spec, fleet):
        self.identity = spec["identity"]
        self.key = spec["authorization_key"]
        self.connectors = {c["connector_id"]: c for c in spec["connectors"]}
        self.faulted = frozenset(cid for cid, c in self.connectors.items() if c["out_of_order"])
        self.fleet = fleet
        self.rng = random.Random(fleet.rng.random())
        # One generator per connector, drawn in manifest order, so --seed gives every connector
        # the same sequence of waits however the event loop happens to interleave them.
        self.connector_rngs = {
            cid: random.Random(self.rng.random()) for cid in sorted(self.connectors)
        }
        stored = fleet.state.get(self.identity)
        self.registers = dict(stored["registers"])
        # Transactions the Central System still believes open because this charger vanished
        # mid-session. Closed with PowerLoss after the next Accepted boot.
        self.leftovers = dict(stored["open_transactions"])
        self.charge_point = None
        self.connected = False
        self.failing = False
        self._logged = set()
        self._meters = {}
        self._finishing = {}  # connector_id -> (entered at, hold seconds)

    # --- state -------------------------------------------------------------------------------

    def snapshot(self):
        """Registers and open transactions, built from the charger object itself so that a
        session started or stopped by any path (fleet, remote, reset) is captured."""
        registers = dict(self.registers)
        open_transactions = dict(self.leftovers)
        if self.charge_point is not None:
            registers.update(self.charge_point.meter_readings)
            open_transactions.update(self.charge_point.open_transactions)
        return {"registers": registers, "open_transactions": open_transactions}

    def save_snapshot(self):
        self.fleet.state.update(self.identity, self.snapshot())

    def _log_once(self, key, message):
        if key not in self._logged:
            self._logged.add(key)
            print(f"{self.identity}: {message}")

    # --- connection lifecycle ----------------------------------------------------------------

    def _jitter(self, seconds):
        return seconds * self.rng.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)

    def _report_failure(self, reason):
        # One line per failure episode; repeats are suppressed until the charger recovers.
        if not self.failing:
            self.failing = True
            print(f"{self.identity}: {reason}; retrying with backoff")

    async def run(self):
        """Connect, run, reconnect -- until the fleet stops (§E.3)."""
        fleet = self.fleet
        uri = f"{fleet.options.url.rstrip('/')}/{quote(self.identity, safe='')}"
        backoff = BACKOFF_INITIAL
        while await fleet.connect_slot():
            try:
                websocket = await fleet.until_stopped(
                    websockets.connect(
                        uri,
                        subprotocols=[Subprotocol("ocpp1.6")],
                        additional_headers={
                            "Authorization": basic_auth_header(self.identity, self.key)
                        },
                        open_timeout=OPEN_TIMEOUT,
                        close_timeout=CLOSE_TIMEOUT,
                    )
                )
            # InvalidHandshake covers InvalidStatus (a refused handshake) and a server that
            # hangs up mid-handshake; TimeoutError is open_timeout expiring.
            except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed) as exc:
                self._report_failure(f"cannot connect ({_describe(exc)})")
                if not await fleet.sleep(self._jitter(backoff)):
                    break
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            if websocket is None:
                break
            try:
                booted, reason = await self._run_connection(websocket)
            finally:
                await _close_quietly(websocket)
            if booted:
                backoff = BACKOFF_INITIAL
            if fleet.stopping:
                break
            self._report_failure(reason)
            if not await fleet.sleep(self._jitter(backoff)):
                break
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _run_connection(self, websocket):
        """Run one connection until it drops or the fleet stops. Returns (booted, reason), the
        reason being why it ended when that was not the fleet stopping."""
        charge_point = SimulatedChargePoint(
            self.identity,
            websocket,
            authorization_key=self.key,
            number_of_connectors=len(self.connectors),
            boot_vendor=BOOT_VENDOR,
            boot_model=BOOT_MODEL,
        )
        charge_point.meter_readings.update(self.registers)
        self.charge_point = charge_point
        self._meters.clear()
        self._finishing.clear()
        listener = asyncio.create_task(charge_point.start())
        session = asyncio.create_task(self._connected(charge_point))
        booted, reason = False, "connection closed"
        try:
            await asyncio.wait({listener, session}, return_when=asyncio.FIRST_COMPLETED)
            if session.done():
                booted = session.result()
            else:
                # The connection closed under us: the peer went away, or an operator Reset
                # (which the simulator performs by closing the connection itself).
                exc = listener.exception()
                if charge_point.reboot_reason is not None:
                    reason = f"rebooting after a {charge_point.reboot_reason} reset"
                elif exc is not None:
                    reason = f"connection lost ({_describe(exc)})"
                booted = self.connected
        except Exception as exc:  # a call failed or timed out: drop this connection, retry
            reason = f"connection abandoned ({_describe(exc)})"
            booted = self.connected
        finally:
            for task in (session, listener):
                task.cancel()
            await asyncio.gather(session, listener, return_exceptions=True)
            charge_point.cancel_limit_watch()
            self.connected = False
            # Whatever was still open is now a leftover for the next connection to close.
            self.registers.update(charge_point.meter_readings)
            self.leftovers.update(charge_point.open_transactions)
            self.charge_point = None
            self.save_snapshot()
        return booted, reason

    async def _connected(self, charge_point):
        """Boot, close leftovers, report statuses, run sessions; shut down when the fleet
        stops. Returns whether the charger got as far as an Accepted boot."""
        boot = await self._boot(charge_point)
        if boot is None:
            return False
        await self._close_leftovers(charge_point)
        await charge_point.send_boot_statuses(faulted=self.faulted)
        self.connected = True
        if self.failing:
            self.failing = False
            print(f"{self.identity}: reconnected")
        sessions = [
            asyncio.create_task(self._session_loop(charge_point, connector_id))
            for connector_id in sorted(self.connectors)
            if connector_id not in self.faulted
        ]
        chores = [
            asyncio.create_task(self._heartbeat(charge_point, max(1, boot.interval))),
            asyncio.create_task(self._housekeeping(charge_point)),
        ]
        try:
            await self.fleet.stop_event.wait()
            if self.fleet.graceful_shutdown:
                await self._shutdown(charge_point, sessions, chores)
        finally:
            for task in sessions + chores:
                task.cancel()
            await asyncio.gather(*sessions, *chores, return_exceptions=True)
        return True

    async def _boot(self, charge_point):
        """BootNotification until Accepted. OCPP 1.6 s4.2: after any other answer, `interval`
        is the minimum wait before the next BootNotification. None if the fleet stopped."""
        while True:
            conf = await charge_point.call(
                call.BootNotification(
                    charge_point_model=charge_point.boot_model,
                    charge_point_vendor=charge_point.boot_vendor,
                ),
                suppress=False,
            )
            if conf.status == RegistrationStatus.accepted:
                return conf
            self._log_once("boot", f"boot answered {conf.status}; retrying every {conf.interval}s")
            if not await self.fleet.sleep(max(1, conf.interval), scaled=False):
                return None

    async def _close_leftovers(self, charge_point):
        """Stop every transaction the last connection left open (§E.3 step 4).

        The charger vanished mid-session and came back without it, and PowerLoss ("complete
        loss of power", OCPP 1.6 s7.36) is the closest reason defined. No idTag: nobody
        presented one. Closing them is also what keeps each connector's idTag usable -- one
        left open would answer ConcurrentTx to every later Authorize. main.py ignores a
        duplicate, so re-sending one a crash interrupted is harmless.
        """
        for connector_id, transaction_id in sorted(self.leftovers.items()):
            await charge_point.call(
                call.StopTransaction(
                    transaction_id=transaction_id,
                    meter_stop=charge_point.meter_readings.get(connector_id, 0),
                    timestamp=now(),
                    reason=Reason.power_loss,
                ),
                suppress=False,
            )
            del self.leftovers[connector_id]
            self.fleet.leftovers_closed += 1
        self.save_snapshot()

    async def _shutdown(self, charge_point, sessions, chores):
        """Graceful stop (§E.6): stop the background work, end every open session, report the
        working connectors Unavailable. Faulted ones stay Faulted: an out-of-order charger is
        still out of order when the demo stops, and moving it would close its FaultEvent."""
        # Every background task returns by itself at its next wait once the fleet is stopping.
        # Waiting for that rather than cancelling means a call already on the wire gets its
        # conf: a StartTransaction's is recorded here, instead of leaving a transaction open on
        # the Central System that nobody knows of.
        await asyncio.wait(sessions + chores, timeout=SESSION_DRAIN_TIMEOUT)
        for connector_id, transaction_id in sorted(charge_point.open_transactions.items()):
            await charge_point.finish_transaction(
                connector_id,
                transaction_id,
                meter_stop=charge_point.meter_readings.get(connector_id, 0),
                reason=Reason.other,
            )
        for connector_id in sorted(charge_point.physical_connectors()):
            status = charge_point.get_connector_state(connector_id).status
            if status in (ChargePointStatus.faulted, ChargePointStatus.unavailable):
                continue
            if status == ChargePointStatus.preparing:
                # OCPP 1.6 s4.9 has no Preparing -> Unavailable; the driver walks away first.
                await charge_point.send_status(connector_id, ChargePointStatus.available)
            await charge_point.send_status(connector_id, ChargePointStatus.unavailable)
        self.save_snapshot()

    # --- background tasks --------------------------------------------------------------------

    async def _heartbeat(self, charge_point, interval):
        """Heartbeat every `interval` seconds from the boot conf. Never scaled."""
        while await self.fleet.sleep(interval, scaled=False):
            try:
                await charge_point.call(call.Heartbeat(), suppress=False)
            except Exception as exc:
                self._log_once("heartbeat", f"heartbeat failed ({_describe(exc)})")

    async def _housekeeping(self, charge_point):
        """Once a tick: advance meters, unplug finished sessions, save the snapshot (§E.4)."""
        while await self.fleet.sleep(self.fleet.tick, scaled=False):
            try:
                await self._advance_meters(charge_point)
                await self._release_finishing(charge_point)
            except Exception as exc:
                self._log_once("housekeeping", f"housekeeping failed ({_describe(exc)})")
            self.save_snapshot()

    def _rate_kw(self, connector_id):
        spec = self.connectors[connector_id]
        rated = spec.get("max_power_kw") or DEFAULT_RATE_KW[spec["power_type"]]
        return rated * self.rng.uniform(*RATE_FACTOR)

    async def _advance_meters(self, charge_point):
        """For every open transaction, whoever started it, add the energy delivered since the
        last reading and send MeterValues, every meter_interval.

        Energy is counted in simulated time (elapsed / time_scale), so a sped-up session still
        delivers what a real one of that length would; at the default scale the two agree.
        """
        options = self.fleet.options
        moment = time.monotonic()
        for connector_id, transaction_id in list(charge_point.open_transactions.items()):
            meter = self._meters.get(connector_id)
            if meter is None or meter.transaction_id != transaction_id:
                self._meters[connector_id] = Meter(
                    transaction_id=transaction_id,
                    rate_kw=self._rate_kw(connector_id),
                    energy_wh=float(charge_point.meter_readings.get(connector_id, 0)),
                    last_at=moment,
                )
                continue
            if moment - meter.last_at < options.meter_interval * options.time_scale:
                continue
            status = charge_point.get_connector_state(connector_id).status
            if status == ChargePointStatus.charging:
                simulated_seconds = (moment - meter.last_at) / options.time_scale
                meter.energy_wh += meter.rate_kw * simulated_seconds / 3600 * 1000
                charge_point.meter_readings[connector_id] = int(meter.energy_wh)
            meter.last_at = moment
            await charge_point.send_meter_values(connector_id)
        for connector_id in set(self._meters) - set(charge_point.open_transactions):
            del self._meters[connector_id]

    def _still_finishing(self, charge_point, connector_id):
        return (
            charge_point.get_connector_state(connector_id).status == ChargePointStatus.finishing
            and connector_id not in charge_point.busy_connectors()
        )

    async def _release_finishing(self, charge_point):
        """A connector left in Finishing with no transaction goes back to Available after its
        random 5-15 s: the driver unplugs. This covers remotely stopped sessions too, which the
        simulator on its own would leave Finishing -- and amber on the map -- forever.

        The hold counts from when the connector actually entered Finishing (the simulator's
        status_since), and one due before the next tick is waited out here, so the unplug
        comes on time rather than up to a tick late.
        """
        for connector_id in sorted(charge_point.physical_connectors()):
            if not self._still_finishing(charge_point, connector_id):
                self._finishing.pop(connector_id, None)
                continue
            entered_at = charge_point.status_since.get(connector_id, time.monotonic())
            if self._finishing.get(connector_id, (None,))[0] != entered_at:
                hold = self.rng.uniform(*FINISHING_HOLD) * self.fleet.options.time_scale
                self._finishing[connector_id] = (entered_at, hold)
            _entered_at, hold = self._finishing[connector_id]
            remaining = entered_at + hold - time.monotonic()
            if remaining > self.fleet.tick:
                continue
            await asyncio.sleep(max(0.0, remaining))
            if self._still_finishing(charge_point, connector_id):
                del self._finishing[connector_id]
                await charge_point.send_status(connector_id, ChargePointStatus.available)

    async def _session_loop(self, charge_point, connector_id):
        """A stream of drivers on one working connector (§E.4). Returns when the fleet stops,
        leaving any session still open to _shutdown."""
        options = self.fleet.options
        rng = self.connector_rngs[connector_id]
        id_tag = self.connectors[connector_id]["id_tag"]

        def idle():
            return rng.uniform(*options.idle_minutes) * 60

        # The first pass puts initial_busy of the connectors into a session within 30 s, so
        # the map is not all green for the first few minutes.
        if rng.random() < options.initial_busy:
            delay = rng.uniform(0, INITIAL_START_WINDOW)
        else:
            delay = rng.uniform(0, options.idle_minutes[1] * 60)
        while await self.fleet.sleep(delay):
            delay = idle()
            # Unavailable through an operator's ChangeAvailability, busy with a remote start,
            # or still Finishing: not this round.
            status = charge_point.get_connector_state(connector_id).status
            if status != ChargePointStatus.available or (
                connector_id in charge_point.busy_connectors()
            ):
                continue
            duration = rng.uniform(*options.session_minutes) * 60
            try:
                transaction_id = await charge_point.start_local_session(connector_id, id_tag)
            except Exception as exc:
                self._log_once(("start", connector_id), f"connector {connector_id}: {exc!r}")
                delay = REFUSED_RETRY
                continue
            if transaction_id is None:
                answer = charge_point.authorization_cache.get(id_tag, {}).get("status")
                self._log_once(
                    ("refused", connector_id),
                    f"connector {connector_id}: {id_tag} refused ({answer}); "
                    f"retrying every {REFUSED_RETRY * options.time_scale:g}s",
                )
                delay = REFUSED_RETRY
                continue
            self.fleet.sessions_started += 1
            if not await self.fleet.sleep(duration):
                return
            # Only a session this task started is ended here, and only if nobody (a remote
            # stop, a reset) ended it first. The driver stops it at the charger: Local.
            if charge_point.open_transactions.get(connector_id) != transaction_id:
                continue
            try:
                await charge_point.finish_transaction(
                    connector_id,
                    transaction_id,
                    meter_stop=charge_point.meter_readings.get(connector_id, 0),
                    id_tag=id_tag,
                    reason=Reason.local,
                )
            except Exception as exc:
                self._log_once(("stop", connector_id), f"connector {connector_id}: {exc!r}")


class Fleet:
    """Shared state for one run: options, the stop signal, the connect limiter, counters."""

    def __init__(self, chargers, options, stop_event, graceful_shutdown):
        self.options = options
        self.stop_event = stop_event
        self.graceful_shutdown = graceful_shutdown
        self.rng = random.Random(options.seed)
        self.state = FleetState(options.state_file)
        self.state.load()
        self.members = [FleetCharger(spec, self) for spec in chargers]
        self.sessions_started = 0
        self.leftovers_closed = 0
        # Housekeeping resolution: every second, finer when time is sped up so the scaled
        # Finishing hold and meter interval can still be honoured.
        self.tick = min(1.0, max(0.05, options.time_scale))
        self._next_slot = time.monotonic()

    @property
    def stopping(self):
        return self.stop_event.is_set()

    async def sleep(self, seconds, scaled=True):
        """Sleep `seconds` (times --time-scale unless scaled=False). Returns False, at once,
        when the fleet is stopping."""
        if self.stopping:
            return False
        if scaled:
            seconds *= self.options.time_scale
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            return True
        return False

    async def until_stopped(self, awaitable):
        """Await `awaitable`, or cancel it and return None if the fleet stops first."""
        task = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(self.stop_event.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None

    async def connect_slot(self):
        """Wait for this connection attempt's turn: at most connect_rate new connections per
        second, fleet-wide. False if the fleet stops meanwhile.

        main.py checks each handshake's key with scrypt (about 30 ms) inside its event loop,
        so a burst of handshakes stalls it. The first pass gives charger i the slot at
        i / connect_rate (§E.2); reconnects after a Central System restart queue the same way.
        """
        moment = time.monotonic()
        slot = max(moment, self._next_slot)
        self._next_slot = slot + 1 / self.options.connect_rate
        return await self.sleep(slot - moment, scaled=False)

    def summary_line(self):
        """§E.7's one-line status: connectors counted from each charger's local mirror."""
        counts = Counter()
        connected = 0
        for member in self.members:
            charge_point = member.charge_point
            if not member.connected or charge_point is None:
                continue
            connected += 1
            for connector_id in charge_point.physical_connectors():
                counts[charge_point.get_connector_state(connector_id).status] += 1
        return (
            f"connected {connected}/{len(self.members)}"
            f" · charging {counts[ChargePointStatus.charging]}"
            f" · available {counts[ChargePointStatus.available]}"
            f" · faulted {counts[ChargePointStatus.faulted]}"
            f" · unavailable {counts[ChargePointStatus.unavailable]}"
            f" · sessions {self.sessions_started}"
        )

    async def report_forever(self):
        while True:
            await asyncio.sleep(SUMMARY_INTERVAL)
            print(self.summary_line(), flush=True)


async def run_fleet(chargers, options, stop_event, graceful_shutdown=True):
    """Run `chargers` (manifest entries) until `stop_event` is set, then shut down.

    `options` is what parse_args() returns. graceful_shutdown=False skips ending sessions and
    reporting Unavailable, and just closes the connections -- a hard kill, for the tests.
    Returns counters for the run.
    """
    fleet = Fleet(chargers, options, stop_event, graceful_shutdown)
    writer = asyncio.create_task(fleet.state.keep_written())
    reporter = asyncio.create_task(fleet.report_forever())
    tasks = [asyncio.create_task(member.run(), name=member.identity) for member in fleet.members]
    try:
        await stop_event.wait()
        _done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_BUDGET)
        if pending:
            print(f"{len(pending)} charger(s) did not stop in time; closing them now")
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=1.5)
    finally:
        for task in (writer, reporter, *tasks):
            task.cancel()
        await asyncio.gather(writer, reporter, return_exceptions=True)
        for member in fleet.members:
            member.save_snapshot()
        # fleet_alive_at goes to null only on a graceful exit; a hard kill leaves its last one.
        fleet.state.write(alive=False if graceful_shutdown else None)
        print(fleet.summary_line(), flush=True)
    return {
        "sessions_started": fleet.sessions_started,
        "leftovers_closed": fleet.leftovers_closed,
    }


def _describe(exc):
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _close_quietly(websocket):
    try:
        await asyncio.wait_for(websocket.close(), timeout=CLOSE_TIMEOUT + 1)
    except Exception:
        pass


def _exit_immediately(signum, frame):
    """The second Ctrl+C: exit now. The state file already holds what the next run needs to
    close the interrupted sessions (§E.3 step 4)."""
    print("\nExiting immediately; the next run will close any interrupted sessions.", flush=True)
    os._exit(130)


async def main(args):
    try:
        chargers = load_manifest(args.manifest)
    except ManifestError as exc:
        print(exc)
        return 1
    if args.limit is not None:
        chargers = chargers[: args.limit]
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    except (NotImplementedError, AttributeError, RuntimeError):
        pass  # Windows: no SIGTERM handler through the event loop
    if args.duration_seconds is not None:
        loop.call_later(args.duration_seconds, stop_event.set)
    print(
        f"Starting {len(chargers)} demo chargers against {args.url} "
        f"({args.connect_rate:g} connections/s). Ctrl+C to stop."
    )
    fleet = asyncio.create_task(run_fleet(chargers, args, stop_event))
    try:
        await asyncio.wait([fleet])
    except asyncio.CancelledError:
        # asyncio.run turns the first Ctrl+C into a cancellation of this task. Undo it, and
        # shut down properly; a second Ctrl+C now exits on the spot.
        asyncio.current_task().uncancel()
        signal.signal(signal.SIGINT, _exit_immediately)
        print("\nStopping: ending sessions and switching chargers off (Ctrl+C again to quit).")
        stop_event.set()
        await asyncio.wait([fleet])
    fleet.result()
    return 0


def _positive(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0, got {value}")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="ws://localhost:9000", help="Central System URL")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="from seed_demo_fleet.py")
    parser.add_argument(
        "--state-file", default=DEFAULT_STATE_FILE, help="registers and open transactions"
    )
    parser.add_argument(
        "--connect-rate", type=_positive, default=10, help="new connections opened per second"
    )
    parser.add_argument(
        "--session-minutes", type=_positive, nargs=2, metavar=("MIN", "MAX"), default=[3, 8],
        help="length of a fleet-started session",
    )
    parser.add_argument(
        "--idle-minutes", type=_positive, nargs=2, metavar=("MIN", "MAX"), default=[5, 15],
        help="gap between sessions on one connector",
    )
    parser.add_argument(
        "--initial-busy", type=float, default=0.35,
        help="share of working connectors that begin already charging",
    )
    parser.add_argument(
        "--meter-interval", type=_positive, default=15,
        help="seconds between MeterValues during a session",
    )
    parser.add_argument(
        "--time-scale", type=_positive, default=1.0,
        help="multiplies every duration above, plus Finishing, but not the heartbeat",
    )
    parser.add_argument("--limit", type=int, default=None, help="only the first N chargers")
    parser.add_argument(
        "--duration-seconds", type=_positive, default=None,
        help="run this long, then shut down gracefully",
    )
    parser.add_argument("--seed", type=int, default=None, help="random seed, for a repeat run")
    args = parser.parse_args(argv)
    for name in ("session_minutes", "idle_minutes"):
        low, high = getattr(args, name)
        if low > high:
            parser.error(f"--{name.replace('_', '-')}: MIN {low:g} is greater than MAX {high:g}")
    if not 0 <= args.initial_busy <= 1:
        parser.error(f"--initial-busy must be between 0 and 1, got {args.initial_busy}")
    return args


if __name__ == "__main__":
    # Line-buffered output: the summary and failure lines should appear as they happen, also
    # when the console is a pipe.
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(asyncio.run(main(parse_args())))
