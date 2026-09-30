# 11 — Demo fleet: a simulated charger for every PlugShare station

Today the map shows 134 PlugShare sites (`09-plugshare-import.md`) as faint grey `unknown` pins,
because this system has never talked to a charger at any of them. This file turns every one of
those sites into a **simulated** site: one simulated OCPP charger for each PlugShare "station" and
one connector for each PlugShare "outlet". A single fleet process connects all of them to the
Central System and drives realistic charging sessions. The map then shows live colours, pins
change without a page reload, and the station panel shows each connector's plug type and rated kW
as PlugShare reported it.

This is **demo data**. The map will show a live status for real, physical chargers that this system
has never connected to. Everything this file creates is marked so that `12-remove-demo-fleet.md`
can remove all of it and restore the state before this file. Keep that marking intact: never
create a demo document that the removal cannot find.

**Depends on:** 04 (normal charge flow), 08 (public API), 09 (the 134 imported sites must already
exist) and 10 (the frontend). It does not depend on 06's unfinished sections I/J.

## Decisions already made (do not reopen)

| Question | Decision |
|---|---|
| How is a converted site marked? | New `SiteSource.simulated`. It behaves exactly like `operator` for status and charger counting. Removal reverts it to `external_reference`. |
| How is a mock charger marked? | New `ChargePoint.simulated = True`. Identity is `PS-<PlugShare station id>`. |
| How does status reach the map? | Only through the real path: the charger sends OCPP messages, then `main.py`, then Redis, then the API, then the browser. Nothing writes connector status into MongoDB directly. |
| Out-of-order outlets | PlugShare `status: "OUTOFORDER"` means the connector reports `Faulted` for the whole run. |
| kW | Taken from PlugShare `kilowatts`, where present (75 of 255 outlets). **Never guessed.** A connector without it shows its type and AC/DC only. |
| Where keys live | A new manifest file, `demo_fleet_manifest.json` (gitignored). `charge_point_credentials.json` and CP001 are never touched. |

## What is actually in the data

Checked directly against `montenegro_only.json`. These counts exclude the one `coming_soon` entry,
which 09 skips and this file skips too.

- **134 locations, 195 stations, 255 outlets.** A PlugShare location is our `Site`, a station is
  a `ChargePoint`, and an outlet is a connector.
- Stations per location: 1 (89 locations), 2 (36), 3 (6), 4 (3), 5 (1). 46 sites have two or more
  chargers.
- Outlets per station: 1 (147 stations), 2 (37), 3 (10), 4 (1). 48 chargers need more than one
  connector, so multi-connector support (§D) is mandatory.
- Station ids and outlet ids are unique across the file. The longest station id has 7 digits.
- `outlets[].connector` is a PlugShare numeric code. 07 §A forbids storing third-party codes, so
  translate it with this table. Every row was verified: translating every outlet at a location
  reproduces that location's own `connector_types` list exactly, for all 134 locations.

  | Code | `connector_type` | Outlets | Always |
  |---|---|---|---|
  | 7 | `Type 2` | 187 | AC |
  | 20 | `CCS2` | 40 | DC |
  | 3 | `CHAdeMO` | 11 | DC |
  | 10 | `Wall (Euro)` | 11 | AC |
  | 14 | `Three Phase` | 4 | AC |
  | 15 | `Caravan Mains Socket` | 1 | AC |
  | 2 | `J-1772` | 1 | AC |

  An unknown code MUST abort the seed with the code and the station id in the error message. Never
  guess a type.
- `power_type` (`"AC"`/`"DC"`) agrees with `is_dc` on every outlet. Use `power_type`.
- `kilowatts` is present on 75 outlets at 34 locations and `null` on the other 180. Values: 11, 15.36,
  22, 30, 40, 43, 50, 120, 150, 200. `power` is `0` on every outlet. Discard it.
- `status` is `"OUTOFORDER"` on 22 outlets, `"UNKNOWN"` on 8 and `null` on 225. The 22 out-of-order
  outlets cover **exactly 11 stations at exactly 10 locations**, and at each of those locations
  every outlet is out of order. No location mixes broken and working outlets. Treat `"UNKNOWN"`
  and `null` as working.
- `stations[].network_id` (34 stations) is discarded.

## A. Data model (`models.py`)

