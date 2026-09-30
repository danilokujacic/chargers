# Platform guide: the public charging map (tasks 07–11)

This is the guide to everything built on top of the OCPP Central System: the **public map of
Montenegro's charging sites**, the **public API** behind it, the **PlugShare import** that fills it,
the **Next.js website**, and the **demo fleet** of simulated chargers that makes it come alive. It
is written for two readers at once:

- **Business readers** — what each part is for, who it serves, what it can honestly claim, and
  where the risks are. Part I is for you; Part II's short background sections will also help.
- **Developers** — how each part works, why it is built the way it is, what data flows where, and
  where to change things. Parts III–V are for you.

It does not re-explain the Central System's internals (`main.py`, the connector state machine,
commissioning, key rotation, the ten OCPP flows). Those are in [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md),
and this guide links there instead of repeating it. What this guide *does* explain in full is every
message a charger sends, what is in it, why it exists, and exactly what this platform does with each
field — because that is what the map is made of.

Everything below was checked against the running system on 2026-09-30. Where it quotes a message,
an API response or a number, that is real output, not an illustration.

> **The demo is simulated.** The 134 PlugShare sites are real places, but while the demo fleet is
> seeded their statuses come from 195 *simulated* chargers, not from the real machines. This system
> has never talked to those chargers. The station panel says "Demo data — simulated chargers."
> [`instructions/12-remove-demo-fleet.md`](instructions/12-remove-demo-fleet.md) specifies how to remove it (not built yet).

## Contents

