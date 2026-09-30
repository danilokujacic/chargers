# System overview: the OCPP Central System

This document explains the entire system that exists in this repository today, for someone who
knows Python and MongoDB but nothing about EV charging or OCPP. It is a reference, not a task
list — for what is still unbuilt, see `instructions/06a-remaining-flows-progress.md`.

Read it top to bottom once, then use the table of contents to jump back to any part later.

## Table of contents

1. [What this project is](#1-what-this-project-is)
2. [Vocabulary you need before anything else makes sense](#2-vocabulary-you-need-before-anything-else-makes-sense)
3. [The shape of the system](#3-the-shape-of-the-system)
4. [How a charger's life actually goes, end to end](#4-how-a-chargers-life-actually-goes-end-to-end)
5. [`main.py` — the Central System](#5-mainpy--the-central-system)
6. [`connector_state_machine.py` — the rulebook for connector status](#6-connector_state_machinepy--the-rulebook-for-connector-status)
7. [`commissioning.py` — onboarding a new charger](#7-commissioningpy--onboarding-a-new-charger)
8. [`models.py` — everything stored in MongoDB](#8-modelspy--everything-stored-in-mongodb)
9. [`simulate_charge_point.py` — a fake charger for testing](#9-simulate_charge_pointpy--a-fake-charger-for-testing)
10. [`operate.py` — the operator's remote control](#10-operatepy--the-operators-remote-control)
11. [`seed.py` and `register_charge_point.py` — provisioning](#11-seedpy-and-register_charge_pointpy--provisioning)
12. [`ocpp_client_auth.py`](#12-ocpp_client_authpy)
13. [The test suite](#13-the-test-suite)
14. [Running the whole thing yourself, step by step](#14-running-the-whole-thing-yourself-step-by-step)
15. [What each of the ten OCPP "flows" actually does](#15-what-each-of-the-ten-ocpp-flows-actually-does)
16. [Design decisions and conventions worth knowing](#16-design-decisions-and-conventions-worth-knowing)
17. [Glossary](#17-glossary)

---

## 1. What this project is

This is a **Central System (CS)** for **OCPP 1.6** — the industry-standard protocol that EV
charge points (charging stations) use to talk to the backend that manages them. If you have ever
used a public EV charger, there was a Central System like this one behind it: it knows which
chargers exist, decides whether a driver's RFID card is allowed to charge, records every
charging session for billing, and lets an operator remotely start/stop sessions, take a charger
out of service, push firmware, and so on.

Concretely, this project is:

- A WebSocket server (`main.py`) that charge points connect to and speak OCPP over.
- A MongoDB database (via `models.py`) that is the durable source of truth for every charger,
  driver credential, transaction, reservation, fault, firmware update, etc.
- A handful of CLI tools (`operate.py`, `seed.py`, `register_charge_point.py`) an operator runs
  from a terminal to provision chargers and control them.
- A charge point **simulator** (`simulate_charge_point.py`) that pretends to be a real physical
  charger, so the whole system can be exercised and demoed without owning actual hardware.
- A pytest suite (`tests/`) that proves all of the above actually works, against a real MongoDB
  and real WebSocket connections — not mocks.

*(Update: the public map platform described below now exists — the API, the PlugShare import,
the website and the demo fleet. It is explained in [`PLATFORM_GUIDE.md`](PLATFORM_GUIDE.md); this
document still covers the Central System itself.)*

When this overview was written there was no user-facing website or map yet. Everything was a
backend protocol implementation plus command-line tools. (Four separate guides describe how to build a public map
on top of this: `instructions/07-public-map-platform.md` covers the data model and a brief
overview; `instructions/08-public-api-service.md` is the self-contained spec for the new,
isolated FastAPI service that sits in the middle; `instructions/09-plugshare-import.md` is the
concrete migration that populates it with ~134 real charging locations from a PlugShare export of
Montenegro; `instructions/10-frontend-nextjs.md` is the self-contained spec for the Next.js +
Tailwind + MapTiler frontend itself.)

## 2. Vocabulary you need before anything else makes sense

- **Charge Point (CP)**: the physical charging unit. In code, its unique name is called its
  **identity** (e.g. `"CP001"`). One charge point can have several **connectors**.
- **Connector**: one physical socket/cable on a charge point. Connector numbers start at 1.
  **Connector 0** is special — it does not mean a socket at all, it means "the charge point as a
  whole" (its main controller). Connector 0 can only ever be `Available`, `Unavailable`, or
  `Faulted` — never `Charging`, because the main controller doesn't charge anything itself.
- **Central System (CS)**: the backend (this project). Chargers connect *to* it, not the other
  way around — the WebSocket connection is always opened by the charger.
- **OCPP**: Open Charge Point Protocol, version 1.6 here. It defines the exact JSON messages a
  Central System and a Charge Point send each other, and what each field means. There are two
  parts: **OCPP 1.6** itself (the messages and their meaning) and **OCPP-J 1.6** (how those
  messages are transported — JSON-over-WebSocket, with a specific framing and the authentication
  rule described below).
- **idTag**: a driver's credential — physically, usually the UID printed on an RFID card. This
  is completely different from a charge point's own identity/key (see next point). An idTag
  authorizes a *person* to draw energy; it has nothing to do with authenticating a *charger*'s
  connection.
- **Authorization key**: a 20-byte secret every charge point must have installed on it. It is
  the *charger's* password for connecting to the Central System (HTTP Basic auth, username =
  identity, password = this key). Never confuse this with an idTag.
- **Transaction**: one charging session — from `StartTransaction` to `StopTransaction`. Not the
  same as a "connector being plugged in": a session can begin with just a cable inserted
  (`Preparing` status) before any transaction exists.
- **CALL / CALLRESULT / CALLERROR**: OCPP-J's three message shapes. A `.req` message is a CALL;
  a successful reply is a CALLRESULT (in this codebase, an object like
  `call_result.BootNotification(...)`); a failed reply is a CALLERROR. Every request/response
  pair in this codebase follows the naming convention `Foo.req` → `Foo.conf` (the spec's own
  term for "the CALLRESULT that answers a Foo request").
- **Central-System-initiated vs charger-initiated messages**: some OCPP messages are always sent
  by the charger (`BootNotification`, `StatusNotification`, `StartTransaction`, ...) and some are
  always sent by the Central System to command the charger (`RemoteStartTransaction`, `Reset`,
  `ChangeAvailability`, ...). Both directions travel over the *same* open WebSocket connection.

## 3. The shape of the system

```
                                   ┌────────────────────────┐
   Physical charger  ── OCPP-J ──▶ │                        │
   (or simulate_charge_point.py)   │        main.py         │◀──── operate.py (CLI)
                                   │   (the Central System)  │       (operator commands,
                                   │                        │        plain HTTP)
                                   └───────────┬────────────┘
                                               │ reads/writes
                                               ▼
                                        ┌───────────────┐
                                        │    MongoDB    │
                                        │ (via models.py)│
                                        └───────────────┘
                                               ▲
                                               │ reads/writes
                                   ┌───────────┴────────────┐
                                   │  seed.py /             │
                                   │  register_charge_point.py │
                                   │  (one-off provisioning)│
                                   └────────────────────────┘
```

`main.py` is the only process that speaks OCPP. Everything else either talks to `main.py` over
plain HTTP (`operate.py`) or talks to MongoDB directly (`seed.py`,
`register_charge_point.py`, and `main.py` itself). `simulate_charge_point.py` is not part of the
Central System at all — it plays the *other* role in the conversation, standing in for a real
charger, and connects to `main.py` exactly the way a real charger would.

**One WebSocket connection carries both directions of traffic.** There is no separate channel
for "charger tells CS things" versus "CS commands charger" — `RemoteStartTransaction`,
`StatusNotification`, everything, goes over the one connection that charger opened when it
booted. This is why `main.py` keeps a registry, `CONNECTED_CHARGE_POINTS` (a plain Python dict,
identity → live connection object), of every currently-open connection: it's the only way for an
operator command to find the right socket to write to.

**The admin HTTP API is multiplexed onto the same port as the OCPP WebSocket endpoint.** This
was a specific, deliberate choice (see §16) rather than running a second server: `main.py` binds
one port (9000 by default), and a single `process_request` hook inspects the incoming HTTP
request's path before any WebSocket upgrade happens. A path starting with `/admin/` or
`/firmware/` is answered as plain HTTP; anything else is treated as a charger's OCPP-J connection
attempt.

## 4. How a charger's life actually goes, end to end

This is the story `main.py` and `commissioning.py` implement, told in order:

1. **Provisioning.** Before a charger can ever connect, someone must register it in the
   database with `seed.py` or `register_charge_point.py`. This creates a `ChargePoint` document
   and either installs a key directly (**Route A**, via `seed.py` — the charger is
   `Accepted` immediately) or leaves it `Pending` until the charger proves itself over OCPP
   (**Route B**, via `register_charge_point.py register`).
2. **Connecting.** The charger opens a WebSocket connection to
   `ws://<host>:9000/<identity>`, with an HTTP Basic `Authorization` header carrying its identity
   and key. `main.py`'s `authorize()` function checks this *before* the WebSocket handshake even
   completes. Wrong key, wrong identity, or missing header → the connection is refused with an
   HTTP error and no OCPP message is ever exchanged.
3. **Booting.** Once connected, the very first message a charger sends is `BootNotification`
   (make/model, serial numbers, firmware version, etc.). `main.py` answers with `Accepted`
   (if the charger is already trusted), `Pending` (still onboarding), or `Rejected`. A
   `Pending` charger, right after this, gets **Route B onboarding** pushed to it automatically —
   see §7.
4. **Heartbeats.** The charger periodically sends `Heartbeat` just to prove it's alive; the CS
   replies with the current time. Nothing else happens here.
5. **Reporting connector status.** Whenever a connector's state changes (a cable inserted, a fault
   detected, charging started), the charger sends `StatusNotification`. This is the single most
   important recurring message in the whole system — see §6 for the rules governing what statuses
   are even legal to report.
6. **A driver arrives.** They present their card; the charger sends `Authorize` with the idTag.
   The CS looks the tag up (`IdTag` document) and says `Accepted`, `Blocked`, `Expired`,
   `Invalid`, or `ConcurrentTx` (already charging elsewhere).
7. **Charging starts.** The charger sends `StartTransaction` (connector, idTag, starting meter
   reading, timestamp). The CS allocates a `transaction_id` and creates a `Transaction`
   document — this is what actually exists as a billable session, not the earlier `Authorize`.
8. **Charging happens.** The charger periodically sends `MeterValues` (energy delivered so far).
   These get appended to the open `Transaction`.
9. **Charging stops.** The charger sends `StopTransaction` (final meter reading, reason, and
   optionally which idTag stopped it). The CS closes the `Transaction`.
10. **Anything else, any time.** In parallel with all of the above, an operator can push
    commands at a connected charger through `operate.py` — remote start/stop, take a connector
    out of service, reboot it, read/change its configuration, reserve a connector, push firmware,
    pull diagnostics — and the charger can report a fault at any moment via the same
    `StatusNotification` mechanism as step 5.

Every one of steps 6–10 is implemented and tested; the sections below explain how.

## 5. `main.py` — the Central System

This is the one process that speaks OCPP. Everything in it falls into one of five buckets.

### 5.1 Connection setup

- `identity_from_path(path)` — pulls `"CP001"` out of the connection URL `/CP001`.
- `parse_basic_credentials(header)` / `authorization_key_from_basic_password(password)` (the
  latter lives in `models.py`) — decode the HTTP Basic header into (identity, key), handling
  the fact that different real chargers send the key either as 40 hex characters or as the raw
  20 bytes (the OCPP-J spec's own example uses raw bytes, which aren't valid UTF-8 — this
  tripped up an earlier, naive implementation, which is why it's called out explicitly in the
  code comments).
- `authorize(connection, request)` — the actual gatekeeper, wired in as `websockets`'
  `process_request` hook. Returns `None` to accept the connection, or an HTTP response object to
  reject it. This function is also where `/admin/*` and `/firmware/*` requests get diverted
  before any charger-auth logic runs at all (see §5.4 and §5.5).
- `on_connect(websocket)` — runs once per accepted connection. Looks up the charger's
  `ChargePoint` document, preloads its connectors' last-known statuses from MongoDB (so state
  survives a restart), registers it in `CONNECTED_CHARGE_POINTS`, and then just runs the
  connection's message loop (`charge_point.start()`) until it disconnects.
- `serve(host, port)` / `main()` — `serve()` does the actual `websockets.serve(...)` call and
  returns the server object; `main()` is the real `python main.py` entry point, which connects
  to MongoDB first and then calls `serve()`. They're split apart specifically so the test suite
  can call `serve()` directly on an OS-assigned free port, without ever needing port 9000.

### 5.2 `MyChargePoint` — handling messages *from* a charger

This class subclasses the `ocpp` library's `ChargePoint`, and one instance exists per open
connection. Handlers are registered with the library's `@on(Action.xxx)` decorator; the library
looks at the `action` field of an incoming CALL and dispatches to the matching method
automatically. A second decorator, `@after(Action.xxx)`, registers a function that runs
*after* the CALLRESULT for that action has actually been sent back over the wire — used whenever
a handler needs to do more work following its own reply (e.g. pushing a configuration change),
because doing that work directly inside the `@on` handler could race ahead of the reply itself.

Every handler, in the order a session would naturally hit them:

| Handler | Message | What it does |
|---|---|---|
| `on_boot` | `BootNotification` | Records vendor/model/serials on the `ChargePoint` document; decides Accepted/Pending/Rejected via `commissioning.boot_status()`. |
| `after_boot` | (after the above) | If still Pending, kicks off Route B onboarding (`commissioning.onboard`). If already accepted, re-pushes any `Inoperative` availability this charger is supposed to have (see the Availability row of §15) — this runs on *every* boot, which is what makes it survive both a Central System restart and the charger itself losing its own memory. |
| `on_heartbeat` | `Heartbeat` | Just returns the current time. |
| `on_status` | `StatusNotification` | Validates the reported status change against `connector_state_machine`, persists it to `ConnectorStatus`, and opens/closes a `FaultEvent` if this transition crosses into or out of `Faulted`. This is the busiest handler in the file. |
| `on_authorize` | `Authorize` | Looks up the idTag (`IdTag.authorize`) and returns its status. |
| `on_start` | `StartTransaction` | Creates the `Transaction`, releases any `Reservation` this session consumes, and logs a warning (without refusing anything) if the idTag wasn't actually `Accepted`. |
| `on_meter_values` | `MeterValues` | Appends to the open `Transaction` if `transaction_id` is given; otherwise stores it as a standalone reading on the connector. |
| `on_stop` | `StopTransaction` | Closes the `Transaction`. Handles two edge cases explicitly: a `StopTransaction` for a transaction the CS never saw start (records it as `incomplete`), and a duplicate delivery of a stop it already processed (no-ops). |
| `on_data_transfer` | `DataTransfer` | The vendor-specific escape hatch. This project registers no vendors, so it always answers `UnknownVendorId`, but logs what was attempted. |
| `on_firmware_status` | `FirmwareStatusNotification` | Records progress on the charger's most recently requested firmware update. |
| `on_diagnostics_status` | `DiagnosticsStatusNotification` | Records progress on the charger's most recently requested diagnostics upload. |

### 5.3 The `send_*` functions — commanding a charger

These are ordinary async functions (not methods, not `@on` handlers) that an operator triggers
via the admin API. Every one of them follows the same shape:

1. Look the identity up in `CONNECTED_CHARGE_POINTS`; if it's not there, raise `RuntimeError`
   ("not currently connected") — there is no way to command a charger that isn't on the line.
2. Call `ensure_commissioned(record, "ActionName")` (from `commissioning.py`), which raises
   `PendingChargerError` if the charger is still `Pending`. **Three exceptions**: `GetConfiguration`
   and `ChangeConfiguration` are allowed even while Pending, because OCPP 1.6 s4.2 explicitly
   carves configuration reads/writes out of the "nothing while Pending" rule, and
   `TriggerMessage` is allowed because it is the one sanctioned way to make a Pending charger
   send anything; everything else (remote start/stop, availability, reset, unlock, clear cache,
   reservations, local list, firmware, diagnostics, charging profiles) is blocked.
3. Send the actual OCPP CALL and await its CALLRESULT.
4. Update MongoDB to reflect what just happened — but **only when the charger's answer actually
   means something changed**. A `Rejected` `ChangeAvailability` must not be recorded as if the
   availability changed; a firmware update always gets recorded because `UpdateFirmware.conf`
   has no accept/reject concept at all.

The full list: `send_remote_start_transaction`, `send_remote_stop_transaction`,
`send_change_availability`, `send_reset`, `send_unlock_connector`, `send_get_configuration`,
`send_change_configuration`, `send_clear_cache`, `send_reserve_now`,
`send_cancel_reservation`, `send_get_local_list_version`, `send_send_local_list`,
`send_update_firmware`, `send_get_diagnostics`, `send_trigger_message`,
`send_set_charging_profile`, `send_clear_charging_profile`, `send_get_composite_schedule`. There
are also pure-read functions with the same naming pattern but no charger interaction at all —
`get_fault_history`, `get_firmware_status`, `get_diagnostics_status`,
`get_installed_charging_profiles` — which just query MongoDB and work even if the charger is
offline, since they're reporting *history* or *records*, not commanding anything live.

### 5.4 The admin HTTP API

`handle_admin_request(connection, request)` is the single dispatcher behind every `/admin/*`
path. Because the `websockets` library version this project uses only ever parses HTTP/1.1 `GET`
on its listening socket (any other verb is rejected before this code even runs), **every admin
call is a GET with parameters in the query string** — there is no request body support, ever.
Every path requires `?token=<value>` matching the `ADMIN_TOKEN` environment variable; if that
variable isn't set at all, every `/admin/*` path answers 404, as if the whole API didn't exist
(rather than "200 OK, no auth required").

The full list of admin paths, one per operator action: `/admin/remote-start`,
`/admin/remote-stop`, `/admin/change-availability`, `/admin/reset`, `/admin/unlock-connector`,
`/admin/get-configuration`, `/admin/change-configuration`, `/admin/clear-cache`,
`/admin/fault-history`, `/admin/update-firmware`, `/admin/firmware-status`,
`/admin/get-diagnostics`, `/admin/diagnostics-status`, `/admin/get-local-list-version`,
`/admin/send-local-list`, `/admin/reserve-now`, `/admin/cancel-reservation`,
`/admin/trigger-message`, `/admin/set-charging-profile`, `/admin/clear-charging-profile`,
`/admin/get-composite-schedule`, `/admin/charging-profiles` (plus the site-location ones, see the
public-map instructions). A whole charging profile travels as one JSON string in a single query
parameter, since there is no request body. Each one calls the
matching `send_*`/`get_*` function above and serializes the result to JSON via
`admin_json_response`.

### 5.5 Hosting firmware images

`handle_firmware_request(connection, request)` answers `GET /firmware/<filename>` by reading a
file out of the local `firmware_files/` directory and returning it. This exists because OCPP's
`UpdateFirmware` message tells the charger a URL to fetch its new firmware image *from* — the
Central System has to actually host that URL somewhere, and this project hosts it itself, on the
same port, rather than depending on any external file storage. See §16 for the scope limits of
this (text files, not real binary; no signing or expiry).

## 6. `connector_state_machine.py` — the rulebook for connector status

This module is deliberately **pure**: no MongoDB, no `async`, no I/O of any kind, nothing beyond
the `ocpp` library's own enum definitions. That's on purpose — it encodes OCPP 1.6 §4.9's status
transition table (which statuses a connector may legally move between, and why) as plain data,
so it can be unit-tested with zero infrastructure and so every other part of the system (the
Central System *and* the simulator, on the charger side) can share one source of truth instead of
each re-deriving the rules.

- The nine possible statuses: `Available`, `Preparing`, `Charging`, `SuspendedEV`,
  `SuspendedEVSE`, `Finishing`, `Reserved`, `Unavailable`, `Faulted`.
- `_TRANSITION_LIST` holds all 53 transitions the spec permits, each labelled with the spec's
  own code (e.g. `"A9"` = Available → Faulted) and the spec's own description of what causes it —
  transcribed verbatim, not paraphrased, so it can be traced back to the document.
- **Connector 0 is special**: `MAIN_CONTROLLER_STATUSES` restricts it to
  `Available`/`Unavailable`/`Faulted` only, since it represents the whole charge point, not a
  socket, and has no `Charging` state of its own.
- `ConnectorState` is the actual object every connector gets, live in memory (mirrored to
  MongoDB via `ConnectorStatus` — see §8): `.status`, `.pre_fault_status`, `.change_to(target)`
  (validates and applies, or raises `IllegalTransition`), `.can_change_to(target)`,
  `.force_status(target)` (bypasses validation — used when a charger reports something the table
  doesn't allow, since the charger is the authority on its own hardware and the CS's job is to
  record reality, not silently reject it), and `.recover_from_fault()` (returns to whatever
  status was recorded right before the connector faulted — never a guessed target).

## 7. `commissioning.py` — onboarding a new charger

This is entirely about the question "does the Central System trust this charger yet?", answered
by the `registration_status` field on its `ChargePoint` document: `Pending`, `Accepted`, or
`Rejected`.

- **Route A** (via `seed.py`): the authorization key is installed on the charger before it ever
  connects (e.g. programmed at the factory). It boots straight to `Accepted`.
- **Route B** (via `register_charge_point.py register`): the charger connects with *some*
  starting key, boots as `Pending`, and `onboard()` immediately pushes it a brand-new key over
  `ChangeConfiguration(key="AuthorizationKey", value=...)`. Only once the charger answers that
  `Accepted` does `confirm_key_rotation()` promote it to `Accepted` — and the charger's *old*
  key keeps working the entire time this is in flight, so a lost message, a rejected change, or
  a dropped connection can never lock a charger out of talking to its own Central System (this
  is a hard requirement straight from OCPP-J 1.6 §6.2.2, not a design preference).
- `ensure_commissioned(record, action_name)` — the single guard every Central-System-initiated
  `send_*` function calls first (see §5.3); raises `PendingChargerError` for anything not
  explicitly exempted.

## 8. `models.py` — everything stored in MongoDB

Every document below is a Beanie `Document` subclass — Beanie is a thin async wrapper that lets
MongoDB documents be defined and queried as ordinary Pydantic models. `init_db()` connects to
MongoDB and registers all of them with `init_beanie(...)`, which is also what actually creates
every declared index on startup (there is no separate "run migrations" step in this project —
see the frontend guide, §B, for why that's fine for the additive schema changes it proposes).

| Document | Collection | What it represents |
|---|---|---|
| `ChargePoint` | `charge_points` | One registered charger: identity, hashed authorization key, `registration_status`, everything `BootNotification` reported, key-rotation bookkeeping for Route B. |
| `IdTag` | `id_tags` | One driver credential: its status (`Accepted`/`Blocked`/`Expired`/...), optional `parent_id_tag` (a group of cards that may stop each other's sessions — see §3.10), optional expiry. |
| `Transaction` | `transactions` | One charging session: connector, idTag, meter start/stop, timestamps *as the charger reported them* (never server time — a charger that was offline can deliver these hours late), the accumulated `MeterValues`, `is_open`, and an `incomplete` flag for orphaned stops. |
| `ConnectorStatus` | `connector_statuses` | The last known status of one connector, persisted so it survives a restart: `status`, `error_code`, `pre_fault_status`, and (since section B) `desired_availability`/`availability_change_scheduled` — the *operator's intent*, tracked separately from the connector's *actual* live status because the two can disagree for a while (a `ChangeAvailability` mid-transaction answers `Scheduled` and only really takes effect once that session ends). |
| `ConfigurationEntry` | `configuration_entries` | One configuration key/value this Central System knows a charger has (from `GetConfiguration`) or has set (`ChangeConfiguration`). Never stores a value for `"AuthorizationKey"`, by design — see §16. |
| `Reservation` | `reservations` | One connector (or, with `connector_id=0`, "any connector") held for one idTag until it's used, cancelled, or its `expiry_date` passes. `is_active`/`released_reason` (`"consumed"`/`"cancelled"`/`"expired"`) is this project's usual open/closed-with-a-reason shape. |
| `LocalListState` | `local_list_state` | Per-charger bookkeeping for the Local Authorization List a Central System can push: the version number and which idTags were pushed. The idTags' own data still lives only in `IdTag` — this never duplicates it. |
| `FaultEvent` | `fault_events` | One fault episode on a connector: `error_code`, `info`, `vendor_error_code`, `entered_at`, `cleared_at` (null while still faulted). Exists because `StatusNotification`'s `error_code` field only ever describes *right now* — without this, a fault that already cleared would be invisible history. |
| `FirmwareUpdate` | `firmware_updates` | One `UpdateFirmware` request and its progress: `location`, `retrieve_date`, `status`, and an append-only `history` list of every status it's passed through. |
| `DiagnosticsRequest` | `diagnostics_requests` | The same shape as `FirmwareUpdate`, for one `GetDiagnostics` request. |
| `InstalledChargingProfile` | `charging_profiles` | A charging profile a charger *accepted* (the whole profile as sent, plus connector, purpose, stack level and, for a `TxProfile`, its transaction). Written only on `Accepted`; replaced by the s3.13.2 rules (same id, or same connector + purpose + stack level); a `TxProfile`'s record is dropped when its transaction stops. It is the operator's record of what limits are in force — the *effective* schedule is asked of the charger. |

A few cross-cutting things worth knowing about how this file works:

- **`next_transaction_id()` / `next_reservation_id()`** atomically increment a shared counter
  document (`counters` collection, no Beanie model of its own) via MongoDB's
  `find_one_and_update`. This is not decorative — two chargers starting a transaction in the same
  millisecond must never be given the same ID, and a random number or a document count both fail
  that guarantee.
- **`get_or_create` methods** (on `ConnectorStatus`, `ConfigurationEntry`, `LocalListState`,
  `Reservation`'s creation path) all guard against a real race: two independent code paths can
  try to create the *same* first-ever document for a connector/charger/key at once. They catch
  `DuplicateKeyError` on the losing insert and re-fetch rather than crash. This was discovered as
  an actual bug during this project's own testing, not designed in speculatively.
- **Updates that might race with another writer use `Document.set({...})`** — an atomic MongoDB
  `$set` on named fields — instead of the more obvious "mutate the Python object, then
  `.save()`". The latter reads the whole document, changes some fields in memory, and writes the
  *whole* document back; if a second writer does the same thing concurrently, whichever finishes
  last silently discards the other's changes. This actually happened (`ConnectorStatus.apply()`
  racing with `set_desired_availability()`) and was fixed by switching to `.set()`; the same
  pattern is used throughout for every document with more than one writer.
- **`hash_authorization_key` / `verify_authorization_key`** — chargers' keys are hashed with
  `scrypt` and a per-charger random salt before ever touching the database, per OCPP-J 1.6
  §6.2.2's own recommendation, so a database leak doesn't hand out live charger credentials.

## 9. `simulate_charge_point.py` — a fake charger for testing

This script plays the *charger* role for real, over a real WebSocket connection to a real
`main.py` — it is not a mock. Its `SimulatedChargePoint` class mirrors, on the charger side,
essentially every flow `main.py` implements on the Central System side: it keeps its own local
`ConnectorState` per connector (so it can correctly decide, e.g., whether a `ChangeAvailability`
should be `Accepted` or `Scheduled`), its own Authorization Cache and Local Authorization List,
its own configuration key/value store, and answers every Central-System-initiated message
(`RemoteStartTransaction`, `Reset`, `ChangeAvailability`, `UnlockConnector`, `GetConfiguration`,
`ChangeConfiguration`, `ClearCache`, `ReserveNow`, `CancelReservation`,
`GetLocalListVersion`, `SendLocalList`, `UpdateFirmware`, `GetDiagnostics`, `TriggerMessage`,
`SetChargingProfile`, `ClearChargingProfile`, `GetCompositeSchedule`).

Two run modes, chosen by CLI flags:

- **Scripted scenario** (default): boot → authorize → start → meter values → (optionally) stop,
  printing PASS/FAIL per step, useful as a smoke test you can run by hand.
- **`--idle-seconds N`**: boot, then just sit there for N seconds doing nothing on its own,
  reachable by `operate.py` commands from another terminal in the meantime. This is how you
  manually exercise every Central-System-initiated flow.

It also has a "reboot" mechanism: whenever a `Reset` or a successful firmware install happens,
it closes its own connection (simulating a power cycle) and the script's own `main()` loop
notices (`charge_point.reboot_reason`) and reconnects, re-running `BootNotification` — standing
in for OCPP 1.6 §4.2.1's requirement that the whole commissioning flow runs again after any
reboot.

A long list of `--reject-*` / `--fail-*` / `--decline-*` / `--simulate-fault` flags exist purely
to exercise the failure branches of every flow on demand (e.g. `--reject-reset`,
`--fail-firmware-download`, `--simulate-fault HighTemperature`).

## 10. `operate.py` — the operator's remote control

A synchronous CLI (plain `urllib`, no `asyncio`) that talks to `main.py`'s admin HTTP API. Every
subcommand corresponds 1:1 to one admin path and one `send_*`/`get_*` function in `main.py`:
`remote-start`, `remote-stop`, `change-availability`, `reset`, `unlock-connector`,
`get-configuration`, `change-configuration`, `clear-cache`, `reserve-now`,
`cancel-reservation`, `get-local-list-version`, `send-local-list`, `fault-history`,
`update-firmware`, `firmware-status`, `get-diagnostics`, `diagnostics-status`,
`trigger-message`, `set-charging-profile`, `clear-charging-profile`, `get-composite-schedule`,
`charging-profiles`. `remote-start` also takes an optional profile. It needs
`ADMIN_TOKEN` set to whatever `main.py` was started with, and `ADMIN_URL` if the server isn't at
the default `http://localhost:9000`.

## 11. `seed.py` and `register_charge_point.py` — provisioning

Two different ways to get a `ChargePoint` document into existence, matching Route A and Route B
from §7:

- **`seed.py`** registers one or more identities as already `Accepted`, writing their plaintext
  keys to `charge_point_credentials.json` (the *only* copy — the database keeps only a hash).
  Running it again on an already-seeded identity rotates its key rather than failing. This is
  what `simulate_charge_point.py`'s default scripted run expects to exist.
- **`register_charge_point.py`** is the fuller operator CLI: `register` (starts `Pending`, for
  Route B), `list`, `rotate`, `verify`. This is the tool an operator would actually use
  day-to-day; `seed.py` is closer to a test fixture.

## 12. `ocpp_client_auth.py`

One function, `basic_auth_header(identity, key, raw_password=False)`, shared between
`simulate_charge_point.py` and the test suite so both build the exact same HTTP Basic header the
exact same way, instead of two slightly-different reimplementations drifting apart over time.

## 13. The test suite

Everything in `tests/` runs against a **real** MongoDB (on a throwaway, uniquely-named database
per run) and a **real** in-process `main.py` server on an OS-assigned free port — never mocks,
never a stub charger that skips real OCPP framing. If MongoDB isn't reachable, the whole suite
skips cleanly (exit 0) rather than failing, via a session-scoped fixture in `conftest.py` that
pings it once up front.

- **`conftest.py`** — the shared fixtures every test file builds on: `db` (connects Beanie to a
  fresh database), `server` (starts `main.py`'s `serve()` on port 0), `registered_charge_point`
  (an already-`Accepted` charger + its key), `connected_charge_point` (an authenticated, running
  OCPP client connection using the plain `ocpp` library's own `ChargePoint` class — good enough
  when a test just needs to send/receive messages without any charger-side business logic).
- **`test_normal_charge_flow.py`** — the original happy-path and edge-case suite for boot →
  authorize → charge → stop, offline timestamps, illegal transitions, duplicate/orphaned stops,
  auth failures.
- **`test_remote_start_stop.py`** — remote start/stop, including the admin HTTP layer itself.
- **`test_change_availability.py`** — availability management, including the `ConnectorStatus`
  race fix.
- **`test_reset_and_unlock.py`** — Soft/Hard reset, the reboot/reconnect cycle, unlock connector.
- **`test_configuration.py`** — GetConfiguration/ChangeConfiguration/ClearCache/DataTransfer,
  including proving the authorization key is never leaked even if a misbehaving charger tries to
  report one.
- **`test_reservation.py`** — ReserveNow/CancelReservation, including the two different ways a
  reservation can end.
- **`test_local_auth.py`** — SendLocalList/GetLocalListVersion, plus bulk offline-timestamp
  trust and orphaned stops (this project's section F explicitly asked for "bulk", not just one
  more of each).
- **`test_fault_history.py`** — fault episodes opening/closing correctly, including that a
  repeated `Faulted` report doesn't open a second episode.
- **`test_firmware_and_diagnostics.py`** — the firmware/diagnostics request-and-progress
  lifecycle, including matching progress to the *most recent* request when more than one exists.
- **`test_trigger_message.py`** — TriggerMessage for every message type, the charger's own
  Rejected/NotImplemented answers, and that it works while `Pending`.
- **`test_charging_profiles.py`** — the pure maths in `charging_profiles.py`, no database or
  network: validation, the replacement/clearing rules, and the composite schedule against
  hand-worked numbers (including the spec's own "6 kW between 08:00 and 20:00" example).
- **`test_smart_charging.py`** — the Central System side of smart charging against a spec-
  following charger double: setting, replacing, clearing, composite schedules, the zero-limit
  suspend/resume cycle, a `TxProfile` ending with its transaction, and the admin API.
- **`test_api_rest.py` / `test_api_websocket.py`** — the public read-only API service (`api/`),
  the latter against a real Redis.

Test doubles that play the charger role in these files (`RemoteControllableChargePoint`,
`AvailabilityControllableChargePoint`, `ReservableChargePoint`, etc.) are deliberately **not**
`simulate_charge_point.py` reused — that script is a standalone entry point whose last line runs
`main()` at import time, so importing it into a test module would try to actually run it.

## 14. Running the whole thing yourself, step by step

```powershell
# 1. Install dependencies (Python 3.13, per README.md)
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Have MongoDB reachable at mongodb://localhost:27017 (or set MONGODB_URL)

# 3. Register a couple of test chargers, already trusted
.\.venv\Scripts\python.exe seed.py --identities CP001 CP002

# 4. Start the Central System (in one terminal)
$env:ADMIN_TOKEN = "some-shared-secret"     # needed for operate.py to work at all
.\.venv\Scripts\python.exe main.py

# 5. In a second terminal, run the full scripted demo session
.\.venv\Scripts\python.exe simulate_charge_point.py --cp-id CP001 --stop

# ...or leave it idling so you can command it from a third terminal:
.\.venv\Scripts\python.exe simulate_charge_point.py --cp-id CP001 --idle-seconds 60

# 6. In that third terminal, try things:
.\.venv\Scripts\python.exe operate.py remote-start CP001 TAG001
.\.venv\Scripts\python.exe operate.py fault-history CP001
.\.venv\Scripts\python.exe operate.py get-configuration CP001

# 7. Run the test suite
.\.venv\Scripts\python.exe -m pytest
```

## 15. What each of the ten OCPP "flows" actually does

`instructions/06-remaining-flows.md` organized the non-trivial-flow work into ten lettered
sections. All of them, A–J, are implemented. In plain terms, not spec-speak:

| # | Flow | In one sentence |
|---|---|---|
| A | Remote start/stop | An operator (or a driver's app) can start or stop a charging session on a connected charger without anyone touching the machine. |
| B | Availability | An operator can take a connector (or the whole charger) out of service for maintenance — immediately if it's free, deferred until the current session ends if it's busy. |
| C | Reset & unlock | An operator can reboot a misbehaving charger (gracefully or immediately) or pop open a stuck cable lock, remotely. |
| D | Configuration | An operator can read what settings a specific charger supports and tune them, and wipe its local authorization cache. |
| E | Reservation | A driver can book a specific connector ahead of time so it's guaranteed free when they arrive. |
| F | Offline & local auth | A charger keeps letting known cards through even when it can't reach this system right now, using a whitelist this system pushed ahead of time plus its own memory of recent decisions; and this system correctly trusts a backlog of sessions a charger reports late, after being offline for a while. |
| G | Fault handling | When a connector reports something genuinely wrong (overheating, a stuck lock, ...), that episode is recorded with a start and end time, not just visible while it's actively happening; and recovery always returns to exactly what the connector was doing before, never a guess. |
| H | Firmware & diagnostics | An operator can push a firmware update to a charger or pull its diagnostic logs remotely, watching real progress the whole way, including the reboot a firmware install causes. |
| I | Smart charging | An operator can cap how much power or current a charger (or one connector, or one session) may deliver — flat, or on a schedule such as "at most 6 kW between 08:00 and 20:00" — see what has been set, ask the charger what it will actually do over the next hour, and remove limits again. A limit of zero pauses a session; lifting it resumes it. |
| J | Remote trigger | Asking a charger to re-send a specific message right now (a status refresh, or — the only way — waking up a still-`Pending` charger enough to send something). |

**How smart charging is put together.** A *charging profile* is a schedule of limits with
metadata: its *purpose* (`ChargePointMaxProfile` caps the whole charger and only goes on
connector 0; `TxDefaultProfile` applies to every new session, on connector 0 for all connectors or
on one; `TxProfile` applies to one running session and ends with it), a *stack level* (when several
profiles are valid at once the highest wins, and a lower one shows through again when a higher one's
duration ends), and a *kind* (`Absolute` counts from a fixed start time, `Recurring` repeats
daily or weekly, `Relative` counts from the start of the session). The charger merges every
purpose by taking the lowest limit at each moment; that merged result is the *composite
schedule*, which `GetCompositeSchedule` asks for. `charging_profiles.py` holds the pure logic:
validation, and a `ChargingProfileSet` that implements what a *charger* does with profiles. The
Central System uses only the validation half — it never computes a composite itself, because the
charger alone knows its local limits — while the simulator and the tests' charger doubles use the
whole thing, and it is importable by tests (the simulator is not).

## 16. Design decisions and conventions worth knowing

- **The admin API shares a port with OCPP** because the `websockets` library version used here
  can serve plain HTTP GET responses from the exact same listening socket a WebSocket server
  uses, and adding a second port/dependency for occasional operator calls was judged to be more
  machinery than the problem needed. This is a POC-appropriate trade-off, not a scalability
  pattern — see the frontend/API guide for why the *public-facing* API is a wholly separate
  process instead.
- **A single shared secret (`ADMIN_TOKEN`), not per-operator accounts**, gates the admin API.
  There is no operator identity model in this project at all.
- **Firmware images are served as plain UTF-8 text, not real binary.** The `websockets` version
  in use only exposes a string-bodied HTTP response helper; constructing a raw binary HTTP
  response is possible but wasn't worth building for a proof-of-concept's stand-in firmware
  file. `firmware_files/example-1.0.0.txt` is that stand-in.
- **The authorization key is never persisted in plaintext, and never returned by
  `GetConfiguration`**, even if a misbehaving charger reports one anyway — this is enforced at
  the Central System, not trusted to charger behavior, because OCPP-J 1.6 only *should*s this,
  it doesn't *must* it.
- **Nothing Central-System-initiated is allowed while a charger is `Pending`**, except reading
  and writing its configuration and `TriggerMessage`. This is a deliberately broader interpretation than OCPP 1.6
  §4.2's literal text (which only explicitly names remote start/stop) — but it is applied
  consistently everywhere in this codebase, so it's safe to assume for anything new added later.
- **Every status change is validated against `connector_state_machine`, with one escape hatch**:
  when a charger reports a transition the table doesn't list, the Central System records what
  was *actually* reported (via `force_status`) and logs a warning, rather than silently
  overwriting it or rejecting the message outright — `StatusNotification.conf` has no way to
  tell a charger "you were wrong" in the first place, so rejecting would accomplish nothing.
- **Reservation/fault/firmware/diagnostics episodes all use the same "one document per episode,
  with an open/closed-with-a-reason shape" pattern** (`Transaction.is_open`/`stop_reason`,
  `Reservation.is_active`/`released_reason`, `FaultEvent.entered_at`/`cleared_at`). Reuse this
  shape for anything new that's naturally episodic, rather than inventing another.

## 17. Glossary

| Term | Meaning |
|---|---|
| CS | Central System — this backend. |
| CP | Charge Point — the physical charger. |
| Connector | One socket on a charge point; connector 0 means the whole unit. |
| idTag | A driver's RFID/app credential. |
| Authorization key | A charger's own 20-byte connection password (not an idTag). |
| Transaction | One charging session, from Start to Stop. |
| `.req` / `.conf` | OCPP's own terms for a request message and the reply that answers it. |
| Central-System-initiated | A message only the CS ever sends (e.g. Reset, RemoteStartTransaction). |
| Charger-initiated | A message only the charger ever sends (e.g. BootNotification, StatusNotification). |
| Pending / Accepted / Rejected | A charger's trust status with this Central System. |
| Available / Preparing / Charging / SuspendedEV / SuspendedEVSE / Finishing / Reserved / Unavailable / Faulted | The nine legal connector statuses (OCPP 1.6 §4.9). |
| Charging profile | A schedule of power or current limits a Central System installs on a charger (OCPP 1.6 §3.13). |
| Stack level | Precedence among a charger's profiles of the same purpose: the highest valid one prevails. |
| Composite schedule | The charger's own merge of all its profiles and local limits into one schedule — what it will actually do. |
| Route A / Route B | This project's own shorthand for "key installed before connecting" vs "key pushed over OCPP after connecting" (OCPP-J 1.6 §6.2.2 describes both). |