1. `SiteSource.simulated = "simulated"`. Update the enum docstring and the `Site` docstring. A
   simulated site is a real place whose chargers are simulated for a demo. It is created only by
   `seed_demo_fleet.py` and reverted only by `remove_demo_fleet.py`.
2. `PowerType(StrEnum)` with `AC = "AC"` and `DC = "DC"`. OCPP 1.6 has no such enum, so define it
   here the same way `SiteType` is defined.
3. `ConnectorSpec`, a plain pydantic `BaseModel` embedded in the charger document (not its own
   `Document`), with these fields:
   - `connector_id: int`, which must be ≥ 1
   - `connector_type: str`
   - `power_type: PowerType`
   - `max_power_kw: float | None = None`

   This describes the hardware. It is not status. OCPP 1.6 never reports plug type or rating, so
   this is the only place the system knows them.
4. On `ChargePoint`, add:
   - `simulated: bool = False`
   - `connector_specs: list[ConnectorSpec] = Field(default_factory=list)`

   Existing documents load unchanged through the defaults. CP001 keeps `simulated=False` and no
   specs.

## B. PlugShare → charger mapping

For each non-`coming_soon` location, and for each station in it, in array order:

- **Identity:** `PS-<station id>`, for example `PS-2946795`.
- **Connectors:** outlet *i* (0-based, in `outlets[]` order) becomes `connector_id = i + 1`. OCPP
  1.6 numbers a charger's connectors from 1 upward, with 0 reserved for the charger as a whole.
  - `connector_type` comes from the table above.
  - `power_type` comes from `power_type`.
  - `max_power_kw` is `kilowatts` as-is: `15.36` stays `15.36` and `null` stays `None`.
  - `out_of_order` is `status == "OUTOFORDER"`. It goes in the manifest only. It is not stored in
    MongoDB, following 09's rule for `under_repair`.
- **idTag per connector:** `DEMO-<station id>-<connector id>`, for example `DEMO-2946795-1`, with
  `parent_id_tag = "DEMO-FLEET"`.
  - Why one per connector: `IdTag.authorize` answers `ConcurrentTx` for a tag that already has an
    open transaction. A shared tag would block every connector after the first one.
  - The longest tag is 14 characters. OCPP's `IdToken` is `CiString20`, so abort if a future
    export would exceed 20.
- **One extra idTag, `DEMO-REMOTE`** (same parent), for demonstrating `operate.py remote-start` by
  hand. It is not used by the fleet.

## C. `seed_demo_fleet.py`

A new top-level operator script, in the same style as `seed.py` and `import_plugshare_sites.py`.
It uses `argparse` with long options and exits with a clear message on `PyMongoError`. Put the
entry point behind `if __name__ == "__main__":` as `import_plugshare_sites.py` already does, so the
tests can import it.

```
python seed_demo_fleet.py [--file montenegro_only.json] [--manifest demo_fleet_manifest.json] [--dry-run]
```

1. **Validate everything before writing anything.**
   - Parse the file and build the full mapping from §B.
   - Load every `Site` whose `external_id` matches a location.
   - Abort with no writes if any of the following is true:
     - any location has no `Site`. Tell the user to run `import_plugshare_sites.py` first.
     - any code is unknown.
     - any idTag would exceed 20 characters.
     - a `PS-…` identity already belongs to a charger with `simulated=False`. Never take over a
       real charger.
2. **Sites.** A `Site` whose `source` is `operator` is **skipped and reported**
   (`skipped (operator site): <name>`), together with its stations. An operator site is a real one
   and is never converted. Every other matched site is `set` to `source=simulated`. Leave
   `site_type`, `external_charge_point_count` and every other field untouched.
3. **Chargers.**
   - **New identity:** use `ChargePoint.register(identity, registration_status=accepted,
     simulated=True, site_id=…, connector_specs=…)`. The key goes into the manifest. This is Route
     A, as in `seed.py`: the key counts as installed at the factory.
   - **Existing simulated identity:** `set` `site_id` and `connector_specs`. Keep its key when the
     manifest has one for it and it verifies (`verify_authorization_key`). Otherwise rotate it
     with `rotate_authorization_key()` and write the new key.
   - Re-running the script must rotate **zero** keys when the manifest is intact.
   - Scrypt costs about 30 ms per hash on the development machine, so the first run spends roughly
     6 s hashing. That is fine.