**Part I — The business view**
1. [What the platform is, in one page](#1-what-the-platform-is-in-one-page)
2. [Three kinds of site, and the honesty rules](#2-three-kinds-of-site-and-the-honesty-rules)
3. [What each part gives the business](#3-what-each-part-gives-the-business)
4. [Risks, limits and open questions](#4-risks-limits-and-open-questions)

**Part II — Background you need**

5. [EV charging in five minutes](#5-ev-charging-in-five-minutes)
6. [OCPP in five minutes](#6-ocpp-in-five-minutes)

**Part III — What a charger says, and what we do with it**

7. [Every message a charger sends](#7-every-message-a-charger-sends)
8. [Every command we send a charger](#8-every-command-we-send-a-charger)
9. [From a charger's message to a pin's colour](#9-from-a-chargers-message-to-a-pins-colour)

**Part IV — The parts, one by one**

10. [The data model (task 07)](#10-the-data-model-task-07)
11. [The Redis event seam (task 08 §E)](#11-the-redis-event-seam-task-08-e)
12. [The public API (task 08)](#12-the-public-api-task-08)
13. [The PlugShare import (task 09)](#13-the-plugshare-import-task-09)
14. [The website (tasks 10 and 11)](#14-the-website-tasks-10-and-11)
15. [The demo fleet (task 11)](#15-the-demo-fleet-task-11)
16. [Demo tooling: stories and videos](#16-demo-tooling-stories-and-videos)

**Part V — Operating it**

17. [Running everything](#17-running-everything)
18. [Configuration reference](#18-configuration-reference)
19. [Tests](#19-tests)
20. [Troubleshooting](#20-troubleshooting)
21. [Where to change things](#21-where-to-change-things)
22. [Glossary](#22-glossary)

---

# Part I — The business view

## 1. What the platform is, in one page

**The problem.** An EV driver in Montenegro wants to know two things before setting off: *where*
can I charge, and *can I charge there right now* — is a connector free, is it broken, does it fit
my car, how fast is it? Directories such as PlugShare answer the first question well and the second
one badly: their status data is crowd-reported and usually stale.

**The product.** A public website: one full-screen map of Montenegro. Every charging site is a pin,
coloured by its live status. Click a pin and a panel opens with every charger and every connector
at that site, each with its plug type, its power and its live status, updating by itself while
you watch. No login, no app.

**Where "live" comes from.** Live status can only come from a charger that is actually connected to
this system's Central System over OCPP (the standard protocol chargers speak — see §6). So the map
mixes two kinds of information and never blurs them:

- sites **this system operates**: live status, straight from the chargers;
- sites it only **knows about** (imported from PlugShare): shown faintly, status "unknown".

**Why the demo fleet exists.** A new operator has almost no chargers of its own, so a live map
would be nearly empty. The demo fleet turns the 134 imported PlugShare sites into *simulated* sites,
each with simulated chargers (one per PlugShare station, one connector per outlet) that connect to
the Central System exactly like real ones and run realistic charging sessions. The map then shows a
whole country of chargers that are free, busy or broken, changing as the audience watches. It is a
demonstration of the platform, clearly labelled, and fully removable.

**The pieces, from the charger to the screen:**

```
 charger (real or simulated)
      │  OCPP over WebSocket, port 9000
      ▼
 main.py — the Central System ──writes──▶ MongoDB ◀──reads── api/ — the public API (port 8000)
      │                                                         ▲          │
      └──────── publishes events ──▶ Redis ── subscribed by ────┘          │ REST + WebSocket
                                                                            ▼
                                                        charger-fe — the website (port 3000)
```

| Piece | Task | What it does |
|---|---|---|
| Data model (`Site`, locations, connector specs) | 07, 11 | Lets the system know *where* chargers are and what plugs they have |
| Redis event publishing in `main.py` | 08 | Tells the outside world, instantly, whenever something changes on a charger |
| Public API (`api/`) | 08 | The only thing the public can reach: read-only REST and live WebSockets |
| PlugShare import | 09 | Fills the map with 134 real, named, located sites |
| Website (`../charger-fe`) | 10, 11 | The map, the station panel, live updates |
| Demo fleet | 11 | 195 simulated chargers so the map is alive for a demo |

## 2. Three kinds of site, and the honesty rules

Every site has a `source`, and the source decides what the platform may claim about it.

| `source` | What it means | Status shown | Chargers counted from | On the map |
|---|---|---|---|---|
| `operator` | A real site this system operates: at least one real charger connects here | Live | Real `ChargePoint` records | Solid pin, live colour |
| `external_reference` | A real place we only know about (imported from PlugShare). No connection to its chargers, ever | Always `unknown` | PlugShare's station count, frozen at import | Faint grey pin |
| `simulated` | A real place whose chargers are *simulated* for the demo | Live (from the simulation) | The simulated `ChargePoint` records | Solid pin, live colour, "Demo data" note in the panel |

Three rules follow from this, and every part of the system respects them:

1. **"Unknown" is not "unavailable".** Grey `unavailable` means "we checked, and it is switched off".
   A site we have no connection to is `unknown` — faint, deliberately the least prominent pin —
   because saying "unavailable" would claim a check we never made.
2. **Third-party status is never imported as live status.** PlugShare's "in use", "under repair" or
   "out of order" flags are snapshots that were already stale when exported. They are discarded
   (§13). The one exception is the demo: PlugShare's "out of order" outlets become *simulated*
   faulted connectors, and that is labelled as demo data.
3. **Simulated data is always marked and always removable.** A simulated charger has
   `simulated = true`, a simulated site has `source = simulated`, and demo driver cards belong to
   the `DEMO-FLEET` group. Nothing else is ever used to find demo data, so removal (task 12) can
   take all of it and nothing more.

## 3. What each part gives the business

| Part | Capability | Why it matters | Honest limit |
|---|---|---|---|
| Public map | Every site in the country, one pin each, coloured by live status | A driver sees at a glance where to go | Only operated (or simulated) sites have live colour |
| Clusters | Nearby pins group into a numbered circle that takes the most useful colour inside it | The whole country is readable at once | A cluster's colour is a summary: red means *some* site inside is broken |
| Station panel | Every charger and connector at a site: plug, AC/DC, rated kW, live status, session and energy | A driver knows whether their car fits and how fast it will charge before driving there | Power is shown only where PlugShare published it (75 of 255 outlets); never guessed |
| Live updates | The panel updates within about a second; pins within 10 seconds | Trust: what you see is what is happening now | Pins refresh by polling every 10 s, not instantly |
| Search | Type a town, the map moves there | Drivers think in place names, not coordinates | Uses the free public Nominatim service, which has strict rate limits (§4) |
| Public API | Read-only, unauthenticated REST and WebSocket feeds | Anyone (the website, a partner, an app) can build on live charger data | Read-only by design: nothing public can command a charger |
| PlugShare import | 134 real, named, located sites | The map is full from day one | Frozen at import time; refreshed only by re-running it |
| Demo fleet | 195 simulated chargers with realistic sessions, faults, reservations, reboots | A convincing live demo without owning hardware | It is simulated; it must be labelled and removed after the demo |
| Operator controls (`operate.py`) | Remote start/stop, switch off/on, reserve, reboot — each visible on the public map | Shows the operator side of the business: remote support and maintenance | Operator tools are command-line only; there is no operator web UI |

## 4. Risks, limits and open questions

These are real, found while building and checking the system. The first two should be fixed before
the public API is exposed to the internet.

1. **Driver card numbers leak through the public WebSocket.** When a session starts, `main.py`
   publishes a `transaction_started` event that includes the driver's `id_tag` (usually the number
   printed on their RFID card), and a `reservation_created` event does the same. The public API
   forwards events to browsers unchanged (`api/live.py`), so anyone watching a site's WebSocket can
   read the card numbers of the drivers using it. The REST endpoints do not expose them. **Fix:**
   strip `id_tag` from events in `api/live.py` before forwarding, or stop publishing it.
2. **The map cannot tell when a charger has silently disappeared.** A charger's last reported
   status is kept until it reports another one. If a charger (or the demo fleet) is switched off
   cleanly, it reports `Unavailable` first and the map turns grey — correct. But if it loses power
   or its network, it sends nothing, and its pin keeps its last colour (say, green) indefinitely.
   Heartbeats (§7.2) are answered but not recorded, so there is nothing to detect the silence with.
   **Fix:** record the last heartbeat time and show "offline" after a few missed intervals.
3. **PlugShare data terms.** The 134 sites come from a PlugShare export. Whether that data may be
   republished on a public map, and whether attribution is required, depends on the terms under
   which the export was obtained — check them. The import keeps each site's PlugShare link
   (`external_url`) for attribution, but the API and the website do not currently display it.
4. **Nominatim, the search provider,** is a free public service limited to about one request per
   second and not meant for commercial traffic. Fine for a demo; for production, switch to
   MapTiler's own geocoding (already paid for, same API key).
5. **The MapTiler key is public by design** (it is visible in every browser). Restrict it by
   website address in the MapTiler dashboard, or someone else can use your quota.
6. **No rate limiting** on the public API yet. It is read-only, so heavy use is not dangerous, but
   it should sit behind a reverse proxy with basic flood protection before going public.
7. **Scale assumptions.** Every `GET /api/v1/sites` reads every site, charger and connector status;
   every open browser does that every 10 seconds. At Montenegro's scale (hundreds of sites) that is
   fine. At thousands of sites or many thousands of viewers it would need caching.
8. **Demo removal is not built.** Task 12 (`remove_demo_fleet.py`) is specified but not
   implemented. Until it is, the demo data stays in the database (the map shows 124 grey and 10 red
   simulated sites when the fleet is stopped).
9. **Operator credentials.** The admin API is protected by one shared token (`ADMIN_TOKEN`); in this
   development setup it is a weak placeholder. Use a long random value anywhere real.

---

# Part II — Background you need

## 5. EV charging in five minutes

**A site, a charger, a connector.** A **site** is a place — a fuel station, a hotel car park. It has
one or more **chargers** (the machines; OCPP calls one a *charge point*). Each charger has one or
more **connectors**: the plugs, numbered 1, 2, 3… Connector **0** is special: it means "the charger
as a whole", never a plug. In this system a site is a `Site` document, a charger is a `ChargePoint`
document, and a connector is identified by the pair (charger identity, connector number).

**Plug types.** The shape of the plug decides which cars can use it. The ones in the data:

| Plug | Current | Typical use |
|---|---|---|
| **Type 2** | AC | The standard European plug; slower charging (up to 22 kW, often 7–11) |
| **CCS2** | DC | Fast charging for most modern European cars (50–350 kW) |
| **CHAdeMO** | DC | Fast charging for older Japanese cars (e.g. early Nissan Leaf) |
| Wall (Euro), Three Phase, Caravan Mains Socket | AC | Ordinary sockets; very slow |
| J-1772 | AC | The North American AC plug; rare here |

**AC vs DC.** With **AC** the car's own on-board charger converts the power, which caps the speed.
With **DC** the station does the conversion and feeds the battery directly, which is why DC is fast.

**kW and Wh.** **kW** is *power* — how fast energy flows (a 200 kW charger is a fast one). **Wh**
(watt-hours) is *energy* — how much has flowed. A car charging at 22 kW for one hour takes
22,000 Wh (22 kWh). Chargers contain an energy meter whose reading, the **register**, only ever
goes up; a session's energy is the register at the end minus the register at the start.

**idTag.** A driver's credential, usually the number printed on an RFID card (or a token from an
app). The charger sends it to the Central System to ask "may this person charge?". idTags can
belong to a **group** (a *parent* idTag); any card in a group may stop a session another card of
the same group started — a family or company fleet, for example.

**A session and a transaction.** A driver plugs in and presents a card: that is a *session*. The
billable part, from the moment energy may flow until it stops, is a **transaction**, which the
Central System numbers (the *transaction id*). The panel shows it as "Session #2013".

**Connector statuses** — the charger reports one of these for every connector, and they are what
the map is coloured by:

| Status | Meaning | Pin colour it pushes towards |
|---|---|---|
| `Available` | Free, nothing plugged in | Green |
| `Preparing` | A driver has started: cable in, card being checked | Amber |
| `Charging` | Energy flowing | Amber |
| `SuspendedEV` / `SuspendedEVSE` | Plugged in but paused, by the car / by the charger | Amber |
| `Finishing` | Charging over, cable still plugged in | Amber |
| `Reserved` | Held for one particular driver | Violet |
| `Unavailable` | Switched off, out of service | Grey |
| `Faulted` | Broken | Red |

## 6. OCPP in five minutes

**OCPP** (Open Charge Point Protocol, version 1.6 here) is the standard language between a charger
and the backend that manages it, called the **Central System** (in this project, `main.py`).

- **The charger dials in, never the other way round.** It opens a WebSocket to
  `ws://<central system>:9000/<its identity>` and keeps it open for as long as it runs.
- **It proves who it is** with HTTP Basic authentication on that first request: username = its
  **identity** (e.g. `PS-2946795`), password = its 20-byte **authorization key** (sent as 40 hex
  characters). The Central System stores only a salted hash of the key. This key authenticates the
  *machine*; an idTag authorizes a *driver*. Never confuse the two.
- **Messages are JSON arrays** of three shapes: a request `[2, id, "Action", {payload}]` (a *CALL*),
  a reply `[3, id, {payload}]` (a *CALLRESULT*), or an error `[4, id, code, description, {}]`. The
  `id` pairs each reply with its request. OCPP names a request `Foo.req` and its reply `Foo.conf`.
- **Both directions share the one connection.** The charger *reports* (boot, status, sessions,
  meter readings) and the Central System *commands* (start, stop, switch off, reboot) over the
  same socket. That is why the Central System keeps a registry of open connections: an operator
  command has to find the right socket.
- **One request at a time in each direction.** A sender waits for the reply before sending the next
  request, so messages from one charger arrive in order.

For how `main.py` implements all of this, see [`SYSTEM_OVERVIEW.md` §4–§5](SYSTEM_OVERVIEW.md#4-how-a-chargers-life-actually-goes-end-to-end).

---

# Part III — What a charger says, and what we do with it

Everything the map shows is built from a handful of messages a charger sends. This part goes
through each one: **why it exists, when it is sent, what is in it, what the Central System answers,
what gets stored where, and what (if anything) the public sees.** The frames are real, taken from
`main.py`'s log while the demo fleet ran.

A reading key for the tables: *Stored* names the MongoDB collection and field; *Published* names
the Redis event the public API receives (§11); *Seen* says where it surfaces on the website.

## 7. Every message a charger sends

### 7.1 BootNotification — "I have just started; here is what I am"

**Why it exists.** A charger that has just powered up (or rebooted, or reconnected) must introduce
itself before anything else, and must learn whether the Central System accepts it. Until the answer
is `Accepted`, OCPP forbids it from doing anything useful.

**When.** The first message on every new connection.

```json
[2, "72b77f0e-…", "BootNotification", {"chargePointModel": "PlugShareMock", "chargePointVendor": "DemoFleet"}]
```

| Field | Required | What it is | What we do with it | Why it is useful |
|---|---|---|---|---|
| `chargePointVendor` | yes | Manufacturer (≤20 chars) | Stored: `charge_points.charge_point_vendor` | Fleet inventory; which vendor to call for support |
| `chargePointModel` | yes | Model (≤20 chars) | Stored: `charge_points.charge_point_model` | Inventory; model-specific quirks and firmware |
| `chargePointSerialNumber` | no | The unit's serial number | Stored if sent | Warranty claims, matching a physical unit to its record |
| `chargeBoxSerialNumber` | no | Older, deprecated serial field | Stored if sent | Still sent by older chargers; kept rather than lost |
| `firmwareVersion` | no | Software version on the charger | Stored if sent | Knowing which units need an update, or which bug a unit may have |
| `iccid`, `imsi` | no | The SIM card in a charger's mobile modem | Stored if sent | Diagnosing connectivity; talking to the mobile operator |
| `meterSerialNumber`, `meterType` | no | The built-in energy meter | Stored if sent | Billing audits: which certified meter measured the energy |

Every boot also stamps `charge_points.last_seen_at`. A field a charger leaves out does not erase a
value an earlier boot reported. The demo fleet sends only vendor `DemoFleet` and model
`PlugShareMock`.

**The answer:**

```json
[3, "72b77f0e-…", {"currentTime": "2026-09-29T23:27:52.475977+00:00", "interval": 30, "status": "Accepted"}]
```

- `status` — `Accepted`, `Pending` or `Rejected`, straight from `charge_points.registration_status`.
  `Pending` means "known but not yet trusted": the Central System then pushes it a fresh key
  (onboarding, see [`SYSTEM_OVERVIEW.md` §7](SYSTEM_OVERVIEW.md#7-commissioningpy--onboarding-a-new-charger)).
  The demo chargers are registered `Accepted`, as if their key had been installed at the factory.
- `interval` — 30 seconds. When accepted, how often to send a Heartbeat; otherwise, how long to wait
  before booting again.
- `currentTime` — the Central System's clock, so the charger can set its own. That matters because
  every session timestamp comes from the charger's clock (a charger that was offline reports late,
  and billing must use when charging really happened).

After an accepted boot the Central System re-sends any "switched off" setting it has on record for
that charger (§8.3), because a charger that rebooted may have forgotten it.

*Published:* nothing. *Seen:* nothing directly — but after booting, a charger reports every
connector's status, and that is what makes its pin appear coloured.

### 7.2 Heartbeat — "I am still here"

**Why it exists.** A quiet charger (nothing plugged in for hours) sends nothing else, so the Central
System needs a regular sign of life; the reply also keeps the charger's clock in step.

**When.** Every `interval` seconds from the boot reply (30 s). The demo fleet sends one every 30 s
per charger, which is 6–7 a second across all 195.

```json
[2, "6cf14098-…", "Heartbeat", {}]
[3, "6cf14098-…", {"currentTime": "2026-09-29T23:28:52.966781+00:00"}]
```

*Stored:* **nothing.** *Published:* nothing. *Seen:* nothing. This is the offline-detection gap
listed in §4: because heartbeats are not recorded, the system cannot notice a charger that has gone
silent. (The WebSocket connection itself also sends low-level ping frames every 20 s, which is how
a dropped connection is eventually noticed by the software, but that is not stored either.)

### 7.3 StatusNotification — "this connector is now …" (the most important message)

**Why it exists.** It is how the Central System knows the state of every plug: free, in use,
reserved, switched off, broken. Everything coloured on the map comes from it.

**When.** Whenever a connector's status changes; for every connector right after an accepted boot;
and on request (a TriggerMessage command, §8.7).

```json
[2, "0dfe1759-…", "StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": "Available", "timestamp": "2026-09-29T23:27:52.477127+00:00"}]
[2, "b2576a35-…", "StatusNotification", {"connectorId": 1, "errorCode": "OtherError", "status": "Faulted", "timestamp": "2026-09-29T23:27:52.479912+00:00", "info": "Out of order (PlugShare report)"}]
```

| Field | Required | What it is | What we do with it | Why it is useful |
|---|---|---|---|---|
| `connectorId` | yes | Which plug; 0 = the whole charger | Key of the `connector_statuses` row (identity + connector) | Status is per plug, not per charger |
| `status` | yes | One of the nine statuses in §5 | Checked against OCPP's legal transitions, then stored: `connector_statuses.status` | Drives the pin colour and the panel |
| `errorCode` | yes | `NoError`, or what is wrong: `ConnectorLockFailure`, `EVCommunicationError`, `GroundFailure`, `HighTemperature`, `InternalError`, `OverCurrentFailure`, `OverVoltage`, `PowerMeterFailure`, `ReaderFailure`, `UnderVoltage`, `WeakSignal`, `OtherError`, … | Stored: `connector_statuses.error_code`; shown in red under the status in the panel when not `NoError` | Tells maintenance what to fix before anyone drives out |
| `timestamp` | no | When it happened, charger's clock | Not stored (the row's `updated_at` is server time) | Ordering for late-delivered messages |
| `info` | no | Free text, ≤50 chars | Stored: `connector_statuses.info` | Human-readable detail; the demo puts "Out of order (PlugShare report)" here |
| `vendorId`, `vendorErrorCode` | no | Manufacturer-specific error detail | Stored | The vendor's own diagnostic code |

What happens on receipt, in order:

1. **Legality check.** OCPP defines which status changes are allowed (e.g. `Preparing` cannot go
   straight to `Unavailable`). An illegal change is logged as a warning but still recorded: the
   charger is the authority on its own hardware, and the reply has no way to tell it otherwise.
   (Rules: [`SYSTEM_OVERVIEW.md` §6](SYSTEM_OVERVIEW.md#6-connector_state_machinepy--the-rulebook-for-connector-status).)
2. **Stored** in `connector_statuses`, one row per (charger, connector), overwritten each time.
3. **Published** as `connector_status` with `{status, error_code}`.
4. **Fault history.** Entering `Faulted` opens a `fault_events` record (and publishes
   `fault_opened`); leaving it closes the record (and publishes `fault_cleared`). A connector that
   is still faulted when it reboots and re-reports `Faulted` does not open a second record.

*The reply* is empty (`{}`): OCPP gives the Central System no way to disagree.

*Seen:* the connector's status text and colour in the panel (instantly, over the WebSocket); the
site's pin colour (within 10 s, §9).

### 7.4 Authorize — "may this driver charge?"

**Why it exists.** The charger does not know who is allowed to charge; the Central System does.

**When.** A driver presents a card, or an operator's remote start asks the charger to act as if one
had been presented (§8.1).

```json
[2, "8d1ce5c9-…", "Authorize", {"idTag": "DEMO-3395127-1"}]
[3, "8d1ce5c9-…", {"idTagInfo": {"status": "Accepted", "parentIdTag": "DEMO-FLEET"}}]
```

| Reply field | What it is | Why |
|---|---|---|
| `status` | `Accepted`, `Blocked`, `Expired`, `Invalid` (unknown card) or `ConcurrentTx` (this card already has a session running elsewhere) | Whether to let the driver charge |
| `parentIdTag` | The card's group | Lets the charger know which other cards may stop this session |
| `expiryDate` | When the card stops being valid, if it does | The charger may cache the answer until then |

The decision uses the `id_tags` collection: unknown → `Invalid`; past its expiry date → `Expired`;
otherwise its stored status — and if that is `Accepted` but the card already has an open
transaction, `ConcurrentTx` (one card cannot run two chargers at once). *Stored:* nothing new.
*Published:* nothing. *Seen:* nothing.

In the demo, every connector has its own card, `DEMO-<station>-<connector>`, in the `DEMO-FLEET`
group. One shared demo card would have made every connector after the first answer `ConcurrentTx`.
A separate card, `DEMO-REMOTE`, exists for trying remote starts by hand.

### 7.5 StartTransaction — "charging has started"

**Why it exists.** This is the start of the billable session. The Central System numbers it, so
that every later message about it can refer to it.

```json
[2, "a81cf72c-…", "StartTransaction", {"connectorId": 1, "idTag": "DEMO-3395127-1", "meterStart": 970, "timestamp": "2026-09-29T23:27:53.168514+00:00"}]
[3, "a81cf72c-…", {"transactionId": 1480, "idTagInfo": {"status": "Accepted", "parentIdTag": "DEMO-FLEET"}}]
```

| Field | What it is | What we do with it | Why it is useful |
|---|---|---|---|
| `connectorId` | Which plug | `transactions.connector_id` | Which plug the session is on |
| `idTag` | Which card started it | Re-checked (as in Authorize); `transactions.id_tag` | Who to bill; who may stop it |
| `meterStart` | The energy register at the start, in Wh | `transactions.meter_start` | Energy = meter at stop − meter at start |
| `timestamp` | When, by the charger's clock | `transactions.started_at` | Billing by real time, even if delivered late |
| `reservationId` | The reservation this uses, if any | Releases that reservation | A reserved connector becomes an ordinary session |

What happens: the transaction id comes from an atomic counter (`counters` collection), so two
chargers starting at the same instant can never get the same number; a `transactions` document is
created with `is_open = true`; `transaction_started` is published with
`{transaction_id, id_tag, meter_start}`; any matching reservation is released (and
`reservation_released` published). If the card turns out not to be accepted, the transaction is
still recorded — the charger may already be delivering energy — and the charger is expected to end
it with reason `DeAuthorized`.

*Seen:* "Session #1480" appears under the connector in the panel.

### 7.6 MeterValues — "this much energy so far"

**Why it exists.** A session can last hours. Periodic readings show progress, give billing evidence
along the way, and survive if the final message is lost.

```json
[2, "dfb02d4a-…", "MeterValues", {"connectorId": 1, "transactionId": 1480, "meterValue": [{"timestamp": "2026-09-29T23:28:08.738703+00:00", "sampledValue": [{"value": "994", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]}]}]
```

| Field | What it is | What we do with it |
|---|---|---|
| `connectorId` | Which plug | — |
| `transactionId` | The session it belongs to (may be absent) | With it: appended to `transactions.meter_values` and published. Without it: stored as `connector_statuses.last_meter_values` (a reading outside any session) |
| `meterValue[].timestamp` | When measured | Kept with the reading |
| `sampledValue[].value` | The number, as a string | — |
| `sampledValue[].measurand` | *What* was measured. `Energy.Active.Import.Register` (the register, Wh) is the default; chargers may also send power (W), current (A), voltage (V), battery charge (%) | The API and the website use the energy register |
| `sampledValue[].unit` | `Wh`, `kWh`, `W`, `A`, `V`, `Percent`, … | — |

*Published:* `meter_value` with `{transaction_id, meter_value}` — the latest reading only.
*Seen:* "Session #1480 · 994 Wh" in the panel, counting up; and `current_transaction.latest_meter_value`
in `GET /api/v1/charge-points/{identity}`. The demo fleet sends one every 15 s during a session.

### 7.7 StopTransaction — "charging has ended, and why"

**Why it exists.** It closes the billable session with the final register reading, and says who or
what ended it.

```json
[2, "a33626ca-…", "StopTransaction", {"meterStop": 1637, "timestamp": "2026-09-29T23:38:44.717692+00:00", "transactionId": 1869, "reason": "Local", "idTag": "DEMO-3550672-1"}]
[2, "a347a83e-…", "StopTransaction", {"meterStop": 4879, "timestamp": "2026-09-29T23:27:52.713730+00:00", "transactionId": 1478, "reason": "PowerLoss"}]
```

| Field | What it is | What we do with it |
|---|---|---|
| `transactionId` | Which session | Finds the `transactions` document |
| `meterStop` | Register at the end, Wh | `transactions.meter_stop` |
| `timestamp` | When it ended | `transactions.stopped_at` |
| `idTag` | Card that stopped it, if one was presented | `transactions.stopped_by_id_tag`; checked: the same card, or one from the same group, may stop it |
| `reason` | How it ended (below) | `transactions.stop_reason` |

**Stop reasons you will see, and what produces them in this system:**

| Reason | Meaning | Produced by |
|---|---|---|
| `Local` | The driver ended it at the charger | A demo session reaching its planned length |
| `Remote` | Stopped from the Central System | `operate.py remote-stop` |
| `Other` | Anything else | The demo fleet stopping gracefully (Ctrl+C) |
| `PowerLoss` | The charger lost power mid-session | The demo fleet, after a crash or dropped connection, closing what it left open |
| `SoftReset` / `HardReset` | A reset command | `operate.py reset` |
| `DeAuthorized` | The card was refused at StartTransaction | A charger whose start was not accepted |
| `EVDisconnected`, `EmergencyStop`, `UnlockCommand`, `Reboot` | As named | Real chargers (not the demo) |

The first delivery of a stop wins: chargers re-send messages after reconnecting, and a repeat must
not overwrite the record. A stop for a transaction the Central System never saw start is still
recorded, marked `incomplete`. *Published:* `transaction_stopped` with `{transaction_id, meter_stop}`.
*Seen:* the session line disappears from the panel; the charger then reports `Finishing`.

*The reply* carries `idTagInfo` only when the request carried an `idTag`.

### 7.8 The rest

| Message | Why a charger sends it | What we do | On the map |
|---|---|---|---|
| `FirmwareStatusNotification` | Progress of a firmware update (Downloading, Installed, …) | Stored in `firmware_updates` | Nothing |
| `DiagnosticsStatusNotification` | Progress of a log upload | Stored in `diagnostics_requests` | Nothing |
| `DataTransfer` | Vendor-specific extension | Answered "unknown vendor" | Nothing |

## 8. Every command we send a charger

Commands go the other way: an operator runs `operate.py`, which calls `main.py`'s admin API over
plain HTTP (`http://localhost:9000/admin/...?token=…`), which sends the OCPP request over that
charger's open connection. The first six below all change what the public sees, which is why the
demo uses them.

```
operate.py ──HTTP──▶ main.py /admin/… ──OCPP request──▶ charger ──reply, then status messages──▶ main.py ──▶ Redis ──▶ API ──▶ browser
```

### 8.1 RemoteStartTransaction — start a session for a driver

**Why.** A driver's card will not read, or they started from an app or by SMS. The operator starts
it for them.

```json
[2, "6da86ef1-…", "RemoteStartTransaction", {"idTag": "DEMO-REMOTE"}]
```

Fields: `idTag` (required), `connectorId` (optional — without it the charger chooses), and an
optional charging-power limit for the session. The reply is `Accepted` ("I will try") or
`Rejected`. Accepted is not the session: the charger then authorizes the card, reports
`Preparing`, sends StartTransaction and reports `Charging`, exactly as if a driver had tapped a card.
The simulator picks the lowest-numbered free connector, and **rejects** the request if that
connector already has a session (starting a second one would leave the first open forever).

*Seen:* Preparing → Charging in the panel, a session number, energy counting up; the pin turns amber
if that was the site's last free connector.

### 8.2 RemoteStopTransaction — end a session remotely

`{"transactionId": 2003}` → `Accepted`/`Rejected`. The charger sends StopTransaction with reason
`Remote` and reports `Finishing`. *Seen:* Finishing; in the demo, `Available` 5–15 s later, when
the simulated driver unplugs.

### 8.3 ChangeAvailability — switch a connector (or a whole charger) off or on

`{"connectorId": 1, "type": "Inoperative"}` (connector 0 means the whole charger) → `Accepted`,
`Rejected`, or `Scheduled` (a session is running; it switches off when the session ends). The
operator's choice is stored (`connector_statuses.desired_availability`) and re-sent after every
reboot, because OCPP requires "switched off" to survive a restart. *Seen:* `Unavailable`, grey.
Back on with `Operative`.

### 8.4 ReserveNow and 8.5 CancelReservation — hold a connector for one driver

```json
[2, "040ad3e0-…", "ReserveNow", {"connectorId": 1, "expiryDate": "2026-09-30T00:05:11+00:00", "idTag": "DEMO-REMOTE", "reservationId": 2}]
[2, "b6bbadcd-…", "CancelReservation", {"reservationId": 2}]
```

The Central System numbers reservations itself. ReserveNow answers `Accepted`, `Occupied`,
`Faulted`, `Unavailable` or `Rejected`; only an accepted one is stored (`reservations`) and
published (`reservation_created`). It ends when the driver starts (consumed), when cancelled, or at
its expiry (a sweep runs every minute) — each publishes `reservation_released` with the reason.
*Seen:* `Reserved`, violet; back to Available on cancel.

### 8.6 Reset — reboot a charger

`{"type": "Soft"}` restarts the software: running sessions are ended first (reason `SoftReset`).
`{"type": "Hard"}` is a power cycle: sessions are cut off. Either way the charger drops the
connection, reconnects, boots and reports its connectors again. *Seen:* Finishing, then Available a
few seconds later.

### 8.7 Commands with no visible effect on the map

| Command | What it is for |
|---|---|
| `UnlockConnector` | Release a cable stuck in the socket |
| `TriggerMessage` | Ask the charger to send a status, meter reading, boot or heartbeat now |
| `GetConfiguration` / `ChangeConfiguration` | Read or change the charger's settings (e.g. heartbeat interval) |
| `ClearCache`, `SendLocalList`, `GetLocalListVersion` | Manage the cards a charger may accept while offline |
| `SetChargingProfile`, `ClearChargingProfile`, `GetCompositeSchedule` | Smart charging: power limits over time |
| `UpdateFirmware`, `GetDiagnostics` | Software updates; fetching logs |

All of them are available through `operate.py` (run `python operate.py --help`) and explained in
[`SYSTEM_OVERVIEW.md` §15](SYSTEM_OVERVIEW.md#15-what-each-of-the-ten-ocpp-flows-actually-does).

## 9. From a charger's message to a pin's colour

One demo session, followed through every part of the system. Times are what the system actually
does; the payloads are real.

```
 charger / fleet            main.py                 MongoDB           Redis              api/                 browser
 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Authorize ───────────────▶ check id_tags ◀────────── read
            ◀─ Accepted ───
 Status: Preparing ───────▶ legal? store ───────────▶ connector_statuses
                            publish ──────────────────────────────▶ connector_status ─▶ to sockets watching  ─▶ panel row: "Preparing" (amber text)
                                                                                         this charger's site
 StartTransaction ────────▶ next id, insert ────────▶ transactions (open)
                            publish ──────────────────────────────▶ transaction_started ─────────────────────▶ "Session #2013"
            ◀─ id 2013 ────
 Status: Charging ────────▶ store + publish ─────────────────────▶ connector_status ─────────────────────────▶ "Charging"
 MeterValues (every 15 s) ▶ append + publish ────────────────────▶ meter_value ──────────────────────────────▶ "Session #2013 · 1903 Wh"
                                                                                 every 10 s: GET /sites ◀── map polls
                                                                                 aggregate_status ────────▶ pin turns amber (if all busy)
 StopTransaction (Local) ─▶ close + publish ─────────────────────▶ transaction_stopped ─────────────────────▶ session line disappears
 Status: Finishing ───────▶ store + publish ─────────────────────▶ connector_status ─────────────────────────▶ "Finishing"
 (5–15 s later)
 Status: Available ───────▶ store + publish ─────────────────────▶ connector_status ─────────────────────────▶ "Available"; pin green within 10 s
```

**Two update paths, on purpose.**

- **The open panel** listens on a WebSocket, `/api/v1/ws/sites/{site}`, and changes within a
  fraction of a second of the charger's message. The chain is: `main.py` → Redis → the API's
  subscriber → every socket subscribed to that site.
- **The pins** re-read `GET /api/v1/sites` every 10 seconds while the tab is visible. Measured
  during the demo: every status change reached its pin between 0.1 and 10.6 s after the API
  reported it. A pin does not open a socket per site, because 136 sockets per browser would cost
  far more than one small request every 10 seconds.

**Where each fact lives afterwards.** The status in `connector_statuses`; the session in
`transactions` (with every meter reading); faults in `fault_events`; reservations in `reservations`.
Redis keeps nothing — it only relays. So a browser that opens the panel later starts from MongoDB
(through REST), then follows Redis live.

**A real event on the wire** (as the API receives it from Redis, and forwards it to browsers):

```json
{
  "type": "meter_value",
  "charge_point_identity": "PS-1567262",
  "connector_id": 3,
  "at": "2026-09-29T20:38:40.511910+00:00",
  "data": {
    "transaction_id": 1314,
    "meter_value": {"timestamp": "2026-09-29T20:38:40.503066+00:00",
                    "sampled_value": [{"value": "1793", "measurand": "Energy.Active.Import.Register", "unit": "Wh"}]}
  }
}
```

---

# Part IV — The parts, one by one

## 10. The data model (task 07)

**Where:** `models.py`. Beanie documents over MongoDB (database `ocpp_poc`).

### 10.1 `Site` — a place that hosts chargers

A generic place, not a "gas station" model: a fuel station, a hotel car park and a public car park
are the same shape of thing (a name, coordinates, some chargers), so one document with a
`site_type` label serves all of them.

| Field | Type | What it is for |
|---|---|---|
| `name` | str | The label on the map and in the panel |
| `site_type` | `SiteType`: `gas_station`, `hotel`, `parking`, `other` | An operator-facing category. Imported sites are all `other` (the export has no reliable venue type) |
| `latitude`, `longitude` | float | Where the pin goes (WGS84 degrees) |
| `address` | str or null | Shown under the name in the panel |
| `source` | `SiteSource`: `operator`, `external_reference`, `simulated` | What the platform may claim about it (§2) |
| `external_id` | str or null | The PlugShare location id. Used only so a re-import updates instead of duplicating |
| `external_url` | str or null | The PlugShare page, kept for attribution (not currently shown) |
| `connector_types` | list[str] or null | Plug types at the site, as plain names (`"CCS2"`, `"Type 2"`); shown as "Connectors: …" |
| `external_charge_point_count` | int or null | PlugShare's station count. Only meaningful for `external_reference` sites (see 10.4) |
| `created_at`, `updated_at` | datetime | Bookkeeping |

### 10.2 `ChargePoint` — additions for the map (07) and the demo (11)

The existing charger record (identity, key hash, registration status, boot details — see
[`SYSTEM_OVERVIEW.md` §8](SYSTEM_OVERVIEW.md#8-modelspy--everything-stored-in-mongodb)) gained:

| Field | Task | What it is for |
|---|---|---|
| `site_id` | 07 | Which `Site` the charger belongs to |
| `latitude`, `longitude` | 07 | The charger's own position, used only when it has no site |
| `simulated` | 11 | `true` only for demo chargers. The selector demo removal uses — nothing else may set it |
| `connector_specs` | 11 | A list of `ConnectorSpec`, one per connector: the hardware description |

`ConnectorSpec` is a plain embedded object (not its own collection): `connector_id` (≥ 1),
`connector_type` (e.g. `"CCS2"`), `power_type` (`PowerType.AC` or `.DC`) and `max_power_kw` (a
number, or `null` meaning *unknown* — never zero, never guessed). **Why it exists:** OCPP 1.6 has no
message that reports a plug's type or rating, so the only way the system can know them is for
someone to describe the hardware. For the demo that someone is the seed script, copying PlugShare's
outlet data. For a real charger it would be whoever installs it. Every real charger today, CP001
included, has no specs, and its connectors show as "Connector 1".

### 10.3 Where a charger appears on the map

`ChargePoint.map_location()` applies one rule, in order:

1. It has a `site_id` → the site's coordinates (the site is authoritative even if the charger has its
   own).
2. Else it has its own `latitude` and `longitude` → there, as a one-charger "standalone" site. The API
   lists it with id `standalone:<identity>`.
3. Else → **not on the map at all.** It still works over OCPP; it is just not placed yet.

Operators place things with `operate.py create-site "<name>" <type> <lat> <lon>` and
`operate.py set-charge-point-location <identity> --site-id <id>` (or `--latitude/--longitude`).
These are the only writes to `Site` outside the import and seed scripts, and they sit behind the
admin token, not on the public API.

### 10.4 Two meanings of "how many chargers"

- **Operated and simulated sites:** counted live, from the `ChargePoint` documents with that
  `site_id`. Never stored, so it can never drift.
- **Reference sites:** there is nothing to count — the system has no chargers there — so PlugShare's
  number is stored at import time (`external_charge_point_count`) and shown as-is. It is only as
  current as the last import.

The API returns both as `charge_point_count` and the site's `source` tells them apart.

### 10.5 Why there are no migrations

MongoDB has no schema to migrate. A new field on a document class simply reads as its default on
old documents, and new collections appear on first write; `init_db()` creates the declared indexes
on every start. Adding the map fields, and later `simulated` and `connector_specs`, required no data
change at all.

### 10.6 Every collection, and who uses it

| Collection | What is in it | Written by | Read by the public API? |
|---|---|---|---|
| `sites` | Places (10.1) | Import, seed, `operate.py create-site` | Yes |
| `charge_points` | Chargers: identity, key hash, boot details, location, demo flag, specs | Seed scripts, `main.py` (boot) | Yes (identity, site, specs) |
| `connector_statuses` | Last status per connector, plus the operator's on/off intent | `main.py` (StatusNotification, ChangeAvailability) | Yes — this is the pin colour |
| `transactions` | Every session, with its meter readings | `main.py` (Start/Meter/StopTransaction) | Only the open one per charger |
| `id_tags` | Driver cards and their status/group/expiry | Seed scripts, operators | No |
| `fault_events` | Every fault, opened and cleared | `main.py` | No (faults reach the map as `Faulted` status) |
| `reservations` | Reservations and how each ended | `main.py` | No |
| `configuration_entries`, `local_list_state`, `firmware_updates`, `diagnostics_requests`, `charging_profiles` | Operator-side bookkeeping | `main.py` | No |
| `counters` | Atomic counters for transaction and reservation ids | `main.py` | No |

### 10.7 What is in the database now

136 sites: 134 PlugShare sites (all `simulated` while the demo is seeded) and 2 real operator
sites — "Petrol Podgorica — Bulevar", where the real test charger CP001 lives, and "Hotel Budva",
which has no charger yet (so it shows grey `unavailable`). 196 chargers: CP001 plus 195 demo
chargers. 257 idTags: `TAG001` plus 256 demo cards.

## 11. The Redis event seam (task 08 §E)

**Why it exists.** The public API must show changes the moment they happen, but it must not be
part of `main.py` (§12.1 explains why). Something has to carry "this just changed" from one process
to the other.

**Why Redis pub/sub, and not the alternatives.**

- *`main.py` calling the API over HTTP* would make the safety-critical process depend on the public
  one, and it would have to know every API replica's address.
- *MongoDB change streams* would require turning MongoDB into a replica set, and would couple the API
  to raw database changes instead of a clean list of events.
- *Redis pub/sub*: `main.py` announces events on one channel without knowing or caring who listens;
  any number of API processes subscribe. Nothing is stored in Redis.

**How `main.py` publishes.** A Redis client is created once in `main()`. Each publish is
fire-and-forget: it has a 0.5 s timeout and any error is logged and ignored, so a Redis outage can
never slow down or break charger handling. (The test suite starts `main.py`'s server without a
publisher, so publishing is a no-op there unless a test connects one.)

**The channel** is `charging-events`. **The envelope**, the same for every event:

| Field | What it is |
|---|---|
| `type` | Which event (below) |
| `charge_point_identity` | Which charger — the API routes events by this |
| `connector_id` | Which connector |
| `at` | When the Central System published it |
| `data` | Type-specific fields |

**The events, where they are published, and what the website does with them:**

| `type` | `data` | Published when | Website |
|---|---|---|---|
| `connector_status` | `status`, `error_code` | A StatusNotification has been stored | Updates the connector's status |
| `transaction_started` | `transaction_id`, `id_tag`, `meter_start` | A StartTransaction created a transaction | Shows "Session #N" |
| `transaction_stopped` | `transaction_id`, `meter_stop` | A StopTransaction closed one (not for duplicates) | Removes the session line |
| `meter_value` | `transaction_id`, `meter_value` (latest reading) | A MeterValues for an open transaction | Shows "· N Wh" |
| `fault_opened` | `error_code`, `info` | A connector entered Faulted | Nothing (the status event already shows it) |
| `fault_cleared` | — | A connector left Faulted | Nothing |
| `reservation_created` | `reservation_id`, `id_tag`, `expiry_date` | A ReserveNow was accepted | Nothing |
| `reservation_released` | `reservation_id`, `reason` | A reservation was consumed, cancelled or expired | Nothing |

> **Privacy issue:** `transaction_started` and `reservation_created` carry the driver's `id_tag`,
> and the API forwards events to browsers unchanged. See §4, item 1.

To watch the channel yourself: `redis-cli -a <password> SUBSCRIBE charging-events` (the password is
in `REDIS_URL` in `.env`).

## 12. The public API (task 08)

**Where:** `api/` in the `chargers` repo. **Run:** `uvicorn api.app:app --host 0.0.0.0 --port 8000`.
Interactive docs at `http://localhost:8000/docs`.

### 12.1 Why it is a separate process

`main.py` talks to physical machines; one bug there can stop real chargers. The public API talks to
anyone on the internet — arbitrary traffic, malformed requests, sockets that hang. The two must fail
independently and must not share memory. So the API:

- never imports `main.py` and cannot see its live connections;
- shares only `models.py` (the document definitions) and MongoDB, which it **only reads**;
- hears about changes only through Redis;
- has **no endpoint that can change anything**: every route is `GET` or a WebSocket (a test checks
  the route table). Commanding a charger stays behind `main.py`'s admin token.

It can be restarted, redeployed or run as several copies without `main.py` noticing.

### 12.2 Files

| File | What it does |
|---|---|
| `app.py` | Creates the app; startup connects MongoDB (exits if unreachable) and Redis (if unreachable: warns, REST still works, WebSockets are refused); adds CORS |
| `config.py` | `redis_url()`, `cors_origins()`, `events_channel()` from environment variables |
| `schemas.py` | The public response shapes. Separate from the database documents on purpose, so an internal field can never leak by accident |
| `queries.py` | All the reading: site list, site detail, charger detail, status aggregation, connector merging |
| `routes_rest.py` | The three REST endpoints |
| `routes_ws.py` | The two WebSocket endpoints |
| `live.py` | The Redis subscriber loop and the registry of open sockets |

### 12.3 `GET /api/v1/sites` — every pin

One entry per site, plus one per standalone charger. Just enough to draw a coloured pin:

```json
{"id": "6abae4a05052262558f12be4", "name": "Rest stop Pelev Brijeg", "site_type": "other",
 "source": "simulated", "latitude": 42.59480496651259, "longitude": 19.394833428493694,
 "connector_types": ["CCS2"], "charge_point_count": 4, "aggregate_status": "unavailable"}
```

**`aggregate_status` — one colour for a whole site.** First match wins:

| # | Rule | Result |
|---|---|---|
| 0 | `source` is `external_reference` | `unknown` (checked first: there is nothing live to look at) |
| 1 | Any connector `Faulted` (including connector 0, the charger as a whole) | `faulted` |
| 2 | Any connector `Available` | `available` |
| 3 | Any connector `Reserved` | `reserved` |
| 4 | Any connector `Preparing`, `Charging`, `SuspendedEV`, `SuspendedEVSE` or `Finishing` | `occupied` |
| 5 | Otherwise (all `Unavailable`, or nothing ever reported) | `unavailable` |

The order encodes what a driver needs: a broken connector is worth flagging first; after that, "is
there anything free for me?" beats "it is busy". So **a site is amber only when every connector is
busy**, and a site with one broken connector is red even if others are free.

### 12.4 `GET /api/v1/sites/{id}` — one site, in full

Everything above plus `address` and every charger with every connector:

```json
{"id": "6abae4a05052262558f12c24", "name": "EKO Tivat", "site_type": "other", "source": "simulated",
 "latitude": 42.43305728041962, "longitude": 18.69885628505272, "address": "11 Mažina, Tivat, Montenegro",
 "connector_types": ["CCS2", "CHAdeMO", "Type 2"],
 "charge_points": [{"identity": "PS-1567258", "connectors": [
   {"connector_id": 1, "status": "Unavailable", "error_code": "NoError", "connector_type": "CHAdeMO", "power_type": "DC", "max_power_kw": 50.0},
   {"connector_id": 2, "status": "Unavailable", "error_code": "NoError", "connector_type": "CCS2",    "power_type": "DC", "max_power_kw": 50.0},
   {"connector_id": 3, "status": "Unavailable", "error_code": "NoError", "connector_type": "Type 2",  "power_type": "AC", "max_power_kw": 22.0}]}]}
```

Chargers are sorted by identity (the website numbers them "Charger 1…N" in this order). How each
connector entry is built — the status row merged with the hardware spec, matched by connector
number:

| The charger has… | The connector is listed as |
|---|---|
| a status row and a spec (demo chargers) | its status, plus plug, AC/DC and kW |
| a status row, no spec (every real charger, e.g. CP001) | its status, hardware fields `null` — the panel shows "Connector 1" |
| a spec, no status row yet (never reported) | `Unavailable` / `NoError`, plus its hardware |

Connector 0 is never listed. A reference site always has `charge_points: []`. Unknown id: 404; an
id that is not a valid ObjectId: 422.

### 12.5 `GET /api/v1/charge-points/{identity}` — one charger

Its connectors (as above), its site, and the session currently open on it, if any:
`current_transaction: {transaction_id, meter_start, latest_meter_value}` (the latest energy reading
in Wh), or `null`.

### 12.6 The WebSockets — live updates

`WS /api/v1/ws/sites/{id}` (everything happening at a site) and `WS /api/v1/ws/charge-points/{identity}`
(one charger). What happens when a browser connects:

1. Redis subscriber not running → accepted and immediately closed with code **1013** ("try again
   later"), so the browser is not left on a silent socket.
2. Unknown site or charger → closed with **1008**, reason "no such site" / "no such charge point".
3. A reference site → closed with **1008** and a distinct reason ("no operator-managed charger at
   this location") — it exists, but nothing live will ever come.
4. Otherwise: accepted; the **first message is a snapshot**, the same shape as the REST detail
   response, so the browser can draw at once; then the socket is registered with the *set of
   charger identities* at that site, computed once, now.
5. From then on, every Redis event whose `charge_point_identity` is in that set is sent to this
   socket, unchanged and in order.
6. Browser closes the tab or panel → the socket is removed. Normal, not an error.

The subscriber is a background task that reads `charging-events` forever, and retries every 2 s if
Redis drops. One channel with filtering in the API is simpler than a channel per charger and is
nowhere near its limits at this scale.

### 12.7 Errors, security, CORS

- 404 for unknown ids, 422 for malformed ones, 500 if MongoDB fails mid-request — never an empty
  list pretending there is no data.
- No authentication, by design: it is public and read-only.
- CORS (`CORS_ORIGINS`) lists which websites may call it from a browser. Empty means **none**, not
  "all". Here it is `http://localhost:3000`.
- Rate limiting is not built (§4).

## 13. The PlugShare import (task 09)

**Where:** `import_plugshare_sites.py`, reading `montenegro_only.json` (a PlugShare export of
Montenegro, committed to the repo). **Run:** `python import_plugshare_sites.py [--dry-run]`.

**Why it exists.** A new operator runs very few chargers, so a map of only its own sites would be
nearly empty. The import adds every known charging location in the country as a reference site.

### 13.1 What is in the file

135 locations, each with nested **stations** (chargers) and **outlets** (plugs): 134 real locations
plus 1 marked "coming soon"; 195 stations; 255 outlets. PlugShare's own numeric plug codes, power
type, rating (on 75 outlets), and per-outlet status (22 "out of order", 8 "unknown", the rest
empty).

### 13.2 What is kept, and what is thrown away

| PlugShare field | Kept as | Why |
|---|---|---|
| `id` | `external_id` | The only reliable key for "already imported" (two names repeat: "Porto Montenegro", "Dukley Hotels & Resort") |
| `name`, `latitude`, `longitude`, `address` | Same names | Real-world facts |
| `connector_types` | `connector_types` | Plug names, already human-readable |
| `url` | `external_url` | Attribution |
| number of `stations` | `external_charge_point_count` | Informational, frozen |
| `coming_soon` | Not stored — such entries are skipped | Nothing exists there yet |
| `under_repair` | Not stored | A stale snapshot; reference sites are "unknown" anyway |
| `access`, `score`, `icon`, `icon_type`, `is_fast_charger`, `station_count`, `available_station_count`, `in_use_station_count` | Not stored | Constant, presentation-only, derivable, or someone else's live status |
| `stations[]`, `outlets[]` detail | Not stored by the import | No chargers are created for a reference site. (The demo seed reads this detail directly from the file, §15.2) |

Every imported site gets `site_type = other`. The export has no reliable venue type, and guessing
one from the name (a "Renault Podgorica" is a car dealer) would be wrong often and silently.

### 13.3 How it runs

For each entry: skip if coming soon; skip (and report) if name or coordinates are missing; look up
a site with the same `external_id`: **found** → update name, coordinates, address, plug types, URL
and station count; **not found** → create it as `external_reference` / `other`. The update never
touches `site_type` (an operator may have corrected it) or `source` (so a site the demo converted
to `simulated` stays simulated). It reports created / updated / skipped. The loop lives in
`import_entries(entries, dry_run)`, which the tests call directly; `main()` is the command-line
wrapper around it.

Real result: first run 134 created, 1 skipped (coming soon); every later run 0 created, 134 updated.

## 14. The website (tasks 10 and 11)

**Where:** the separate repository `../charger-fe` (branch `live-map`). **Run:** `npm run dev`, then
open `http://localhost:3000`. It talks only to the public API.

### 14.1 Stack, and why

| Choice | Why |
|---|---|
| **Next.js 16** (App Router), **React 19**, **TypeScript** | The first view is rendered on the server (no blank map while data loads); the map itself runs in the browser. Types mirror the API contract, so a changed field is a compile error, not a runtime surprise. Read `charger-fe/AGENTS.md` first: this Next.js version differs from older ones |
| **Tailwind CSS 4** | Styling in the markup; no separate stylesheets |
| **MapTiler SDK** (built on MapLibre GL) | A WebGL map: pins are *data* styled by rules, so recolouring 136 pins is one data update, not 136 DOM changes; clustering is built in |

### 14.2 Files

| File | What it does |
|---|---|
| `app/page.tsx` | Server-side: fetches `GET /api/v1/sites` on every request (no caching — the data must be current) and hands it to the map |
| `app/layout.tsx` | Page shell; title "Montenegro Charging Map" |
| `components/MapView.tsx` | The map, the pin data, clustering, clicks, and the 10-second pin refresh |
| `components/StationPanel.tsx` | The panel for one clicked site |
| `components/SearchBox.tsx` | The "Search a place in Montenegro…" box |
| `lib/api.ts` | `listSites()`, `getSite(id)`, `getChargePoint(identity)` |
| `lib/types.ts` | TypeScript copies of the API's shapes and the event envelope |
| `lib/useStationLiveStatus.ts` | The live WebSocket for the open panel |
| `lib/statusColors.ts` | The one place colours are defined |
| `e2e/smoke.spec.ts`, `e2e/demo-fleet.spec.ts` | Browser tests (Playwright) |

### 14.3 The map (`MapView.tsx`)

- **Camera.** Starts centred on Montenegro (`[19.3, 42.7]`, zoom 8); zoom limited to 7–19; panning
  limited to Montenegro's box (18.4–20.4 °E, 41.85–43.6 °N), so a user cannot get lost. Map
  controls sit bottom-right, clear of the panel.
- **One data source, `sites`.** Each site becomes one GeoJSON point whose properties carry its id,
  name, source, plug types, charger count and `aggregate_status`.
- **Clustering.** Built into MapLibre: points within 50 px of each other merge into a numbered
  circle, recomputed on every zoom; above zoom 14 every site shows on its own. Clusters are sized by
  count (16/20/26 px). Each cluster also *counts* its faulted and available sites, which decides its
  colour: **red** if any site inside is broken, else **green** if any has a free connector, else
  **grey**. So a cluster answers "is there something free (or broken) in here?" before you zoom in.
- **Pins.** 9 px circles coloured by `aggregate_status` (§14.6). `unknown` pins are drawn at 15%
  opacity — deliberately faint, because the system knows nothing live about them.
- **Clicks.** A cluster: the map asks the cluster at what zoom it would split, and flies there in
  0.5 s. A pin: opens the panel for that site.
- **Pin refresh (task 11).** After the map loads, every 10 s — only while the tab is visible — it
  calls `listSites()` and replaces the source's data. A failed request keeps the old pins silently;
  the timer is cleared when the map is removed. This is what makes pins change colour without a
  reload.
- **Test hook.** In development only, the map object is exposed as `window.__chargerMap`, so the
  browser tests (and the demo recorder) can find pins by data instead of guessing pixels.

### 14.4 The station panel (`StationPanel.tsx`)

Opens on the right. It loads `GET /api/v1/sites/{id}` straight away (showing a grey placeholder
for the moment it takes), and — unless it is a reference site — opens the live WebSocket at the same
time.

**Top:** name; address; **"Up to N kW"** (the highest rating of any connector at the site, shown
only if at least one rating is known); "Connectors: CCS2, Type 2" (the site's plug list).

**A reference site** shows a faint "unknown" badge and "Imported reference data — this system has no
live connection to this location." It never opens a WebSocket (the API would refuse it anyway).

**Otherwise:** "Live" (or "Connecting…"), then **every charger**, titled "Charger 1", "Charger 2", …
in the API's order, with its identity in small grey text. Each charger lists its connectors:

| The connector has… | Label |
|---|---|
| plug, AC/DC and kW | `CCS2 · 200 kW DC` |
| plug and AC/DC, no kW | `Type 2 · AC` |
| no hardware spec (real chargers today) | `Connector 1` |

Ratings print without decimals when whole (`22 kW`), otherwise to one decimal (15.36 → `15.4 kW`).
Next to each: the **status**, coloured like the pins (Available green; Preparing, Charging,
Suspended and Finishing amber; Reserved violet; Faulted red; anything else grey). Under it, the
error code in red if there is one, and during a session **"Session #N · X Wh"**.

**At the bottom**, for simulated sites: "Demo data — simulated chargers."

Earlier (task 10) the panel collapsed multi-charger sites into an accordion; task 11 removed it, so
every charger's live status is visible at once (the largest site has five).

### 14.5 Live updates (`useStationLiveStatus.ts`)

Given a site id, it opens `WS {NEXT_PUBLIC_WS_BASE_URL}/api/v1/ws/sites/{id}` and keeps a live
state for each connector, keyed by **charger identity + connector number** (`PS-2420955:1`). The
panel shows the live state where it has one, and the REST data otherwise.

| Frame | What the hook does |
|---|---|
| The first frame (the snapshot, which has no `type`) | Ignored — the REST call already drew the panel |
| `connector_status` | Sets that connector's status and error code |
| `transaction_started` | Remembers the session number |
| `meter_value` | Takes the energy reading from the MeterValue's `sampled_value` (preferring the energy register); also learns the session number if the panel opened mid-session |
| `transaction_stopped` | Clears the session and the reading |
| anything else | Ignored |

Switching to another site, or closing the panel, closes the socket. Two bugs were fixed here during
task 11: live state used to be keyed by connector number alone, so at a multi-charger site every
charger's "connector 1" overwrote the others; and energy readings were read from the wrong place,
so "· X Wh" never appeared.

### 14.6 Colours (`statusColors.ts`)

| Status | Colour | Hex |
|---|---|---|
| `available` | green | `#22c55e` |
| `occupied` | amber | `#f59e0b` |
| `reserved` | violet | `#8b5cf6` |
| `faulted` | red | `#ef4444` |
| `unavailable` | grey | `#6b7280` |
| `unknown` | light grey, 15% opacity | `#d1d5db` |

`statusColorExpression()` turns this table into the map's styling rule; `connectorStatusColor()`
maps a connector's OCPP status onto the same palette for the panel.

### 14.7 Search (`SearchBox.tsx`)

Sends the typed text to OpenStreetMap's Nominatim geocoder, limited to Montenegro, and flies the map
to the first result at zoom 14. No result: "Nothing found.", and the map stays put. (Nominatim's
limits: §4.)

### 14.8 Configuration

`.env.local` in `charger-fe` (`.env.example` lists them). Browser-visible variables must start with
`NEXT_PUBLIC_`:

| Variable | Value here | What for |
|---|---|---|
| `NEXT_PUBLIC_MAPTILER_API_KEY` | (your key) | Map tiles and styles. Public by nature — restrict it by domain in MapTiler |
| `NEXT_PUBLIC_API_BASE_URL` | `http://localhost:8000` | REST |
| `NEXT_PUBLIC_WS_BASE_URL` | `ws://localhost:8000` | WebSockets |

## 15. The demo fleet (task 11)

### 15.1 What it is, and what it is not

A way to make the whole platform visibly work, end to end, without owning chargers: 195 simulated
chargers, one per PlugShare station, each connecting to `main.py` exactly as a real charger would
(same authentication, same OCPP messages), each running realistic sessions.

It is **not** a shortcut around the real path. Nothing writes statuses into the database directly:
every colour on the map got there as a real OCPP message through `main.py`, Redis and the API. The
Central System cannot tell a demo charger from a real one — apart from the `simulated` flag that
marks it for removal.

### 15.2 From PlugShare to chargers

| PlugShare | Becomes | Details |
|---|---|---|
| location | the existing `Site`, `source` changed to `simulated` | Nothing else on the site changes, so removal can change it back |
| station | a `ChargePoint`, identity `PS-<station id>` (e.g. `PS-2946795`), `simulated = true` | Registered `Accepted`; its key goes into the manifest |
| outlet *i* | connector *i + 1* | OCPP numbers connectors from 1 |
| outlet plug code | `connector_type` | Translated (below); an unknown code stops the seed — never guessed |
| outlet `power_type` | `power_type` (AC/DC) | |
| outlet `kilowatts` | `max_power_kw` | As published (15.36 stays 15.36); missing stays `null` |
| outlet `status == "OUTOFORDER"` | the connector reports **Faulted** for the whole run | Kept in the manifest only, not in the database |
| — | one driver card per connector, `DEMO-<station>-<connector>`, group `DEMO-FLEET` | Plus `DEMO-REMOTE` for manual remote starts |

PlugShare's plug codes, and the checks that fixed them: translating every outlet at a location
reproduces that location's own plug-name list, for all 134 locations.

| Code | Plug | Outlets | Current |
|---|---|---|---|
| 7 | Type 2 | 187 | AC |
| 20 | CCS2 | 40 | DC |
| 3 | CHAdeMO | 11 | DC |
| 10 | Wall (Euro) | 11 | AC |
| 14 | Three Phase | 4 | AC |
| 15 | Caravan Mains Socket | 1 | AC |
| 2 | J-1772 | 1 | AC |

Result: 134 sites, 195 chargers (88 sites with one charger; 46 with two to five), 255 connectors (75
with a rating, 22 out of order — making exactly 10 sites entirely broken), 256 cards.

### 15.3 `seed_demo_fleet.py` — creating it

`python seed_demo_fleet.py [--dry-run] [--file …] [--manifest demo_fleet_manifest.json]`

1. **Checks everything before writing anything.** It aborts, writing nothing, if a location has no
   `Site` (run the import first), if a plug code is unknown, if a card id would exceed OCPP's 20
   characters, or if a `PS-…` identity already belongs to a **real** charger (it will never take one
   over).
2. **Sites:** real operator sites are skipped and reported; the rest become `simulated`.
3. **Chargers:** new ones are registered with a fresh key. Existing demo chargers are updated in place
   and **keep their key** if the manifest still holds a matching one — so a re-run rotates nothing.
   If the manifest was lost, their keys are rotated (only a hash is stored, so a lost key cannot be
   recovered).
4. **Cards:** created if missing, left alone if present.
5. **Manifest** (`demo_fleet_manifest.json`, gitignored) written atomically — to a temporary file,
   then renamed into place, so a crash cannot leave a half-written file. It is the **only copy of
   the chargers' keys**, like `charge_point_credentials.json` is for `seed.py`:

```json
{"generated_at": "…", "source_file": "montenegro_only.json", "chargers": [
  {"identity": "PS-2946795", "authorization_key": "<40 hex characters>", "site_name": "kolasin 1600",
   "connectors": [{"connector_id": 1, "connector_type": "Type 2", "power_type": "AC", "max_power_kw": null,
                   "out_of_order": false, "id_tag": "DEMO-2946795-1"}]}]}
```

6. **Report:** sites converted / skipped, chargers created / updated / keys rotated, connectors (with
   kW, out of order), cards created. `--dry-run` prints the same report and writes nothing.

### 15.4 The simulator changes (`simulate_charge_point.py`)

The fleet reuses the project's simulator class, `SimulatedChargePoint`, one instance per charger.
It needed:

| Change | Why |
|---|---|
| Importable (the script's run line is behind `if __name__ == "__main__":`) | The fleet and the tests import the class |
| `number_of_connectors` | Chargers with 2–4 connectors; also reported in the `NumberOfConnectors` setting |
| `send_boot_statuses(faulted=…)` | After booting, report every connector: Available, or Faulted with "Out of order (PlugShare report)". Skips a connector already reported on this connection, so a "switched off" setting the Central System re-sends after boot is not overwritten |
| `send_status(…, info=…)` | To send that text |
| `start_local_session(connector, card)` | A driver at the charger: Authorize → Preparing → StartTransaction → Charging. Refused card: nothing happens. Card refused at start: the session is ended at once (`DeAuthorized`) |
| `busy_connectors()` | Connectors with a session, a remote start on the way, or a local start in progress: nothing may start a second session on them |
| Remote start chooses a connector | Lowest free one; refuses a busy one; the choice is held until the start finishes |
| Energy register used everywhere | Sessions start and stop at the connector's current meter reading, so energy is never negative |
| `status_since` | When each connector entered its current status (for the unplug timer) |
| `send_meter_values` (renamed from private) | The fleet calls it |

The hand-run flows from tasks 04 and 06 (`python simulate_charge_point.py …`) behave as before.

### 15.5 `run_demo_fleet.py` — running it

`python run_demo_fleet.py` (Ctrl+C to stop). One process, one event loop, one WebSocket per charger.
It never touches MongoDB: like a real charger, it knows only its configuration (the manifest) and
its own "flash storage" (the state file).

**Starting.** Chargers connect at most 10 per second. Why: `main.py` checks each charger's key with
scrypt (a deliberately slow hash, ~30 ms) inside its event loop, so 195 at once would freeze it for
~6 s. The same limit applies to reconnections, so a Central System restart does not cause a
stampede. All 195 are up in about 20 s.

**One charger's life:**

1. **Connect**, with its identity and key. On failure: wait 2 s, then 4, 8, 16, up to 30 s, with ±20%
   randomness so chargers do not retry in lockstep. It prints the first failure, then stays quiet
   until it recovers ("reconnected").
2. **Boot.** If not accepted, wait the interval the Central System gave and boot again.
3. **Close leftovers.** Any session the state file says was still open — the process died, or the
   connection dropped mid-session — is closed with reason `PowerLoss`. This matters: a session left
   open keeps its card "busy", and every later start with it would be refused (`ConcurrentTx`).
4. **Report every connector** (Available / Faulted).
5. **Run**, until the connection drops or the fleet stops:
   - a **heartbeat** every 30 s;
   - a **session loop per working connector**: wait, check the connector is Available and not busy,
     start a session with its own card, charge for the chosen time, stop it (`Local`). Faulted
     connectors never get one;
   - **housekeeping** every second: advance meters, release Finishing connectors, save state.
6. **Connection lost** (including an operator reset): stop everything, save what was open, go back
   to 1.

**Session timing** (all adjustable, §18):

| What | Default |
|---|---|
| Session length | random 3–8 minutes |
| Gap between sessions on a connector | random 5–15 minutes |
| Connectors already charging at start | 35% (they start within the first 30 s) |
| Meter reading | every 15 s |
| Driver unplugs after Finishing | 5–15 s later (applies to remote stops too) |
| A refused start retries after | 60 s |

With these defaults a working connector is in use about a third of the time. Since a site is amber
only when *all* its connectors are busy, the map shows mostly green, a handful of amber (mostly
single-connector sites) and the 10 red broken sites.

**Energy.** Every meter tick adds *power × time*: the connector's rating if known, otherwise 7.4 kW
(AC) or 50 kW (DC), times a random 60–100% chosen once per session (cars rarely draw the full
rating). Only while `Charging`. Time is measured in *simulated* time, so a sped-up run
(`--time-scale`) still delivers realistic energy. This assumed power only shapes the Wh readings;
it is never shown as the connector's rating.

**The state file** (`demo_fleet_state.json`, gitignored) is each charger's flash memory: its energy
registers and any sessions still open. It is written atomically at most once a second (the first
change after a quiet second is written straight away), and stamped `fleet_alive_at` every 10 s while
running. The stamp becomes `null` on a clean exit; the removal script (task 12) is specified to
refuse to run while it is recent. A missing or unreadable file just means an empty state.

**Stopping** (Ctrl+C, SIGTERM, or `--duration-seconds`), within 20 s:

1. Background work stops at its next pause — waiting rather than cancelling, so a StartTransaction
   already sent gets its answer recorded instead of leaving a session open that nobody knows about.
2. Every open session is closed (reason `Other`).
3. Every connector that is not Faulted reports **Unavailable** (a Preparing one reports Available
   first, since OCPP has no Preparing → Unavailable). Faulted ones stay Faulted: an out-of-order
   charger is still out of order when the demo stops.
4. Connections are closed and the state file is written with `fleet_alive_at: null`.

The map then shows the honest picture: 124 grey sites (switched off) and 10 red (broken). A **second
Ctrl+C** exits immediately; the next start closes what it left open, as in step 3 of a charger's
life.

**Its console:** one line per charger on its first connection failure and on recovery, and every
30 s a summary: `connected 195/195 · charging 62 · available 168 · faulted 22 · unavailable 0 · sessions 195`.

**Measured behaviour.** All 195 connected by the first summary (30 s). 134 sessions completed in the
first 10 minutes, none with negative energy. After restarting `main.py`, all 195 reconnected by
themselves in 23.7 s, the sessions the restart interrupted being closed as `PowerLoss`. After
`kill -9`, the next start closed all 90 interrupted sessions as `PowerLoss` and sessions resumed on
the same cards. Ctrl+C exits in about a second.

**Known gap:** if the process is killed within about a second of a session starting, before the
state file is written, that session's number is lost, and its connector refuses sessions
(`ConcurrentTx`) until the demo data is removed. It did not happen in testing, but it can.

### 15.6 Removing it (task 12, not built)

Specified in [`instructions/12-remove-demo-fleet.md`](instructions/12-remove-demo-fleet.md): delete
every `simulated` charger and everything recorded against its identity, the `DEMO-FLEET` cards and
the two local files, and turn every `simulated` site back into `external_reference`. It must never
touch CP001, operator sites, other cards, or the transaction counter.

## 16. Demo tooling: stories and videos

- **[`demo/USER_STORIES.md`](demo/USER_STORIES.md)** — twelve user stories (driver, operator,
  presenter), every path through each that the system supports, which video shows it, how to show
  the rest live, what cannot be demoed yet, and things to know before presenting.
- **`demo/videos/`** — six narrated, captioned recordings at 1920×1080 (the files are gitignored):

| Video | Length | Stories |
|---|---|---|
| `01-map-at-a-glance.mp4` | 1:50 | Map, colours, clusters, search |
| `02-plugs-and-power.mp4` | 3:18 | Plugs, AC/DC, ratings, unknown power |
| `03-remote-session-live.mp4` | 2:12 | Remote start, live energy, pin colour, remote stop |
| `04-busy-and-broken-sites.mp4` | 2:06 | Five chargers at one site; a broken site |
| `05-operator-controls.mp4` | 2:09 | Switch off/on, reserve/cancel, reboot |
| `06-restarts-and-honest-stop.mp4` | 2:19 | Central System restart, honest stop, restart |

- **`demo/record_demo.mjs`** — how they are made, and how to remake them
  (`node demo/record_demo.mjs [video numbers]`). It drives the real website in headless Chrome
  (GPU-rendered), starts its own fleet so it can show the fleet's console, and draws captions, a
  pointer, highlight boxes and an "operator terminal" on top of the page. Every operator step runs
  the real `operate.py` command and shows its real output; nothing in the app is faked. It captures
  Chrome's screen frames and encodes them with ffmpeg (H.264). A take that goes wrong — for example,
  the fleet happens to start a session on the chosen connector just before the operator does — is
  thrown away and recorded again. Video 6 restarts `main.py`.

---

# Part V — Operating it

## 17. Running everything

This section is about running it for development. **On a server, use Docker and the Makefile
instead: see [`DEPLOY.md`](DEPLOY.md)** (`make setup`, `make deploy`).

**What must be running, and on which port:**

| Process | Port | Start with | Needs |
|---|---|---|---|
| MongoDB | 27017 | (Docker container `chargers-fe-test-mongo` on this machine) | — |
| Redis | 6379 | (Docker container `popravime-redis-1`; password-protected) | — |
| Central System, `main.py` | 9000 (chargers + admin API) | `python main.py` | MongoDB, Redis |
| Public API | 8000 | `uvicorn api.app:app --host 0.0.0.0 --port 8000` | MongoDB, Redis (WebSockets only) |
| Website | 3000 | `cd ../charger-fe && npm run dev` | The API |
| Demo fleet (optional) | — | `python run_demo_fleet.py` | `main.py`, a seeded demo |

**First-time setup, in order** (from `chargers/`):

```
python import_plugshare_sites.py      # once: the 134 PlugShare sites
python seed_demo_fleet.py --dry-run   # check: 134 / 0 / 195 / 255 (75, 22) / 256
python seed_demo_fleet.py             # once; safe to re-run
```

**For a demo:** start MongoDB and Redis, then `main.py`, the API, the website, and last the fleet.
Wait for the fleet's first `connected 195/195` line (about 30 s) before showing the map. For
operator commands, in another terminal:

```
export ADMIN_TOKEN=$(grep ^ADMIN_TOKEN= .env | cut -d= -f2-)
python operate.py remote-start PS-2946795 DEMO-REMOTE
```

**Two things specific to this machine.** There are two Python environments: `.venv/` (ocpp 2.0.0,
matching `requirements.txt`) and `venv/` (ocpp 2.1.0), which the servers have been run from; the
whole test suite passes in both. And `.env` is loaded by `models.py` on import, so a `main.py` or API
started before that change was made will not have `ADMIN_TOKEN` or the Redis password — restart
them.

## 18. Configuration reference

**`chargers/.env`** (read by `main.py`, the API and the scripts; real environment variables win):

| Variable | Used by | Meaning |
|---|---|---|
| `MONGODB_URL`, `MONGODB_DB` | everything | Database (`mongodb://localhost:27017`, `ocpp_poc`) |
| `REDIS_URL` | `main.py`, API | Redis, including its password |
| `CORS_ORIGINS` | API | Websites allowed to call the API from a browser (comma-separated). Empty = none |
| `ADMIN_TOKEN` | `main.py`, `operate.py` | The shared secret for the admin API. Unset = admin API disabled |

`operate.py` also reads `ADMIN_URL` (default `http://localhost:9000`) — but not `.env`: export
`ADMIN_TOKEN` in the shell.

**`run_demo_fleet.py` options:**

| Option | Default | Meaning |
|---|---|---|
| `--url` | `ws://localhost:9000` | Central System |
| `--manifest` | `demo_fleet_manifest.json` | Chargers and keys, from the seed |
| `--state-file` | `demo_fleet_state.json` | Registers and open sessions |
| `--connect-rate` | 10 | New connections per second |
| `--session-minutes MIN MAX` | 3 8 | Session length |
| `--idle-minutes MIN MAX` | 5 15 | Gap between sessions on one connector |
| `--initial-busy` | 0.35 | Share of connectors already charging at start |
| `--meter-interval` | 15 | Seconds between meter readings |
| `--time-scale` | 1.0 | Multiplies every duration above (and the unplug delay), not the heartbeat. 0.1 = ten times faster |
| `--limit N` | all | Only the first N chargers |
| `--duration-seconds` | — | Run this long, then stop cleanly |
| `--seed` | — | Random seed, for a repeatable run |

**`seed_demo_fleet.py`:** `--file`, `--manifest`, `--dry-run`. **`import_plugshare_sites.py`:**
`--file`, `--dry-run`.

**Website** (`charger-fe/.env.local`): §14.8. **Playwright:** `PLAYWRIGHT_API_BASE_URL` (default
`http://localhost:8000`).

**Recorder** (`demo/record_demo.mjs`): `DEMO_PACE` (pause multiplier, default 1), `DEMO_FFMPEG`
(ffmpeg with libx264), `DEMO_PYTHON` (default `venv/bin/python`), `DEMO_KEEP_FLEET=1` (leave the
fleet running), `DEMO_NO_GPU=1` (software rendering), `DEMO_APP_URL`, `DEMO_API_URL`.

## 19. Tests

**Backend:** `python -m pytest` from `chargers/` — 316 tests, about 30 s. They use a real MongoDB
(a throwaway database per run, dropped afterwards), a real Central System started in-process on a
free port, and real Redis where needed; they skip cleanly when MongoDB or Redis is missing.

| File | Covers |
|---|---|
| `test_sites.py` | The site model, the location rules, and the operator commands that create sites and place chargers (07) |
| `test_api_rest.py`, `test_api_websocket.py` | The public API: every status precedence case, shapes, 404/422, read-only routes, CORS, WebSocket filtering and close codes, Redis-down behaviour, `main.py`'s event shape (08) |
| `test_plugshare_import.py` | The import (09) |
| `test_demo_fleet_seed.py` | Seed mapping, plug codes, kW, out-of-order, cards, every abort case, re-runs rotate no keys, a re-import keeps sites simulated |
| `test_demo_fleet_api.py` | Simulated sites are live, specs merged, spec-only connectors are Unavailable, real chargers unchanged, the WebSocket accepts simulated sites |
| `test_demo_fleet_run.py` | The fleet against a real Central System at 1/100 speed: every connector reports, faults stay, sessions complete with positive energy, clean shutdown leaves nothing open, a hard kill's leftovers are closed as PowerLoss |
| `test_simulate_charge_point.py` | Multi-connector bookkeeping and remote-start connector choice |
| the rest | The Central System's OCPP flows (see `SYSTEM_OVERVIEW.md` §13) |

**Website:** in `charger-fe`, `npm run lint` and `npm run build`; `npx playwright test` runs the
browser tests against the running dev server and API (`demo-fleet.spec.ts` needs the fleet running).

## 20. Troubleshooting

Every row here happened while building this.

| Symptom | Cause | Fix |
|---|---|---|
| `operate.py` answers "admin API disabled" | `main.py` has no `ADMIN_TOKEN` (started before `.env` loading, or from another directory) | Restart `main.py` from `chargers/`; export `ADMIN_TOKEN` for `operate.py` |
| Panel stays "Connecting…", or never changes | The API has no Redis (WebSockets refused with 1013), or `main.py` cannot publish (wrong Redis password) | Check `REDIS_URL`; restart the API; watch the channel with `redis-cli` (§11) |
| Pins keep their colours after the fleet was killed | The offline gap (§4, item 2): nothing reported the chargers gone | Start the fleet (it closes leftovers and reports every connector), or stop it cleanly next time |
| `remote-start` answers `Rejected` | That connector already has a session (the fleet may have just started one) | Wait for it to finish, or pick another charger |
| A demo connector never starts sessions; the fleet logs "refused (ConcurrentTx)" | A session is open in the database that the fleet does not know about (a crash in the ~1 s window, §15.5) | Remove and re-seed the demo (task 12), or add that transaction to the charger's `open_transactions` in the state file while the fleet is stopped, so the next start closes it |
| Ctrl+C on a fleet started in the background does nothing | Background jobs ignore Ctrl+C's signal | `kill -TERM <pid>` — the fleet treats SIGTERM exactly like Ctrl+C |
| A code change does not show up | Python processes do not reload | Restart `main.py` and/or the API (the website's dev server reloads by itself) |
| Many "connection lost" lines in the fleet | `main.py` restarted | Normal; each charger prints "reconnected" within ~25 s |
| The browser console shows CORS errors | The website's address is not in `CORS_ORIGINS` | Add it and restart the API |
| Port 9000 already in use | An old `main.py` still running | Stop it (`ss -ltnp` shows which process holds the port) |
| Search says "Nothing found." for a real town | Nominatim rate limit or no network | Wait a second and retry |

## 21. Where to change things

| To… | Change |
|---|---|
| Support a new PlugShare plug code | `CONNECTOR_TYPES` in `seed_demo_fleet.py` |
| Change a status colour | `charger-fe/lib/statusColors.ts` (pins, clusters and panel all read it) |
| Change which status wins for a site | `aggregate_status()` in `api/queries.py`, and the cluster colour rule in `MapView.tsx` to match |
| Refresh pins faster or slower | `PIN_REFRESH_MS` in `MapView.tsx` |
| Make demo sessions longer, shorter or busier | `run_demo_fleet.py` options (§18), or the defaults in its `parse_args` |
| Add a real site / place a charger | `operate.py create-site …` / `operate.py set-charge-point-location …` |
| Describe a real charger's plugs | Set its `connector_specs` (no command for it yet; a small `operate.py` subcommand would be the right home) |
| Add a field to the API | `api/schemas.py` and `api/queries.py`, `charger-fe/lib/types.ts` in the same change, and a test |
| Add a live event | `publish_event(…)` in `main.py`, `LiveEventType` in `lib/types.ts`, and the hook's `applyEvent` |
| Stop leaking card numbers | Drop `id_tag` from events in `ConnectionManager.dispatch` (`api/live.py`) |
| Detect offline chargers | Record the last heartbeat in `main.py`'s `on_heartbeat`, and treat stale chargers as offline in `api/queries.py` |
| Show PlugShare attribution | Add `external_url` to the API's site schemas and a link in the panel |

## 22. Glossary

| Term | Meaning |
|---|---|
| **aggregate_status** | One status for a whole site, computed from its connectors (§12.3) |
| **Authorization key** | A charger's password for connecting (20 bytes, 40 hex characters). Not a driver card |
| **CALL / CALLRESULT / CALLERROR** | OCPP's request / reply / error message shapes |
| **Central System** | The backend chargers connect to: `main.py` |
| **Charge point, charger** | One charging machine. Identified by its **identity** |
| **Cluster** | Several nearby pins shown as one numbered circle |
| **Connector** | One plug on a charger, numbered from 1; 0 means the whole charger |
| **ConnectorSpec** | The hardware description of a connector: plug, AC/DC, rated kW |
| **ConcurrentTx** | "This card already has a session running elsewhere" |
| **Demo fleet** | The 195 simulated chargers (§15) |
| **external_reference** | A site we only know about, never connected to (status "unknown") |
| **Fault** | A connector reporting `Faulted`; recorded in `fault_events` |
| **idTag, card** | A driver's credential; may belong to a group (parent idTag) |
| **Manifest** | `demo_fleet_manifest.json`: the demo chargers, their connectors and their keys |
| **OCPP / OCPP-J** | The protocol chargers speak (1.6), and its JSON-over-WebSocket transport |
| **Operator** | Whoever runs the chargers; uses `operate.py` |
| **Register** | A charger's energy meter reading, in Wh; only goes up |
| **Session / transaction** | One charge; the transaction is its billable part, numbered by the Central System |
| **Site** | A place with one or more chargers |
| **Simulated** | Demo data: `ChargePoint.simulated = true`, `Site.source = simulated`, cards in `DEMO-FLEET` |
| **State file** | `demo_fleet_state.json`: the fleet's memory of registers and open sessions |
| **StatusNotification** | The message that reports a connector's status — the source of every colour |
| **Standalone charger** | A charger with its own coordinates and no site; shown as a one-charger site |
