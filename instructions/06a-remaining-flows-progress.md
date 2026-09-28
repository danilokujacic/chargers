# 06a — Progress on remaining flows (read before continuing 06)

`06-remaining-flows.md` covers ten sub-flows, A through J, and says to do them in order. This
file is a handoff note, not a task: it records exactly what is done, what depends on it, and
what to pick up next. Read this first, then jump to the relevant heading in
`06-remaining-flows.md` -- do not redo section A.

## Done: section A (Remote start and stop)

Fully implemented, tested, and verified against a live server and MongoDB. Do not rebuild any
of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `CONNECTED_CHARGE_POINTS` registry (pre-existing from 03); `ADMIN_PATH_PREFIX`, `ADMIN_TOKEN_ENV_VAR`, `handle_admin_request()`, `admin_json_response()` -- the admin HTTP API; `send_remote_start_transaction()` / `send_remote_stop_transaction()`, now validating `connectorId != 0` |
| `operate.py` | New. Operator CLI: `remote-start <identity> <id_tag> [--connector-id N]`, `remote-stop <identity> <transaction_id>`. Talks to the admin API over plain HTTP via stdlib `urllib` |
| `simulate_charge_point.py` | `SimulatedChargePoint` now answers `RemoteStartTransaction`/`RemoteStopTransaction`, both `AuthorizeRemoteTxRequests` modes; `--idle-seconds N`, `--authorize-remote-tx-requests` / `--no-authorize-remote-tx-requests`, `--reject-remote-start` |
| `tests/test_remote_start_stop.py` | New. 13 tests | 

Full suite: `python -m pytest` → **30 passed** (17 from 05 + 13 from this).

### The one architectural decision every later section should reuse

06's own text ("Add operator CLI commands ... to trigger these against a connected charger")
assumes the CLI can reach the live-connection registry, but `CONNECTED_CHARGE_POINTS` lives in
`main.py`'s process memory and any CLI script is a **separate OS process** -- there is no way
for it to read another process's memory. The fix, now in place and proven to work:

- `main.py`'s `process_request` hook (already used for charger Basic-auth) also routes any
  path under `/admin/` to `handle_admin_request()`, answering plain HTTP GET with JSON. This
  works because `websockets` 16.1.1 happily serves non-upgrade HTTP responses from the same
  listening socket -- confirmed with real separate-process `curl` calls; a block-on-urllib
  call made *from inside* the server's own process/event loop deadlocks it, so don't do that.
- Only GET is available: the library's HTTP/1.1 parser rejects any other method before
  `process_request` ever runs, so parameters are always in the query string, never a body.
- Gated by a single shared secret, `ADMIN_TOKEN` (env var, checked per-request so no restart is
  needed to pick up a change). Unset → every `/admin/*` path answers 404, not 200-with-no-auth.
- `operate.py` is a synchronous script using stdlib `urllib.request` against that API.

**Every later section that needs an operator command** (Change Availability in B, Reset/
UnlockConnector in C, ReserveNow/CancelReservation in E, TriggerMessage in J, …) should add a
new `/admin/<verb>` case to `handle_admin_request()` and a new subcommand to `operate.py`,
rather than inventing a second mechanism.

### Other reusable lessons from this pass

- **A connection race, easy to reintroduce.** `on_connect` in `main.py` populates
  `CONNECTED_CHARGE_POINTS[cp_id]` *before* calling `await charge_point.start()`. The
  client-side WebSocket handshake completing does **not** mean that has happened yet -- there's
  a real gap. Tests must await one genuine round trip (e.g. a `Heartbeat`) before assuming the
  registry is populated; a `sleep`/poll loop is the wrong fix and the doc05 tests rule against
  it for exactly this reason.
- **`@after` hooks, not bare `asyncio.create_task`, for anything that must follow a conf onto
  the wire.** Reused again here for `RemoteStartTransaction`/`RemoteStopTransaction` on both
  the simulator and the test double, following the same pattern `03`'s commissioning code and
  `after_boot` already established.
- **Simulator idle/reactive tooling exists now**: `--idle-seconds` is a generic "connect, boot,
  then wait to be acted on" mode, not specific to remote-start. Later sections needing a
  charger that sits and reacts to a Central-System-initiated message (ChangeAvailability in B,
  Reset in C, ReserveNow in E, TriggerMessage in J) can reuse this flag rather than adding
  another one-off waiting mode.
- **Test doubles must implement the full documented charger behaviour, not a shortcut.** The
  first version of `RemoteControllableChargePoint` in the test file skipped the
  `StatusNotification(Charging)` / `StatusNotification(Finishing)` steps a real charger sends
  around start/stop, and a test caught it. Section B's `ChangeAvailability` doc text explicitly
  requires "After the change takes effect the charger sends StatusNotification with the new
  status" -- don't let a test double skip that either.

## Done: section B (Availability management)