4. **idTags.** Insert each §B tag that doesn't exist yet (status Accepted, parent `DEMO-FLEET`).
   Leave existing ones alone.
5. **Manifest.**
   - Write it atomically: write a temp file in the same directory, then `os.replace`.
   - Use UTF-8 with 2-space indent. It is the only copy of the keys, as
     `charge_point_credentials.json` is for `seed.py`.
   - Shape:

     ```json
     {
       "generated_at": "2026-09-29T10:00:00+00:00",
       "source_file": "montenegro_only.json",
       "chargers": [
         {
           "identity": "PS-2946795",
           "authorization_key": "<40 hex chars>",
           "site_name": "kolasin 1600",
           "connectors": [
             {"connector_id": 1, "connector_type": "Type 2", "power_type": "AC",
              "max_power_kw": null, "out_of_order": false, "id_tag": "DEMO-2946795-1"}
           ]
         }
       ]
     }
     ```
6. **Report** these counts:
   - sites converted
   - sites skipped
   - chargers created, updated, and keys rotated
   - connectors (with kW, out of order)
   - idTags created

   `--dry-run` computes and prints the same report and writes nothing: no MongoDB writes and no
   manifest.

Re-running `import_plugshare_sites.py` afterwards MUST leave the sites `simulated`. Its update
path does not set `source` today, so keep it that way. An acceptance criterion checks this.

## D. `simulate_charge_point.py`: importable and multi-connector

1. **Make it importable.** Replace the module-level `raise SystemExit(...)` with the
   `if __name__ == "__main__":` guard. Add a comment with the same reasoning `main.py` gives: the
   fleet and the tests import `SimulatedChargePoint`. `python simulate_charge_point.py …` must
   behave exactly as before.
2. **Add `number_of_connectors=1` to `SimulatedChargePoint.__init__`.**
   - The `NumberOfConnectors` configuration entry becomes `(str(number_of_connectors), True)`.
   - `physical_connectors()` returns `{1..number_of_connectors} | known_connectors |
     {default_connector_id}`.
   - Add a `--number-of-connectors` CLI flag (default 1) and pass it through `run_connection`.
3. **Change `send_status(connector_id, status, error_code=…, info=None)`.** It passes `info` into
   `StatusNotification`. OCPP 1.6 allows up to 50 characters, and a `None` value must be left out
   of the payload rather than sent.
4. **Add `send_boot_statuses(faulted=frozenset())`.** After an Accepted boot it reports every
   physical connector in ascending order:
   - `Available`, or
   - `Faulted` with `ChargePointErrorCode.other_error` and `info="Out of order (PlugShare
     report)"`, for ids in `faulted`.

   Idle mode (`--idle-seconds`) calls it after an Accepted boot, so a hand-run multi-connector
   charger shows all of its connectors on the map. Real chargers report each connector after
   booting.
5. **Add `start_local_session(connector_id, id_tag) -> int | None`.** It moves the steps that
   `run_scenario` currently does inline into one method:
   1. `authorize(id_tag)`. If the answer isn't Accepted, return `None` without touching the
      connector.
   2. Report `Preparing`.
   3. Send `StartTransaction` with `meter_start` equal to this connector's current meter register
      (`meter_readings`, default 0).
   4. Record `open_transactions`, `transaction_started_at` and `meter_readings`.
   5. If `StartTransaction.conf`'s idTagInfo isn't Accepted, finish the transaction with
      `Reason.de_authorized` and return `None`.
   6. Otherwise report `Charging` and return the transaction id.

   `run_scenario` keeps its own PASS/FAIL checks and does not need to use this method.
6. **Change remote start.**
   - When `RemoteStartTransaction` has no `connectorId`, choose the lowest-numbered physical
     connector whose local status is `Available` and which is not busy. Fall back to
     `default_connector_id` as today.
   - Record the choice in `self.pending_remote_starts[id_tag] = connector_id` in
     `on_remote_start_transaction`, and pop it in `after_remote_start_transaction`, so both halves
     use the same connector.
   - `meter_start` becomes the connector's register instead of `0`.
