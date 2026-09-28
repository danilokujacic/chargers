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

## Not done: sections B through J

None of the following exist yet. Tackle them in this order (06's own "roughly by usefulness"
ordering); each is independently scoped in `06-remaining-flows.md` under its own heading.

| # | Section | One-line reminder of the trap |
|---|---|---|
| B | Availability management | A transaction in progress → `Scheduled`, not `Accepted`; must actually defer, not just report. Persists across reboot. |
| C | Reset and unlock | Either reset type re-runs the **whole commissioning flow** from `BootNotification` (s4.2.1) -- reuse `commissioning.py`, don't assume the charger stays "booted". |
| D | Configuration | Generalise the `ChangeConfiguration` handling `03` built only for `AuthorizationKey`. Needs a per-charger config store. |
| E | Reservation | New `Reservation` document + expiry sweep. State machine only allows `Available`/`Faulted` → `Reserved`; `StartTransaction`'s optional `reservationId` must release it. |
| F | Offline / local auth | Mostly already covered by `04`'s offline-timestamp handling; this section is about `SendLocalList`/`GetLocalListVersion` and the Authorization Cache concept specifically. |
| G | Fault handling | `connector_state_machine.recover_from_fault()` already exists and is tested (doc01); this section is about the fault *history* document and wiring real `ChargePointErrorCode` values through `on_status` (which currently just persists error_code without a history). |
| H | Firmware / diagnostics | Needs the CS to host or presign a URL for `UpdateFirmware` -- decide how before starting. |
| I | Smart charging | Largest optional profile; explicitly "leave it last" per 06 itself. |
| J | Remote trigger | Small, but the *only* sanctioned way to make a `Pending` charger send anything (s4.2) -- worth doing before I/H if you want to unblock testing those from a Pending state. |

## What "done" looks like for each of B–J

Same bar section A was held to, per `06-remaining-flows.md`'s own acceptance criteria: the
messages exist, every status change goes through `connector_state_machine`, the spec-mandated
statuses (`Scheduled`, `Occupied`, `NotSupported`, …) are actually returned when required, state
persists across a Central System restart, tests exist following `05`'s rules, and
`python -m pytest` stays green with the normal charge flow (`04`) still passing unchanged.

Write the completion brief specified in `README.md#report-when-you-finish` for whichever
section(s) you complete -- the same "what/use case/why/verification" shape as every other task
in this folder, not a shape specific to this progress file.