Fully implemented, tested (`tests/test_change_availability.py`, 8 tests), and verified against
a live server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py` pair. Do not
rebuild any of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `send_change_availability()` (CS -> charger, like section A's remote start/stop); `reapply_persisted_availability()`, called from `after_boot` on every non-Pending boot; `/admin/change-availability` case in `handle_admin_request()` |
| `operate.py` | New `change-availability <identity> <connector_id> <type>` subcommand |
| `models.py` | `ConnectorStatus` gained `desired_availability` and `availability_change_scheduled`, plus `set_desired_availability()`; `apply()` now clears the scheduled flag once the deferred change actually lands |
| `simulate_charge_point.py` | Answers `ChangeAvailability` (Accepted/Scheduled, connectorId 0 broadcast, deferral until an open transaction ends); refactored every `StatusNotification` send through a new `send_status()` helper that keeps a local `connector_state_machine.ConnectorState` mirror, and every transaction-stop path through a new `finish_transaction()` helper, so deferred availability changes apply at the right moment regardless of which path ended the transaction |
| `tests/test_change_availability.py` | New. 8 tests |

Full suite: `python -m pytest` -> **38 passed** (30 from section A + 8 from this), run 5 times in
a row with no flakiness once the race below was fixed.

### The one correctness bug this section's testing found (not specific to availability)

`ConnectorStatus.get_or_create` did find-then-insert with no protection against two callers
racing to create the SAME connector's first-ever document -- which section B is the first flow
to actually trigger, because `send_change_availability`'s own persistence of
`desired_availability` and the resulting `StatusNotification`'s `apply()` (from the charger's
`@after` hook, an independent background task per `ocpp.routing.after`) can both reach a brand
new `(charge_point_identity, connector_id)` pair at the same time. Two fixes, both now in
`models.py`, both worth reusing for any future document with a similar shape:

- `get_or_create` catches `DuplicateKeyError` on the losing insert and re-fetches, rather than
  letting it surface as an `InternalError` CALLError.
- `apply()` and `set_desired_availability()` write with `Document.set({...})` (an atomic
  MongoDB `$set` on only their own fields), not mutate-then-`save()`. Two in-memory copies of
  the same document, each loaded at a slightly different moment and each calling `.save()`,
  otherwise let whichever finishes last silently discard the other's fields -- this reproduced
  as a real, seen-in-this-session test failure (a connector's `status` reverting from
  `Unavailable` back to `Available`), not a theoretical concern.

### Other reusable lessons from this pass

- **A charger's own memory cannot be trusted after a reconnect.** OCPP 1.6 s5.2's "Connector
  set to Unavailable shall persist a reboot" is a requirement on the CHARGER, but this project's
  simulator is a fresh OS process on every run with no disk state of its own. `main.py` cannot
  tell a charger that remembers from one that does not, so `reapply_persisted_availability`
  resends `ChangeAvailability` unconditionally on every boot when `desired_availability` is
  Inoperative, never conditioned on this Central System's own last-known `status` -- that value
  describes what THIS SYSTEM last heard, not what the reconnecting charger actually kept.
  ChangeAvailability is idempotent for a charger that does remember, so the redundant send costs
  nothing.
- **Not every status has a path to Unavailable.** OCPP 1.6 s4.9's table has A8/C8/D8/E8/F8/G8 ->
  Unavailable but deliberately no B8 (Preparing has none): a connector plugged in but not yet
  transacting cannot go straight to Unavailable. `simulate_charge_point.py`'s
  `_availability_blocked()` checks `ConnectorState.can_change_to()`, not just "is there an open
  transaction", specifically so this case defers instead of raising `IllegalTransition`.
- **`Document.set()` beats mutate-then-`save()` whenever two code paths might touch the same
  document independently** -- see the bug above. `Transaction` and `ChargePoint`'s existing
  methods all still use mutate-then-`save()`; that remains fine for them only because nothing
  else writes those documents concurrently. A future section adding a second writer to an
  existing document (e.g. G's fault history alongside `on_status`) should check this first.

## Done: section C (Reset and unlock)

Fully implemented, tested (`tests/test_reset_and_unlock.py`, 11 tests), and verified against a
live server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py` pair,
including watching it actually reconnect and re-boot after a reset. Do not rebuild any of this;
extend it.

| File | What it holds |
|---|---|
| `main.py` | `send_reset()` and `send_unlock_connector()` (CS -> charger, following section A/B's pattern); `/admin/reset` and `/admin/unlock-connector` cases in `handle_admin_request()` |
| `operate.py` | New `reset <identity> <Soft\|Hard>` and `unlock-connector <identity> <connector_id>` subcommands |
| `simulate_charge_point.py` | Answers `Reset` (Soft gracefully stops any open transaction with `Reason.soft_reset` first; Hard abandons it, simulating real power loss) and `UnlockConnector`; `after_reset` closes its own connection to simulate the reboot, and `main()` was restructured into `run_connection()` + an outer loop so it actually reconnects and re-sends `BootNotification` afterward, rather than just exiting |
| `tests/test_reset_and_unlock.py` | New. 11 tests |

Full suite: `python -m pytest` -> **49 passed** (38 from sections A+B + 11 from this), run 5
times in a row with no flakiness.

### Key decision: Reset/UnlockConnector do not need any new state on the Central System side

Unlike B, this section added no new persisted fields. OCPP 1.6 s4.2.1's "commissioning flow
runs again from BootNotification" is already exactly what `on_boot`/`after_boot` do on **every**
boot, unconditionally -- there was nothing to special-case for "this boot follows a reset". The
same is true of a connector's status surviving a reset ungracefully (e.g. reporting `Available`
directly after reboot with no legal transition from whatever it was mid-session): the existing
`IllegalTransition` -> `force_status` fallback in `main.py`'s `on_status` (built for doc01/04)
already covers it. If a future section is tempted to add reset-specific bookkeeping, check
whether it's actually needed first -- it often isn't, by design.