7. **Change remote stop.** `after_remote_stop_transaction`'s `meter_stop` becomes the connector's
   register instead of `0`. Otherwise a remotely stopped fleet session reports negative energy.
8. **Add `busy_connectors()`.** It returns the connectors with an open transaction plus those in
   `pending_remote_starts`. The fleet never starts a session on one of these.
9. **Rename `_send_meter_values` to `send_meter_values`**, since the fleet calls it, and update
   the internal callers.

Keep every existing flag and behaviour. The hand-run flows in 04 and 06 must still pass.

## E. `run_demo_fleet.py`: the fleet runner

One process and one asyncio loop, with one WebSocket connection per charger in the manifest. It is
a charger simulator, so **it never reads or writes MongoDB**. Everything it knows comes from the
manifest and its own state file, just as a real charger knows only its own configuration and
flash storage.

Put the entry point behind the `__main__` guard. Make `parse_args(argv=None)` accept a list, and
expose `async def run_fleet(chargers, options, stop_event, graceful_shutdown=True)` so the tests
can drive it in-process.

After importing `simulate_charge_point`, which calls `logging.basicConfig(INFO)`, set
`logging.getLogger("ocpp").setLevel(logging.WARNING)`. Otherwise every OCPP frame from 195
chargers is logged.

### E.1 CLI

| Flag | Default | Meaning |
|---|---|---|
| `--url` | `ws://localhost:9000` | Central System |
| `--manifest` | `demo_fleet_manifest.json` | from §C |
| `--state-file` | `demo_fleet_state.json` | §E.5 |
| `--connect-rate` | `10` | new connections opened per second |
| `--session-minutes MIN MAX` | `3 8` | length of a fleet-started session |
| `--idle-minutes MIN MAX` | `5 15` | gap between sessions on one connector |
| `--initial-busy` | `0.35` | share of working connectors that begin already charging |
| `--meter-interval` | `15` | seconds between MeterValues during a session |
| `--time-scale` | `1.0` | multiplies every duration above, plus Preparing/Finishing, but not the heartbeat. Tests use about `0.01`. |
| `--limit` | none | only the first N chargers in the manifest |
| `--duration-seconds` | none | run this long, then shut down gracefully |
| `--seed` | none | `random.Random` seed, for a reproducible run |

With the defaults, a working connector is busy about 35% of the time. A site is `occupied` only
when **all** of its connectors are busy, because 08 §F's precedence puts `available` ahead of
`occupied`. So at any moment the map shows mostly green pins, a steady handful of amber ones
(mostly among the 51 single-connector sites), and the 10 red ones.

### E.2 Startup and ramp

- Load the manifest and the state file. Validate that the manifest has chargers.
- Start charger *i* after `i / connect_rate` seconds, so 195 chargers are all up in about 20 s.
- **Why the ramp is required:** `main.py` verifies each handshake's key with scrypt (about 30 ms)
  synchronously inside the event loop. 195 simultaneous handshakes would stall the Central System
  for about 6 s, and some would time out.
- Use `websockets.connect(..., open_timeout=30)` with the same subprotocol and
  `basic_auth_header` as the simulator.
- Do not change `main.py` to fix this. Moving scrypt to a thread would be a reasonable later
  improvement, but it is out of scope here.

### E.3 One charger's lifecycle

This loop runs until the stop event is set:

1. **Connect.** On `OSError`, `InvalidStatus` or `ConnectionClosed`, wait with backoff: 2 s,
   doubling to a 30 s cap, with ±20% jitter. Then retry. Print the first failure for each charger
   and suppress repeats. A Central System restart must leave the whole fleet reconnected within
   about 60 s, with nobody intervening.
2. **Create the charger object.** Build a `SimulatedChargePoint` with `number_of_connectors` from
   the manifest, `boot_vendor="DemoFleet"` and `boot_model="PlugShareMock"` (both are
   `CiString20`). Seed its `meter_readings` from the state file's registers, and start its
   `start()` listener task.
3. **Boot.** If the answer isn't Accepted, wait the `interval` from the conf (OCPP 1.6 s4.2) and
   boot again.
4. **Close leftovers.** For each transaction this charger still holds in the state file, send
   `StopTransaction` with:
   - `meter_stop` = that connector's register
   - `reason = Reason.power_loss`
   - no `idTag`

   **Why:** the process died, or the connection dropped, mid-session. From the Central System's
   side, the charger vanished and came back without the session, and `PowerLoss` is the closest
   defined reason.

   This step is also what prevents `ConcurrentTx`. If it were skipped, each leftover's idTag
   would stay attached to an open transaction forever. `main.py` already ignores a duplicate stop.
5. **Report statuses.** Call `send_boot_statuses(faulted=<the manifest's out-of-order
   connectors>)`.
6. **Run the background tasks.** Run the heartbeat task (every `interval` from the boot conf), the
   housekeeping task (§E.4) and one session task per working connector (§E.4). Faulted connectors
   get no session task and stay `Faulted`.
7. **Reconnect when the connection drops.** When the connection closes for any reason, including
   an operator `Reset`, which the simulator handles by closing, cancel this charger's tasks,
   snapshot its state (§E.5), and go back to step 1.

All status changes go through `send_status` or `finish_transaction`, so the simulator's local
connector mirror stays correct, and `ChangeAvailability`, `RemoteStop` and `TriggerMessage` keep
working against fleet chargers. Every sequence below is legal under OCPP 1.6 s4.9, as encoded in
`connector_state_machine.py`:

- `Available → Preparing → Charging → Finishing → Available`
- `→ Unavailable` at shutdown
- `Unavailable → Available/Faulted` at the next boot

### E.4 Sessions and housekeeping

**Session task, one per working connector, repeating:**

1. **Wait.**
   - **First pass only:** with probability `initial_busy`, wait a random 0–30 s (scaled) and start
     a session with a random duration from the session range. Otherwise, wait a random time
     between 0 and `idle max` before trying.
   - **Every later pass:** wait a random time from the idle range.
2. **Check.** If the connector's local status isn't `Available`, or it is in `busy_connectors()`,
   skip this round and go back to waiting. The connector could be Unavailable through an
   operator's `ChangeAvailability`, or busy with a remote start.
3. **Start.** Call `start_local_session(connector, its DEMO-… idTag)`. If it returns `None`, log
   that once per connector and wait 60 s (scaled) before the next attempt.
4. **Charge** until the chosen duration elapses.
5. **Stop.** Call `finish_transaction(connector, tx, meter_stop=register, id_tag=tag,
   reason=Reason.local)`, the driver ending the session at the charger. Leave `Finishing` to
   housekeeping.

Only sessions this task started are stopped by it. A session started with `operate.py
remote-start` runs until `operate.py remote-stop`.

**Housekeeping task, one per charger, ticking every second:**

- **Meter.** For every open transaction, whoever started it, advance the register every
  `meter_interval`. Add `rate_kw × elapsed_s / 3600 × 1000` Wh, where `rate_kw` is:
  - `max_power_kw` when known, otherwise 7.4 (AC) or 50 (DC)
  - multiplied by a random 0.6–1.0, chosen once per session

  Then call `send_meter_values(connector)`. This rate only shapes the Wh readings. It is never
  shown as the connector's rating.
- **Finishing.** A connector that has been in `Finishing` with no open transaction for its random
  5–15 s (scaled) goes back to `Available`. The unplug covers both fleet and remote sessions. The
  simulator alone would leave a remotely stopped connector in `Finishing`, and so amber, forever.
- **Snapshot.** When this charger's open transactions or registers changed, update the state file
  (§E.5).

### E.5 State file (`demo_fleet_state.json`, gitignored)

This stands in for each charger's flash storage, like the simulator writing a rotated key back to
its credentials file:

```json
{
  "fleet_alive_at": "2026-09-29T10:00:00+00:00",
  "chargers": {
    "PS-2946795": {"registers": {"1": 48213}, "open_transactions": {"1": 812}}
  }
}
```

- Build it from each charger's own `open_transactions` and `meter_readings`. Do not update it at
  individual call sites. That way transactions started or stopped by any path (fleet, remote,
  reset) are captured.
- Write it atomically (temp file plus `os.replace`), at most once per second across the whole
  fleet.
- **`fleet_alive_at`:**
  - Refresh it every 10 s while running.
  - Set it to `null` on graceful exit.
  - `remove_demo_fleet.py` (12) refuses to run while it is less than 30 s old.