### Other reusable lessons from this pass

- **Soft vs. Hard is a policy difference, not a protocol one.** Both use the same `Reset.req`;
  the difference is entirely in what the CHARGER does before disconnecting. This project's
  simulator models Soft as "gracefully stop every open transaction with `Reason.soft_reset`,
  then disconnect" and Hard as "disconnect immediately, no stop" -- matching the instruction
  file's "expect a StopTransaction after the reboot" (possibly late, possibly never for Hard).
- **`ocpp.routing`'s `@after` hook is also how a charger-side (client) handler defers work past
  its own `conf`, not just a Central-System (server) one.** `after_reset` closing the
  connection only after `Reset.conf` has actually been sent, via the same `@after` pattern
  `main.py` uses for `after_boot`/`after_change_availability`, is what let this be tested
  deterministically (`await reset_applied.wait()`) instead of racing a bare disconnect.
- **`simulate_charge_point.py`'s `main()` needed restructuring to reconnect at all** -- it was
  previously a single `async with websockets.connect(...)` block with no retry. It is now
  `run_connection()` (one connection's worth of work) called from a `while True` loop in
  `main()`, which reconnects when `charge_point.reset_requested` comes back set. Any later
  section needing the simulator to reconnect (none currently do) should reuse this loop rather
  than adding a second one.

## Done: section D (Configuration)

Fully implemented, tested (`tests/test_configuration.py`, 15 tests), and verified against a
live server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py` pair. Do not
rebuild any of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `send_get_configuration()`, `send_change_configuration()`, `send_clear_cache()` (CS -> charger); `on_data_transfer()` on `MyChargePoint` (charger -> CS, answers `UnknownVendorId`); `/admin/get-configuration`, `/admin/change-configuration`, `/admin/clear-cache` cases |
| `operate.py` | New `get-configuration`, `change-configuration`, `clear-cache` subcommands |
| `models.py` | New `ConfigurationEntry` document (`charge_point_identity` + `key` unique), written via an atomic upsert on the raw collection rather than beanie's `Document.save()` -- see the note below |
| `simulate_charge_point.py` | `on_change_configuration` generalised: `AuthorizationKey` keeps its own dedicated, security-sensitive branch; every other key now goes through a small `self.configuration` dict (`HeartbeatInterval` writable + `RebootRequired`, `NumberOfConnectors` readonly, both examples); new `on_get_configuration`/`on_clear_cache` handlers |
| `tests/test_configuration.py` | New. 15 tests |

Full suite: `python -m pytest` -> **64 passed** (49 from sections A+B+C + 15 from this), run 5
times in a row with no flakiness.

### Key decision: configuration reads/writes are the one thing allowed while Pending

06's cross-cutting requirement #3 ("Nothing Central-System-initiated while Pending except
configuration reads and writes") is explicit and was taken literally: `send_get_configuration`
and `send_change_configuration` do **not** call `ensure_commissioned`, unlike every other
`send_*` function in main.py (remote start/stop, ChangeAvailability, Reset, UnlockConnector,
`send_clear_cache`). Tested directly
(`test_get_configuration_and_change_configuration_allowed_while_pending`). `ClearCache` is
**not** exempted -- it is not "reading or writing configuration" -- and follows the same
Pending-blocks-it default as the others.

### The security rule got enforced at the Central System, not trusted to the charger

OCPP-J 1.6 s6.2.2 only says a charger "should not give back the authorization key" -- a SHOULD,
not a MUST, and nothing stops a buggy or malicious charger from doing it anyway.
`send_get_configuration` in `main.py` unconditionally blanks the value for the
`AuthorizationKey` key before it ever reaches `ConfigurationEntry.upsert`, regardless of what
the charger's `GetConfiguration.conf` actually contained. `test_configuration.py` proves this by
having its test double **misbehave on purpose** (report a real-looking value for that key) and
asserting the stored document still has `value=None`. Don't relax this to "trust the charger
filtered it" in a later section.

### Other reusable lessons from this pass

- **A new document written by many small updates (one row per config key, potentially from
  overlapping `GetConfiguration` sweeps or a `ChangeConfiguration` landing mid-sweep) gets the
  same atomic-`$set`-on-raw-collection treatment `ConnectorStatus` needed in section B**, applied
  here from the start rather than discovered by a flaky test a second time. `ConfigurationEntry.
  upsert()` never does beanie's own find-then-insert/save.
- **Not every OCPP message that touches "configuration" is CS-initiated.** `DataTransfer` is
  the one Core-profile message in this whole flow that goes the other way (charger -> CS,
  s4.15), which is why it lives as an `@on` handler inside `MyChargePoint` in `main.py`, not as
  a `send_*` function. No CS-initiated `DataTransfer.req` (s5.5, the reverse direction) was
  built -- nothing in this project needs to send one yet, and speculatively adding it would be
  unused code.
- **A generic per-key config store still needs at least one real branch per `ConfigurationStatus`
  value to be worth anything as a demo.** The simulator's `HeartbeatInterval` (writable ->
  `RebootRequired`) and `NumberOfConnectors` (readonly -> `Rejected` on any write) are
  deliberately chosen as the two simplest distinct examples; a future section adding a real
  config key some flow actually reads (e.g. E's reservation expiry, or F's
  `LocalAuthorizeOffline`) should add it to this same dict rather than inventing a second
  mechanism.

## Done: section E (Reservation)

Fully implemented, tested (`tests/test_reservation.py`, 14 tests), and verified against a live
server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py` pair, including
watching a reservation actually get consumed by a real StartTransaction and a connector move
Reserved -> Preparing -> Charging in one live run. Do not rebuild any of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `send_reserve_now()` / `send_cancel_reservation()` (CS -> charger); `next_reservation_id()` usage; `Reservation.release_for_start()` called from `on_start`; `sweep_expired_reservations_forever()` background task started from `main()` (not `serve()` -- see below); `/admin/reserve-now`, `/admin/cancel-reservation` cases |
| `operate.py` | New `reserve-now <identity> <connector_id> <id_tag> <expiry_date> [--parent-id-tag]` and `cancel-reservation <identity> <reservation_id>` subcommands |
| `models.py` | New `Reservation` document (`reservation_id` unique) with `create()`, `release_for_start()`, `release_by_cancel()`, `sweep_expired()`, `find_active()`; `next_reservation_id()` counter alongside `next_transaction_id()` |
| `simulate_charge_point.py` | `on_reserve_now`/`after_reserve_now` (Available -> Reserved on Accepted; Occupied/Faulted/Unavailable from the connector's actual status; connectorId 0 accepted without moving any connector) and `on_cancel_reservation`/`after_cancel_reservation` (Reserved -> Available) |
| `tests/test_reservation.py` | New. 14 tests |

Full suite: `python -m pytest` -> **78 passed** (64 from sections A+B+C+D + 14 from this), run 5
times in a row with no flakiness.

### Key decision: `StartTransaction`'s reservationId is not the only way a reservation ends

OCPP 1.6 s3.11 states a reservation ends when "the reserved idTag is used on the reserved
connector, or on any connector when connectorId was 0/unspecified" -- not "when
StartTransaction.req's optional reservationId matches". `Reservation.release_for_start()`
therefore releases on **either** signal: the explicit `reservationId` field if the charger
echoed it, or an idTag match against an active reservation on this connector or a connectorId-0
(any-connector) one, whichever a charger actually sends. A charger that never populates
`reservationId` (plenty don't) still correctly releases its reservation.

### Key decision: the expiry sweep only touches this Central System's own bookkeeping

There is no OCPP message for "your reservation expired, please revert" -- a real charger tracks
`expiryDate` on its own clock and reverts its connector's status by itself, with no CS
involvement. `Reservation.sweep_expired()` (and the background task that calls it every 60s from
`main()`) therefore never sends anything to a charger; it only stops this system's own record
from still calling a lapsed reservation active, which is what `StartTransaction`/
`CancelReservation` match against. `test_sweep_expired_reservations` proves this by asserting
the reservation record itself becomes inactive after a sweep, while the (never-notified) test
double's own connector state is deliberately left as `Reserved` -- documenting a real, unfixed
limitation: this simulator does not model a charger's own autonomous expiry-driven
`StatusNotification`. A future section polishing the simulator further should add that if it
starts to matter.

### Why the sweep is started in `main()`, not `serve()`

Unlike everything else added in sections A-D, `sweep_expired_reservations_forever()` is a
real-time background task with no natural end. `serve()` is what the test suite's session-scoped
`server` fixture calls directly (see `tests/conftest.py`); starting a perpetual task there would
leak an uncancelled task across the whole test session for no test's benefit, since every test
calls `Reservation.sweep_expired()` directly instead (immediate, no timing dependency, per
`instructions/05-normal-charge-flow-tests.md`'s "no sleep to synchronise" rule). It is started
only from `main()`, the real entry point `python main.py` runs, which tests never call.

### Other reusable lessons from this pass

- **`I7` (`Faulted` -> `Reserved`) in `connector_state_machine.py` is a fault-*recovery* entry,
  not a "reservation accepted while Faulted" path.** Every `I`-series transition shares the same
  description, "Fault is resolved and status returns to the pre-fault state" -- `I7` only ever
  fires via `recover_from_fault()` for a connector that was already `Reserved` before it
  faulted. A fresh `ReserveNow` while a connector is currently `Faulted` is answered
  `ReservationStatus.faulted` (rejected), never a transition. Reading 06's "only Available ->
  Reserved (A7) and Faulted -> Reserved (I7) are legal" as "both are ways to accept a new
  reservation" would have been wrong.
- **`Reservation.release_for_start`/`release_by_cancel`/`sweep_expired` all write with
  `update_many`/`update_one` directly, following section B/D's atomic-`$set` lesson from the
  start** -- a `StartTransaction` consuming a reservation and an expiry sweep landing on the
  same document are exactly the kind of overlap that already caused a real bug once.

## Done: section F (Offline operation and local authorization)

Fully implemented, tested (`tests/test_local_auth.py`, 13 tests), and verified against a live
server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py` pair. Do not rebuild
any of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `send_get_local_list_version()` / `send_send_local_list()` (CS -> charger); `_authorization_data_for()` builds a `SendLocalList` entry from an `IdTag` document; `/admin/get-local-list-version`, `/admin/send-local-list` cases |
| `operate.py` | New `get-local-list-version` and `send-local-list [--update-type] [--id-tags] [--remove-id-tags]` subcommands |
| `models.py` | New `LocalListState` document (per-charger list version + pushed idTags), written via the same atomic `.set()` upsert pattern as `ConnectorStatus`/`ConfigurationEntry`/`Reservation` |
| `simulate_charge_point.py` | Real `authorization_cache` (dict, refreshed from every `Authorize`/`StartTransaction`/`StopTransaction` conf's `IdTagInfo`, genuinely emptied by `ClearCache` -- previously a no-op); real `local_list`/`local_list_version` state answering `GetLocalListVersion`/`SendLocalList`; `LocalAuthorizeOffline`/`LocalPreAuthorize` added to the section D configuration dict; `LocalPreAuthorize` actually wired into `after_remote_start_transaction` via a new `local_authorize()` helper |
| `tests/test_local_auth.py` | New. 13 tests: SendLocalList/GetLocalListVersion (10), plus this section's explicit "test it here in bulk" instruction for offline-timestamp trust, bulk orphaned stops, and a transaction recorded for a now-refused tag (3) |

Full suite: `python -m pytest` -> **91 passed** (78 from sections A-E + 13 from this), run 5 times
in a row with no flakiness.

### Key decision, and a stated scope limit: no real offline simulation

`LocalAuthorizeOffline` ("authorize locally while offline") and `LocalPreAuthorize` ("start
without waiting for the Central System even when online") are genuine OCPP 1.6 s3.5/s3.6
config keys, reported and changeable through section D's generic configuration store. Only
`LocalPreAuthorize` got real behavioural wiring: `simulate_charge_point.py`'s
`after_remote_start_transaction` checks `local_authorize()` (the Local List, then the
Authorization Cache) FIRST when that key is `"true"`, and only falls back to a live
`Authorize.req` round trip if nothing local matched -- genuinely "start without waiting for the
Central System even when online," reproducible over the always-connected WebSocket this
project's simulator uses.

`LocalAuthorizeOffline` has **no** behavioural wiring, and that is a deliberate, stated limit,
not an oversight: this simulator has no way to actually go offline (there is one live WebSocket
connection, up or down, not a charger that keeps running while unreachable). Faking a "pretend
you can't reach the CS" mode would not exercise anything a live connection can't already cover,
and would add a mechanism nothing else in this project needs. The bulk offline-timestamp tests
this section DOES add exercise the real consequence of offline operation -- queued transactions
landing late with old timestamps -- via the mechanism 04 already built for exactly that, which
needs no pretend-offline mode to test: a client can simply send an old timestamp any time.

### Other reusable lessons from this pass

- **A Local Authorization List entry is built from `IdTag`, never stored twice.** `IdTag` is
  already this project's one source of truth for a driver credential's status (Authorize,
  StartTransaction, `Reservation`'s `id_tag`/`parent_id_tag`, ...); `LocalListState` only tracks
  *which* idTags and *what version* this Central System believes it pushed, exactly the same
  "counter and provenance, not a second copy" shape `ConfigurationEntry` and `Reservation`
  already established. A future section needing to reference a driver credential should look
  for `IdTag` first rather than adding another parallel record of it.
- **The Authorization Cache is genuinely charger-side data with no CS-side counterpart to
  test** -- the CS's only touchpoint with it is `ClearCache` (section D), already covered.
  Do not go looking for a "the CS's view of the cache" concept; there isn't one, by design
  (OCPP 1.6 s3.5.1 -- it is the charger's own stand-alone record).
- **"Test it in bulk" earns its own test, not just a bigger loop inside an existing one.**
  `test_bulk_offline_transactions_trusted_with_old_timestamps` and
  `test_bulk_stop_without_matching_start_are_recorded_as_incomplete` exist specifically because
  06's own text calls out bulk behaviour by name; a single retried assertion inside 05's
  existing `test_offline_timestamps_are_trusted` would not have been the same claim.

## Done: section G (Fault handling)

Fully implemented, tested (`tests/test_fault_history.py`, 8 tests), and verified against a live
server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py` pair. Do not rebuild
any of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `MyChargePoint._record_fault_history()`, called from `on_status`, opens/closes a `FaultEvent` when a connector's status crosses into or out of `Faulted`; `get_fault_history()` (a pure DB read, works with no live connection); `/admin/fault-history` case |
| `operate.py` | New `fault-history <identity> [--connector-id]` subcommand |
| `models.py` | New `FaultEvent` document (`entered_at`/`cleared_at`, connector 0 = whole unit) with `open_new()`, `close_open()`, `history_for()` |
| `simulate_charge_point.py` | New `simulate_fault_and_recover()` / `recover_from_fault()` methods (the latter reads `ConnectorState.pre_fault_status` and calls the state machine's own `recover_from_fault()` rather than guessing a target) and a `--simulate-fault ERROR_CODE` flag for `--idle-seconds` mode |
| `tests/test_fault_history.py` | New. 8 tests |

Full suite: `python -m pytest` -> **99 passed** (91 from sections A-F + 8 from this), run 5 times
in a row with no flakiness.

### Key decision: almost nothing about fault RECOVERY needed building -- it already worked

`connector_state_machine.py`'s I1-I8 transitions and `recover_from_fault()`, and
`ConnectorStatus.pre_fault_status` surviving a restart, were all already built and tested (doc01/
04) before this section started. The entire new CS-side surface is `FaultEvent` plus the four
lines in `on_status` that decide when to open or close one. Resist the urge to add anything
resembling a second state machine for faults -- there is only one, and it already existed.

### Key decision: only a status TRANSITION opens or closes a fault event, not every message

A charger can report `StatusNotification(Faulted, <new error_code>)` more than once while
already Faulted -- real hardware escalating from one problem to a worse one, or just a resend.
`_record_fault_history()` compares the status *before* this message to the status *after*, and
only acts on an actual crossing into or out of `Faulted`; a same-status repeat updates
`ConnectorStatus.error_code` (already handled by `apply()`) but never opens a second
`FaultEvent` or closes one that should stay open. `test_repeated_faulted_report_does_not_open_a_
second_event` proves this specifically, because it is exactly the kind of case naive "insert on
any Faulted message" logic gets wrong.

### Key decision: there is no way for the Central System to cause a fault

Every other section (B-F) added a `send_*` function because OCPP gives the Central System a
message to send. Fault handling has no such message in either direction that starts a fault --
real chargers detect their own. So this section added no admin/operate.py command for
*triggering* a fault, only `fault-history` for *reading* what happened. The simulator's own
`--simulate-fault` flag exists purely as a demo/testing convenience local to
`simulate_charge_point.py`, not something `main.py` or `operate.py` can invoke.

### Other reusable lessons from this pass

- **`ConnectorState.recover_from_fault()` belongs on the CHARGER side of a simulation, not just
  the Central System's.** Before this section, only `main.py`'s `on_status` validated recovery
  transitions; nothing on the simulator side used `recover_from_fault()` to decide what status
  to report when recovering -- it would have been easy for a naive fault-simulation feature to
  just hardcode "recover to Available," which is wrong whenever the fault happened mid-session.
  `simulate_charge_point.py`'s new `recover_from_fault()` method reads `pre_fault_status` from
  its own local `ConnectorState` mirror for exactly this reason.
- **A pure read (`get_fault_history`) needs no `CONNECTED_CHARGE_POINTS` check, no
  `ensure_commissioned` call, and no live connection at all** -- unlike every `send_*` function
  in this project so far. `test_fault_history_is_a_pure_read_that_works_without_a_connection`
  exists specifically to keep that property from regressing if a future edit reflexively adds
  connection-checking boilerplate to it.

## Done: section H (Firmware and diagnostics)

Fully implemented, tested (`tests/test_firmware_and_diagnostics.py`, 14 tests), and verified
against a live server, a live MongoDB, and the real `simulate_charge_point.py`/`operate.py`
pair -- including fetching the hosted firmware file over plain `curl` and watching a real
post-install reboot/reconnect. Do not rebuild any of this; extend it.

| File | What it holds |
|---|---|
| `main.py` | `handle_firmware_request()` (serves `firmware_files/*` over plain HTTP under `/firmware/`, routed from `authorize()` alongside `ADMIN_PATH_PREFIX`); `send_update_firmware()`/`get_firmware_status()`; `send_get_diagnostics()`/`get_diagnostics_status()`; `on_firmware_status`/`on_diagnostics_status` handlers on `MyChargePoint`; `/admin/update-firmware`, `/admin/firmware-status`, `/admin/get-diagnostics`, `/admin/diagnostics-status` |
| `operate.py` | New `update-firmware`, `firmware-status`, `get-diagnostics`, `diagnostics-status` subcommands |
| `models.py` | New `FirmwareUpdate` / `DiagnosticsRequest` documents, each with an append-only `history` list (atomic `$push` + `$set`, matched to the most recently requested one per charger since neither status notification carries a correlation id) |
| `simulate_charge_point.py` | `on_update_firmware`/`after_update_firmware` (simulates Downloading -> Downloaded -> Installing -> Installed, then reboots) and `on_get_diagnostics`/`after_get_diagnostics` (Uploading -> Uploaded); `--fail-firmware-download`, `--decline-diagnostics`, `--fail-diagnostics-upload` flags; `reset_requested` renamed to `reboot_reason` throughout so Reset (C) and firmware installs share one reconnect mechanism in `main()`/`run_connection()` |
| `firmware_files/example-1.0.0.txt` | New. A stand-in firmware image, fetchable at `http://localhost:9000/firmware/example-1.0.0.txt` once `main.py` is running |
| `tests/test_firmware_and_diagnostics.py` | New. 14 tests |

Full suite: `python -m pytest` -> **113 passed** (99 from sections A-G + 14 from this), run 5
times in a row with no flakiness.

### Key decision: "host or presign a URL" means this project hosts it itself, as text

`main.py` serves whatever is in `firmware_files/` directly, off the same port as everything
else, via a new `FIRMWARE_PATH_PREFIX` routed inside `authorize()` next to `ADMIN_PATH_PREFIX`.
Two scope boundaries worth knowing before extending this:
- **Files are served as UTF-8 text, not true binary.** `ServerConnection.respond()` (the
  `websockets` version in use) only accepts a `str` body -- there is no ready-made way to hand
  back arbitrary bytes without constructing a raw `Response` object by hand. A POC's stand-in
  firmware image (a version string / changelog) fits comfortably in that constraint; a section
  that needs to serve a real binary blob will have to build that `Response` manually.
  `firmware_files/example-1.0.0.txt` is exactly this kind of stand-in, not a real firmware image.
- **No signing, no expiry.** "Presign" was the other half of the instruction's phrasing; this
  project does neither -- the URL is just `{FIRMWARE_BASE_URL}/firmware/{filename}`, unguarded,
  same trust model as the admin API's shared-secret-over-plain-HTTP approach elsewhere in this
  project. A production deployment would need real signing/expiry and TLS.

### Key decision: GetDiagnostics' upload destination is a real string with no real receiver

`diagnostics_upload_base_url()` builds a location the charger is told to upload to, and that
location is recorded for audit purposes -- but nothing actually listens there. The `websockets`
version in use only parses HTTP/1.1 GET on this project's one listening socket (established
back in section A's `handle_admin_request`), so there was never a way to receive an upload on
this same port even if this section tried. `simulate_charge_point.py`'s own "upload" is
correspondingly a `DiagnosticsStatusNotification` progress sequence with no bytes ever moving --
consistent with how this project already never actually meters real electricity or presents a
real RFID card. A real deployment would point this at FTP or a presigned object-store URL and
have something on the other end.

### Key decision: firmware and diagnostics progress match "most recently requested", not by id

Neither `FirmwareStatusNotification` nor `DiagnosticsStatusNotification` carries a correlation
id back to the request that triggered it (OCPP 1.6 doesn't give them one) -- so
`FirmwareUpdate.record_status()`/`DiagnosticsRequest.record_status()` use
`find_one_and_update(..., sort=[("requested_at", -1)])` to always land on the newest request for
that charger. `test_firmware_status_matches_most_recently_requested_update` proves the earlier
of two overlapping requests is left alone rather than being (incorrectly) updated too.

### Other reusable lessons from this pass

- **A field with no protocol-level accept/reject (`UpdateFirmware.conf`, `GetDiagnostics.conf`)
  still needs a "did anything actually happen" signal for persistence to make sense.**
  `UpdateFirmware.conf` has no status at all -- a record is created unconditionally.
  `GetDiagnostics.conf`'s only signal is whether `file_name` came back non-null; that, not a
  separate accept/reject enum, is what gates creating a `DiagnosticsRequest`.
- **`reset_requested` becoming `reboot_reason` is the generalization section C's own progress
  notes anticipated** ("a future section needing the simulator to reconnect... should reuse this
  loop rather than adding a second one") -- section H is that future section. Any later work
  that also reboots the simulated charger (there isn't one currently) should extend
  `reboot_reason`, not add a third mechanism.

## Done: section J (Remote trigger)

Implemented and tested (`tests/test_trigger_message.py`, 12 tests), and checked live with
`main.py` + `simulate_charge_point.py --idle-seconds` + `operate.py`. Extend, don't rebuild.

| File | What it holds |
|---|---|
| `main.py` | `send_trigger_message(identity, requested_message, connector_id=None)`; `/admin/trigger-message` |
| `operate.py` | `trigger-message <identity> <message> [--connector-id N]` |
| `simulate_charge_point.py` | `on_trigger_message`/`after_trigger_message` (answers, *then* sends the message once the conf is on the wire); `--reject-trigger-message`; tracks last firmware/diagnostics status (Idle before any) and per-connector meter readings so a trigger has something true to re-report |
| `tests/test_trigger_message.py` | New. 12 tests |

Decisions worth knowing:
- **Allowed while `Pending`** -- `send_trigger_message` does not call `ensure_commissioned`, like
  configuration reads/writes. s4.2 bars only RemoteStart/RemoteStop while Pending, and this is
  the sanctioned way to get a message out of such a charger. Tested.
- `connector_id` is refused (`ValueError`, HTTP 400) for anything but `MeterValues` /
  `StatusNotification`; an unknown message name is also a 400. Rejected / NotImplemented are the
  charger's own answers and are returned as-is.
- Nothing new was needed on the receiving side: triggered messages arrive through the existing
  `on_status` / `on_meter_values` / `on_boot` handlers. A repeated same-status
  StatusNotification is a no-op in the state machine, so a status refresh never causes a
  transition.
- The simulator treats its `--connector-id` (default 1) as a connector it owns even before it has
  reported on it, so a trigger to an idle charger is not wrongly Rejected.
- A triggered `MeterValues` reports the simulator's real last reading (`meter_readings`, kept up
  to date by the scripted scenario and by remote start). The first version reported 0 Wh
  regardless, because those updates were lost in an edit; fixed alongside section I.

## Done: section I (Smart charging)

Implemented and tested, and checked live: `main.py` + the real `simulate_charge_point.py` +
`operate.py` (remote start carrying a profile, a 0 W profile suspending the session and lifting
by itself six seconds later with nothing sent, composite schedules, the refusals). With MongoDB
and Redis up, `python -m pytest` -> **286 passed**, run three times in a row. Extend, don't
rebuild.

| File | What it holds |
|---|---|
| `charging_profiles.py` | New, flat like `connector_state_machine.py`. `parse_profile()` (pydantic; accepts camelCase as printed in the spec or snake_case as the ocpp library delivers it; one-line-per-problem `ValueError`s), `purpose_connector_problem()`, `installation_problem()` (what a *charger* must refuse), and `ChargingProfileSet` -- install/replace/clear/drop-TxProfile plus `composite()`, `limit_at()`, `next_change_after()`. The Central System uses only the validation half. |
| `models.py` | New `InstalledChargingProfile` document (`charging_profiles`): `install()` (s3.13.2 replacement), `clear()`, `drop_transaction_profiles()`, `for_charge_point()`. Two unique indexes: per charger by profile id, and per charger by connector + purpose + stack level |
| `main.py` | `send_set_charging_profile()`, `send_clear_charging_profile()`, `send_get_composite_schedule()`, `get_installed_charging_profiles()`; `send_remote_start_transaction()` gained an optional `charging_profile`; `on_stop` drops the ended transaction's TxProfile records; `/admin/set-charging-profile`, `/admin/clear-charging-profile`, `/admin/get-composite-schedule`, `/admin/charging-profiles`, and an optional `profile` on `/admin/remote-start`; `admin_json_response` now turns `Decimal` into a float |
| `operate.py` | `set-charging-profile`, `clear-charging-profile`, `get-composite-schedule`, `charging-profiles`, and `--limit/--profile-file/--profile-json` on `remote-start`. A whole profile is JSON (a file or a string); the common case is `--limit 16 --unit A`, which builds a one-period Relative profile |
| `simulate_charge_point.py` | Real Smart Charging: the four `s3.13.5` configuration keys, `on_set_charging_profile` (Rejected for anything `installation_problem` finds), `on_clear_charging_profile` (Unknown when nothing matched), `on_get_composite_schedule` (Rejected for an unknown connector), `apply_charging_limits()`/`refresh_charging_limits()` and a watcher task that re-applies at the next schedule boundary; `--reject-charging-profile`, `--no-smart-charging` |
| `tests/test_charging_profiles.py` | New. 69 pure tests, expected values worked out by hand |
| `tests/test_smart_charging.py` | New. 50 tests against `SmartChargingChargePoint`, a spec-following double |

### Decisions worth knowing

- **The Central System never computes a composite schedule.** It asks the charger
  (`GetCompositeSchedule`): only the charger knows its local limits, and s5.7 calls the answer
  "only indicative for that point in time". `ChargingProfileSet.composite()` exists for the
  simulator and the test doubles, and is in its own module because the simulator cannot be
  imported by tests.
- **Records are written only on `Accepted`** (like `Reservation`), and a clear is applied to the
  records whatever the answer: `Accepted` removed them on the charger, and `Unknown` means the
  charger never held them, so a matching record is stale. Criteria on a clear are read *together*
  (ANDed), and a clear with none removes everything -- `operate.py` demands `--all` for that.
- **A TxProfile is refused before sending unless a transaction is open on the connector**, and
  the Central System fills in `transactionId` itself (s5.16: it SHALL include it), refusing one
  that names another transaction. `ChargePointMaxProfile` off connector 0 and `TxProfile` on
  connector 0 are refused before sending too (s3.13.1). All of these are `ValueError` -> HTTP 400.
- **A profile sent with `RemoteStartTransaction` has no transaction yet** (s5.16.2: purpose
  TxProfile, no `transactionId`), so it is remembered in memory, keyed by charger, connector
  and idTag, and recorded when the matching `StartTransaction` arrives. Entries lapse after five
  minutes so a start that never happens cannot attach to a later one. Lost on a restart of the
  Central System, which only costs the bookkeeping for a session already mid-start.
- **`TxDefaultProfile` on connector N beats the one on connector 0 for N**, whatever their stack
  levels (s3.13.1: it is "replaced only for that specific connector"); a `TxProfile` beats both
  while it says anything. Uniqueness of (stack level, purpose) is therefore per connector, not
  per charger.
- **Composite maths conventions**: watts and amps convert at 230 V, three phases unless
  `numberPhases` says otherwise (s7.12); a limit with nothing installed is the charger's
  hardware maximum (32 A x 3 phases in the simulator); connector 0 is the sum of its connectors'
  limits held down by the `ChargePointMaxProfile`; a Relative profile counts from the start of
  the transaction, from the moment of planning if there is none, and from installation for a
  `ChargePointMaxProfile`; a `Weekly` recurrence is seven days from `startSchedule` (the spec's
  wording), not Monday morning as the ocpp library's enum docstring says.
- **The simulator only reacts to a limit of zero.** That is the state change the spec makes
  visible (Charging -> SuspendedEVSE, C5, and back, E3); other limits change how much power
  flows, which it does not model, so it does not split a `ChargePointMaxProfile` between
  connectors either. It does not persist profiles across a simulated reboot -- the same scope
  boundary as its authorization cache and local list.
- **The ocpp library parses every JSON number it receives as a `Decimal`.** Fine for pydantic
  and for `==` against floats, but `json.dumps` chokes on it; `admin_json_response` converts.
- **Not sent automatically**: the spec lets a Central System push a profile at the start of
  every transaction (s5.16.1). Nothing here does that on its own; a `TxDefaultProfile` on the
  charger is the way to get a policy applied to every session.