- A missing or unreadable file means an empty state. Log a warning and do not crash.

### E.6 Shutdown

Trigger shutdown on any of:

- Ctrl+C
- SIGTERM (POSIX only)
- `--duration-seconds` running out

`asyncio.run` turns the first Ctrl+C into a cancellation of the main task. Catch it there and run
the shutdown. Use `loop.add_signal_handler` for SIGTERM where the platform supports it.

Graceful shutdown, bounded to 20 s in total:

1. Cancel the session and housekeeping tasks.
2. For every open transaction, call `finish_transaction(…, reason=Reason.other)`.
3. Report `Unavailable` for every connector that isn't `Faulted`. Leave `Faulted` ones as they
   are: an out-of-order charger is still out of order when the demo stops, and moving it would
   falsely close its `FaultEvent`.
4. Close the connections, write the final state file, and exit 0.

Results:

- **After a graceful stop:** the 124 working sites show grey `unavailable` (known and operated,
  but switched off), the 10 broken ones stay red, and no transaction is left open. This is the
  honest picture when nothing is connected.
- **A second Ctrl+C:** the process exits immediately. The next run cleans up in §E.3 step 4.
- **`graceful_shutdown=False`** (tests only): skip steps 2 and 3 and just close the connections.
  This simulates a hard kill.

### E.7 Output

Keep the console readable:

- Print one line per charger only on its first connection failure and on recovery.
- Every 30 s, print one summary line, for example:

  `connected 195/195 · charging 81 · available 138 · faulted 22 · unavailable 0 · sessions 146`

  The status counts are over connectors, taken from the simulator's local mirror, and `sessions`
  is the number started this run.

`main.py`'s own console will scroll quickly under this load, since it prints every message.
That's expected. Do not change its logging in this task.

## F. API changes (`api/`)

1. **`schemas.py`.** Add these to `ConnectorOut`, all defaulting to `None`:
   - `connector_type: str | None`
   - `power_type: str | None`
   - `max_power_kw: float | None`

   Document that `source` can now also be `"simulated"`.
2. **`queries.py`.**
   - Change `_connector_outs(records, specs)` to merge each connector's `ConnectorSpec`, matched by
     `connector_id`.
     - A connector with a spec but no `ConnectorStatus` row yet (the charger has never reported
       it) is still listed, with `status="Unavailable"` and `error_code="NoError"`. This matches
       the rule that a site with no reported connectors aggregates to `unavailable`.
     - A connector with a status row but no spec (every real charger, CP001 included) is listed
       as today, with the three new fields `None`.
   - Use it in `_charge_points_in` and `charge_point_detail`.
3. **`site_detail`.** Load members for every source except `external_reference`, instead of only
   `== operator`. `list_sites` and `aggregate_status` already treat anything but
   `external_reference` as live. Add a test so that stays true.
4. **`routes_ws.py`.** Nothing to change. It refuses only `external_reference`, so simulated sites
   get their live WebSocket. Cover this with a test.

## G. Frontend changes (`charger-fe/`, the Next.js app from 10)

The app has its own repository next to this one. Read its `AGENTS.md` first: this Next.js version
differs from what you may expect.

1. **`lib/types.ts`.**
   - `SiteSource` gains `"simulated"`.
   - `ConnectorOut` gains `connector_type`, `power_type` and `max_power_kw`, all nullable.
2. **`lib/useStationLiveStatus.ts`. This is a bug fix and it is required.**
   - Live state is currently keyed by `connector_id` alone. On a site with several chargers, every
     charger has a connector 1, so an event from one charger overwrites the others. 46 sites have
     two or more chargers.
   - Key the state by `` `${charge_point_identity}:${connector_id}` ``.
   - Ignore any frame without a `type`. The first frame is 08 §G's `SiteDetail` snapshot, not an
     event.
3. **`components/StationPanel.tsx`.**
   - **Charger list:** render every charger's connectors directly, without the accordion. The
     largest site has 5 chargers and 5 connectors, and at this size collapsing only hides the
     live changes the demo exists to show. Title each charger "Charger N", numbered in the API's
     order (sorted by identity), with the identity in small grey text beside it.
   - **Connector row:**
     - label: `connector_type · <kW> power_type`, for example `CCS2 · 200 kW DC`.
     - no kW: `Type 2 · AC`.
     - no spec at all: `Connector N`, as today.
     - Format kW with no decimals when whole (`22 kW`), otherwise one decimal (`15.4 kW`).
   - **Status text:** colour it with a new `connectorStatusColor(status)` in
     `lib/statusColors.ts`. It maps an OCPP status onto the existing `STATUS_COLORS`:
     - `Available` → available
     - `Preparing`, `Charging`, `SuspendedEV`, `SuspendedEVSE` and `Finishing` → occupied (the
       same `IN_SESSION` set as `api/queries.py`)
     - `Reserved` → reserved
     - `Faulted` → faulted
     - anything else → unavailable
   - **Header:** under the address, show "Up to <max> kW" when any connector has a kW value.
   - **Demo note:** when `source === "simulated"`, show one small grey line at the bottom: "Demo
     data — simulated chargers." It is the honest label for a demo. Removing it later is one line.
4. **`components/MapView.tsx`. Refresh the pins; this is required.**
   - Today pins keep their page-load colour until a reload.
   - After the map's `load` event, every 10 s while `document.visibilityState === "visible"`, call
     `listSites()` and pass the result to `getSource("sites").setData(toFeatureCollection(sites))`.
     10 F.2 already chose `setData` for this.
   - Keep the old data silently when a fetch fails.
   - Clear the interval on unmount.
   - This calls the API from the browser, so `CORS_ORIGINS` must include the frontend origin.
     It already does in `.env`.
5. **Playwright (SHOULD).** Add a test to `e2e/` that assumes the fleet is running:
   - Open "Rest stop Pelev Brijeg" (PlugShare 1351739).
   - Expect four chargers, each labelled `CCS2 · 200 kW DC`.
   - Expect the demo note.

## H. Housekeeping

- `.gitignore` (LF endings): add `demo_fleet_manifest.json` and `demo_fleet_state.json`.
- `README.md`: add a "Demo fleet" section containing §J's command order.
- `instructions/07-public-map-platform.md` §A's `source` row and `08-public-api-service.md` §F: add
  one sentence each saying that `simulated` exists, behaves like `operator`, and is defined here.

## I. Testing

Use this project's real-infrastructure convention: no mocks, the `db` and `server` fixtures from
`tests/conftest.py`, and skip when MongoDB is absent.

The test database is shared across the session, so use unique PlugShare ids and identities in
every test. Never assert global counts.

- **`tests/test_demo_fleet_seed.py`** uses small inline synthetic exports and a manifest under
  `tmp_path`. It covers:
  - a 2-station site and a 3-outlet station (connector ids 1..3)
  - code 20 becomes `CCS2`/DC with `max_power_kw` kept, and `null` kW stays `None`
  - an out-of-order outlet appears in the manifest as `out_of_order: true`
  - one idTag per connector plus `DEMO-REMOTE`
  - an unknown code aborts with no writes
  - a missing `Site` aborts with no writes
  - an `operator` site is skipped and left untouched
  - an identity already owned by a non-simulated charger aborts
  - running twice creates nothing new and rotates zero keys
  - running the import afterwards leaves `source=simulated`
- **`tests/test_demo_fleet_api.py`** covers:
  - a simulated site aggregates live status (not `unknown`) and lists its chargers
  - connector specs are merged into `ConnectorOut`
  - a spec-only connector is listed as `Unavailable`
  - CP001-style chargers without specs are unchanged
  - `WS /api/v1/ws/sites/{id}` accepts a simulated site
- **`tests/test_demo_fleet_run.py`** runs `run_fleet` in-process against `server`, with
  `--time-scale 0.01`, seeding a synthetic site first. It covers:
  - every connector gets a `ConnectorStatus` row
  - the out-of-order connector is `Faulted`
  - at least one transaction completes with `meter_stop > meter_start`
  - after graceful shutdown, no transaction is open and the working connectors are `Unavailable`
  - after a `graceful_shutdown=False` run with `initial_busy=1.0`, a second run closes the
    leftover with `stop_reason` PowerLoss and sessions then resume
- **`SimulatedChargePoint` units:** with `number_of_connectors=3`, `physical_connectors()` is
  `{1, 2, 3}` and `NumberOfConnectors` is `"3"`. Construct it with a dummy connection object;
  `__init__` never touches it.
- **Frontend:** `npm run lint` and `npm run build` pass in `charger-fe/`.

## J. Running the demo

Run these in order, from `chargers/` unless noted. MongoDB and Redis must be running, and `.env`
must be filled in (`REDIS_URL`, `CORS_ORIGINS`, `ADMIN_TOKEN`).

```
python main.py                                            # terminal 1
uvicorn api.app:app --host 0.0.0.0 --port 8000            # terminal 2
python import_plugshare_sites.py                          # once; skip if already imported
python seed_demo_fleet.py                                 # once; safe to re-run
python run_demo_fleet.py                                  # terminal 3; Ctrl+C to stop
cd ../charger-fe && npm run dev                           # terminal 4, then open :3000
```

## Acceptance criteria

1. `python seed_demo_fleet.py --dry-run` reports:
   - 134 sites to convert, 0 skipped
   - 195 chargers to create
   - 255 connectors (75 with kW, 22 out of order)
   - 256 idTags

   It writes nothing.
2. The real run leaves these counts in MongoDB:
   - `ChargePoint{simulated: true}`: 195
   - `Site{source: "simulated"}`: 134
   - `IdTag{parent_id_tag: "DEMO-FLEET"}`: 256

   A second run creates 0 documents and rotates 0 keys.
3. Re-running `python import_plugshare_sites.py` reports 134 updated, and all 134 sites stay
   `simulated`.
4. `python run_demo_fleet.py` prints `connected 195/195` within 60 s.
5. With the fleet running, `GET /api/v1/sites` returns:
   - 0 sites with `unknown`
   - exactly 10 with `faulted` (the all-out-of-order locations, e.g. Vranjina, PlugShare 286566)
   - at least 50 with `available` and at least 1 with `occupied`
   - Hotel Budva `unavailable`, as before
6. `GET /api/v1/sites/{id}` returns:
   - "Rest stop Pelev Brijeg" (PlugShare 1351739): 4 chargers, each with one `CCS2`/`DC` connector
     at 200 kW
   - "Porto Montenegro" (PlugShare 1156615; the name appears twice, so use the id): 2 × `CCS2`
     150 kW
   - "kolasin 1600" (PlugShare 1534921): one charger, `PS-2946795`, with one `Type 2`/`AC`
     connector and `max_power_kw: null`
7. Within 10 minutes of starting, at least 50 closed transactions exist for `PS-…` chargers.
   Every one has `meter_stop ≥ meter_start`.
8. **In the browser, with no reload:**
   - pins change colour within about 15 s of a status change
   - Pelev Brijeg's panel shows `CCS2 · 200 kW DC` four times, plus the demo note
   - on GreenCar.me (PlugShare 1037505, 5 chargers) each charger shows its own independent status
9. **Remote start and stop:**
   - `python operate.py remote-start PS-2946795 DEMO-REMOTE` takes the connector to `Charging`,
     and meter values flow.
   - `remote-stop` with that transaction id returns it through `Finishing` to `Available` within
     15 s.
10. **Ctrl+C and restart:**
    - Ctrl+C exits within 20 s.
    - Afterwards, 0 transactions are open for `PS-…` chargers, and the map shows 124 grey and 10
      red simulated sites.
    - Starting the fleet again reconnects everything with no `ConcurrentTx` or authorization
      failures.
11. **Hard kill and restart:** after `kill -9` of the fleet, the next start closes every
    interrupted transaction with stop reason `PowerLoss`, and sessions resume.
12. **Restarting `main.py`** while the fleet runs leaves all 195 chargers reconnected within 60 s,
    with nobody intervening.
13. CP001, its site, Hotel Budva and `charge_point_credentials.json` are byte-for-byte and
    document-for-document unchanged.
14. `python -m pytest` passes. `npm run lint` and `npm run build` pass in `charger-fe/`.

## When you are done

Write the completion brief specified in `README.md#report-when-you-finish`. The use case is a
demo. Someone opens the map in front of an audience. Instead of 134 faint, grey "we know nothing"
pins, they see a country of chargers that are free, busy or broken, updating as they watch. They
click one and see what plugs it has and how fast it charges. State plainly in the brief that the
statuses are simulated, and point to 12 for removal.
