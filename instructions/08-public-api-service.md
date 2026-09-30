# 08 — The public API service (FastAPI, its own isolated process)

`instructions/07-public-map-platform.md` describes the whole public-map platform: the data model
(`Site`, §A), why no migration framework is needed (§B), and how the frontend should be built
(§G). This file is the authoritative, self-contained spec for the one piece in the middle: **a
second backend process, built with FastAPI, that is the only thing the public frontend ever
talks to.** If you are implementing the API service, this file is everything you need; you should
not need to keep `07` open alongside it, beyond knowing what a `Site` document looks like (§A of
that file) and reusing the `Site`/`ChargePoint`/etc. documents this service reads from
`models.py`.

Read `SYSTEM_OVERVIEW.md` first if you have not already. This file assumes you understand what
`main.py` (the Central System, "CS") currently does.

## Why this is a second process, not a feature added to `main.py`

`main.py` is safety-relevant: it is the only thing standing between an operator command and a
physical charger, and its message loop talks to real hardware over OCPP-J. This service is the
opposite kind of thing: public, unauthenticated, internet-facing, read-only, expected to handle
arbitrary browser traffic including malformed requests and WebSocket connections that hang around
or disappear without warning.

**These two must never be the same process, and must never share memory.** Concretely:

- This service **MUST NOT** `import main` for any reason, and **MUST NOT** read `main.py`'s
  `CONNECTED_CHARGE_POINTS` registry or any other of its in-process state. That registry only
  exists inside the CS process's own memory; there is no way to reach it from here, by design.
- This service **MAY** `import models` freely — `models.py` (the Beanie document definitions,
  `init_db()`, `mongodb_url()`, etc.) belongs to neither process specifically; it is the shared
  schema definition both processes read and write MongoDB through.
- The **only two channels** between this service and the CS are:
  1. **MongoDB**, read by both, written mostly by the CS (this service writes nothing except
     what §F below explicitly allows — which, per that section, is nothing).
  2. **Redis pub/sub**, written by the CS, read (subscribed) by this service. See §E.
- This service can be started, stopped, restarted, redeployed, or run as multiple replicas behind
  a load balancer, all **without the CS process needing to know or care**. If you find yourself
  wanting to add a third channel (an HTTP call from the CS into this service, or vice versa),
  stop — that almost certainly means something belongs in a Redis event (§E) instead.

## A. Directory structure and dependencies

This is the one deliberate exception to `instructions/README.md`'s "flat layout, no packages"
convention — because this genuinely is a second, independently-deployable service, not a module
of the first one.

```
api/
  app.py          FastAPI() instance + the lifespan (startup/shutdown) hook. Defines `app` at
                  module level for uvicorn to import — see the note at the end of this section.
  config.py       Plain functions reading environment variables, e.g. redis_url(),
                  cors_origins() — mirror models.py's mongodb_url() style exactly rather than
                  introducing a settings/config framework this project doesn't otherwise use.
  schemas.py      Pydantic response models for the wire contract in §F/§G. Deliberately separate
                  from the Beanie Documents in models.py: this is the public, versioned contract,
                  and it must not silently change shape just because an internal-only field gets
                  added to a Document later.
  routes_rest.py  The REST endpoints, §F.
  routes_ws.py    The WebSocket endpoints, §G.
  live.py         The Redis subscriber loop and the in-process WebSocket connection manager
                  (which open sockets currently care about which charge point identities).
  __init__.py
tests/
  test_api_rest.py        New. REST endpoint tests.
  test_api_websocket.py   New. WebSocket endpoint tests.
```

Test files stay in the project's existing single `tests/` directory, reusing `tests/conftest.py`
fixtures (`db`, `mongodb_available`) rather than duplicating them — sharing pytest fixtures across
test files is a test-authoring convenience and has nothing to do with the process isolation this
file requires at *runtime*. Do not read "isolated" as "must also be tested from a separate
directory."

New dependencies, added to `requirements.txt` pinned to specific versions per this project's
existing convention: `fastapi`, `uvicorn` (its `standard` extra), `redis` (the async
`redis.asyncio` client), and `httpx` (for the test client — see §K).

**Naming note**: the FastAPI instance's module is `api/app.py`, never `api/main.py`. This is
launched by an ASGI server (`uvicorn api.app:app`), not run directly with `python`, so it has no
`raise SystemExit(asyncio.run(main(parse_args())))` entry point the way every other script in
this project does — that convention exists for scripts that ARE the process's entry point; here,
`uvicorn` is. This is a deliberate, documented deviation, not an oversight.

## B. Configuration

Plain environment variables, read through small functions in `api/config.py`, exactly like
`models.mongodb_url()`:

| Variable | Default | Read by |
|---|---|---|
| `MONGODB_URL` | `mongodb://localhost:27017` | Reused as-is from `models.py` — do not invent a second variable for the same thing. |
| `MONGODB_DB` | `ocpp_poc` | Reused as-is from `models.py`. |
| `REDIS_URL` | `redis://localhost:6379/0` | New. `redis_url()` in `api/config.py`. |
| `CORS_ORIGINS` | empty (no origins allowed) | New. `cors_origins()`, comma-separated, parsed to a list. Empty MUST mean "allow nothing", not "allow everything" — an explicit opt-in list is required before this is reachable from a browser at all. |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Only used if you also provide your own `if __name__ == "__main__":` convenience runner; the normal way to run this is `uvicorn api.app:app --host ... --port ...` directly, which reads host/port from its own CLI flags, not these. |

## C. Startup and shutdown

Use FastAPI's `lifespan` context manager (not the older `@app.on_event` hooks, which are
deprecated). On startup:

1. Call `models.init_db()` — the exact same function `main.py` and every CLI script in this
   project already calls. If it raises (MongoDB unreachable), fail the same way `main.py` does:
   print a clear message naming `mongodb_url()` and exit non-zero, rather than starting a service
   that will error on every request.
2. Connect a `redis.asyncio.Redis` client using `redis_url()`. **If this fails, log a clear
   warning and start anyway** — see the next paragraph for why.
3. Start a background `asyncio` task that subscribes to the `charging-events` channel (§E) and
   feeds `api/live.py`'s connection manager. If step 2 failed, skip starting this task, and make
   sure `routes_ws.py` refuses new WebSocket connections with a clear reason (e.g. close code
   1013, "try again later") rather than accepting connections that will never receive anything.

**REST endpoints MUST keep working even if Redis is completely unreachable.** They only ever read
MongoDB directly (§F) and have no Redis dependency at all. Only the WebSocket layer (§G) needs
Redis. Making the whole service's health depend on Redis when most of its surface area doesn't
need it would be a needless, avoidable single point of failure.

On shutdown: close the Redis connection and the MongoDB client cleanly. Cancel the background
subscriber task and let any open WebSocket connections close (they should already handle a server
-initiated disconnect the way §G's contract describes).

## D. Reading the data (REST and WebSocket alike)

Every read this service ever does is a normal Beanie query against `models.py`'s documents —
`Site`, `ChargePoint`, `ConnectorStatus`, `Transaction`, `FaultEvent`, `Reservation` — exactly the
way `main.py` itself queries them. There is nothing special about reading them from a second
process; MongoDB is a shared database, not something owned by the CS.

This service **MUST NOT write to `ChargePoint`, `ConnectorStatus`, `Transaction`,
`Reservation`, `FaultEvent`, `FirmwareUpdate`, or `DiagnosticsRequest`** — those are the CS's
domain, written only in response to real OCPP messages or a real operator action through
`main.py`'s admin API. Writing to `Site` (creating/editing a physical location) is the one
legitimate exception — see §I.

## E. The seam: Redis pub/sub, and the one small change `main.py` needs

**Decision: Redis pub/sub, not an HTTP callback in either direction, and not MongoDB Change
Streams.** In short — an HTTP callback from the CS into this service would create a reverse
dependency (the safety-critical CS process needing to know this service's address and call it,
which gets worse the moment this service runs as more than one replica), and Change Streams would
require turning the project's standalone MongoDB into a replica set for no other benefit and
would couple this service's live-update logic to raw oplog shape instead of a clean event schema
the CS controls. See `07-public-map-platform.md` §D for the full reasoning if you want it; this
section gives you everything you need to actually build against.

### E.1 What `main.py` must additionally do

Add one small async Redis publisher to `main.py`, connected once at its own startup (in `main()`,
alongside its existing `init_db()` call), used as a simple fire-and-forget `PUBLISH` — a
connection failure here MUST NOT be allowed to break or slow down OCPP message handling; wrap
each publish in a bare `try`/`except`, log, and move on, exactly the way `main.py` already treats
`reapply_persisted_availability`'s own failures as non-fatal.

Add exactly one publish call at each of these existing points (none of their current behaviour,
return values, or the acceptance criteria from `instructions/01`–`06a` change — this is strictly
additive):

| Event `type` | `data` fields | Add the publish call in `main.py`, right after: |
|---|---|---|
| `connector_status` | `status`, `error_code` | `on_status`'s `record.apply(...)` call succeeds |
| `transaction_started` | `transaction_id`, `id_tag`, `meter_start` | `on_start` creates the `Transaction` |
| `transaction_stopped` | `transaction_id`, `meter_stop` | `on_stop`'s normal-stop and orphaned-stop branches (not the duplicate-delivery branch — it changed nothing) |
| `meter_value` | `transaction_id`, the latest reading only | `on_meter_values`, only when tied to an open transaction |
| `fault_opened` | `error_code`, `info` | `FaultEvent.open_new(...)` is called |
| `fault_cleared` | (nothing beyond the envelope) | `FaultEvent.close_open(...)` is called |
| `reservation_created` | `reservation_id`, `id_tag`, `expiry_date` | `send_reserve_now`, only when the charger answers `Accepted` |
| `reservation_released` | `reservation_id`, `reason` (`"consumed"`/`"cancelled"`/`"expired"`) | `Reservation.release_for_start`, `release_by_cancel`, and the periodic `sweep_expired` all actually release one |

### E.2 The envelope

One shared channel, `charging-events`. Every message is one JSON object:

```json
{
  "type": "connector_status",
  "charge_point_identity": "CP001",
  "connector_id": 1,
  "at": "2026-09-28T18:14:10.228000+00:00",
  "data": { "status": "Charging", "error_code": "NoError" }
}
```

One shared channel, not one per charge point, is deliberate: at this project's scale (a small
country's charging network — realistically dozens to a few hundred charge points), a single
`SUBSCRIBE` in this service and an in-process filter by `charge_point_identity` (§G) is simpler
than Redis pattern-subscription, and there is no realistic load level at which this project would
outgrow it. If that ever changes, revisit; do not pre-optimize for it now.

## F. REST contract

Base path: `/api/v1/`. Every endpoint is `GET`. **This service MUST NOT expose any endpoint that
changes charger state** — no start/stop, no availability, no configuration, no reservations, no
firmware. All of that remains exclusively reachable through `main.py`'s existing admin API and
`operate.py`. This is the actual point of this being a separate, unauthenticated, public service:
nothing reachable from the public internet through it can ever command a physical charger. Treat
any endpoint proposal that would violate this as a design error, not a shortcut.

`source` can also be `"simulated"` (a demo site whose chargers are simulated, defined in
`11-demo-fleet.md`); everywhere in this section it behaves exactly like `"operator"`.

### `GET /api/v1/sites`

Every `Site` — both `source: "operator"` sites (at least one real `ChargePoint` points at them)
and `source: "external_reference"` sites (informational-only, imported from a third party,
never any real `ChargePoint`) — plus every `ChargePoint` with no `site_id` but its own
coordinates (represented as a synthetic one-charger site, `site_type: "standalone"`,
`source: "operator"`, per `07-public-map-platform.md` §A's location-resolution rule). Enough
detail to paint a coloured pin — not full connector detail, which is a separate, more expensive
call made only once a pin is actually clicked.

```json
[
  {
    "id": "670f...",
    "name": "Petrol Podgorica — Bulevar",
    "site_type": "gas_station",
    "source": "operator",
    "latitude": 42.4304,
    "longitude": 19.2594,
    "connector_types": ["CCS2", "Type 2"],
    "charge_point_count": 3,
    "aggregate_status": "available"
  }
]
```

`aggregate_status` MUST be computed with this exact precedence (first match wins):

0. `source == "external_reference"` (this site has no `ChargePoint` this system has ever
   connected to, by definition — see `07-public-map-platform.md` §A) → `"unknown"`. This MUST be
   checked before anything below; there are no connectors to evaluate a live status from, and
   reporting `"unavailable"` here would falsely claim a live check was made. `"unknown"` is the
   honest answer: this system has no live connection to this location at all.
1. any connector `Faulted` → `"faulted"`
2. any connector `Available` → `"available"`
3. any connector `Reserved` → `"reserved"`
4. any connector actively in a session (`Preparing`, `Charging`, `SuspendedEV`, `SuspendedEVSE`,
   `Finishing`) → `"occupied"`
5. otherwise (`Unavailable`, or no status ever reported) → `"unavailable"`

`connector_types` on the response is `Site.connector_types` verbatim (`null` if never set) —
frontend-relevant for both kinds of site (a driver cares what plug is available whether or not
this system operates the charger), and the only per-site field this endpoint exposes that isn't
itself live status.

`charge_point_count` has two different sources depending on `source`, and this MUST NOT be
blurred into one code path that treats them as interchangeable:

- `source: "operator"` → a live count of real `ChargePoint` documents with this `site_id`. Always
  accurate, by construction — a `ChargePoint` only exists when this system genuinely manages one.
- `source: "external_reference"` → `Site.external_charge_point_count` (see
  `07-public-map-platform.md` §A "Two different meanings of 'how many chargers'" and
  `09-plugshare-import.md`). This is a static number from whenever the site was last imported,
  not a live count — there is nothing to count live for a charger this system has never
  connected to. Do not present it identically to the operator case without the frontend being
  able to tell the two apart if it ever needs to (the `source` field on the same response object
  is exactly how it tells them apart — it does not need a second, separate flag for this).

### `GET /api/v1/sites/{site_id}`

Full detail: every field above, plus every charge point with per-connector status:

```json
{
  "id": "670f...",
  "name": "Petrol Podgorica — Bulevar",
  "site_type": "gas_station",
  "source": "operator",
  "latitude": 42.4304,
  "longitude": 19.2594,
  "address": "Bulevar Svetog Petra Cetinjskog, Podgorica",
  "connector_types": ["CCS2", "Type 2"],
  "charge_points": [
    {
      "identity": "CP001",
      "connectors": [{ "connector_id": 1, "status": "Charging", "error_code": "NoError" }]
    }
  ]
}
```

For a `source: "external_reference"` site, `charge_points` is always `[]` — there is nothing
further to fetch than what §F already returned in the list; the frontend panel for such a site
should show its name/address/connector types and an "unknown" status, not attempt to open a
WebSocket subscription for live updates that will never arrive (§G).

`404` if `site_id` does not exist.

### `GET /api/v1/charge-points/{identity}`

One charge point on its own — used for a standalone charger, or when the frontend drills into one
charger from within a multi-CP site panel:

```json
{
  "identity": "CP001",
  "site_id": "670f...",
  "connectors": [{ "connector_id": 1, "status": "Charging", "error_code": "NoError" }],
  "current_transaction": { "transaction_id": 42, "meter_start": 0, "latest_meter_value": 1500 }
}
```

`current_transaction` is `null` when no connector on this charger has an open transaction.
`404` if `identity` is not a registered charge point.

## G. WebSocket contract

Two endpoints:

- **`WS /api/v1/ws/sites/{site_id}`** — every event from §E.2 for every charge point currently
  belonging to this site. Resolve the site's member charge point identities **once, at connect
  time**; `api/live.py`'s connection manager keeps that identity set per open socket and checks
  membership per incoming Redis event — it does not need to re-query MongoDB per event.
- **`WS /api/v1/ws/charge-points/{identity}`** — events for exactly one charge point.

On connect, before forwarding any live event, the server SHOULD send one initial message shaped
like the matching REST detail response (§F) so the client has something to render immediately.

If `site_id`/`identity` does not exist, close the connection immediately with code 1008 (policy
violation) and a reason string — do not accept the connection and then send nothing. A
`source: "external_reference"` site (§F) is a real, existing site with zero member charge
points, not a nonexistent one: **MUST** also close with code 1008, since it will never have a
live event to send — but the reason string should say why (no operator-managed charger at this
location), distinct from "no such site at all".

If the Redis subscriber (§C) is not running, refuse new connections with close code 1013 ("try
again later") rather than accepting a socket that will sit open and silent forever.

A client disconnecting (tab closed, panel closed) is normal and frequent — handle
`WebSocketDisconnect` by removing that connection from the manager's tracked set and otherwise do
nothing; it is not an error condition worth logging above debug level.

Do not build a `/ws/map` (all-of-Montenegro) feed as part of this file's scope — see
`07-public-map-platform.md` §F for why that is an explicit, deliberate later step, not part of
this service's first version.

## H. Error handling and status codes

- Unknown `site_id`/`identity` on a REST endpoint → `404`, with a small JSON body,
  `{"detail": "..."}` — FastAPI's default shape is fine, do not invent a custom error envelope.
- A malformed request (invalid ObjectId format, etc.) → `422`, FastAPI's default validation
  error behaviour (from declaring the right Pydantic/path types) — do not manually catch and
  reformat this.
- MongoDB unreachable **while the service is already running** (it was fine at startup, then the
  database dropped) → the underlying Beanie/pymongo error should surface as a `500`. Do not
  silently swallow it and return an empty list; a driver seeing "no chargers exist" when the real
  problem is "the database is down" is worse than a clear failure.
- Redis unreachable while running → REST endpoints are unaffected (§C). WebSocket behaviour is
  covered in §G.

## I. Creating/editing a `Site`

This service is read-only for everything charger-related (§D), but *something* needs to let an
operator place a new `Site` on the map, since that is genuinely new data this platform
introduces that `main.py`'s existing admin API has no concept of.

**Do not put this on the public API.** A `POST`/`PATCH` for `Site` is an operator action, not a
public one, and belongs on the *existing* trust boundary this project already has: extend
`main.py`'s admin API (a new `/admin/create-site` / `/admin/set-charge-point-location` pair,
following the exact pattern every other admin path already uses — see `SYSTEM_OVERVIEW.md` §5.4)
and a matching `operate.py` subcommand, gated by the same `ADMIN_TOKEN` as everything else there.
This keeps "which trust boundary can write what" simple and consistent: `main.py`'s admin API is
where all writes happen; this service (§A–H) only ever reads.

**Bulk-importing `source: "external_reference"` sites (e.g. from a PlugShare-style export) is a
one-off script, not an admin endpoint** — see `07-public-map-platform.md` §A's "Importing
third-party reference data" for exactly which fields to keep and which to discard. It writes
`Site` documents directly (via `models.py`, the same way `seed.py` writes `ChargePoint`
documents directly, without going through any HTTP API at all) and never touches `ChargePoint` —
there is nothing OCPP-related to create for a charger this project doesn't operate.

## J. Security and CORS

- No authentication token of any kind on this service, ever — that is what would turn it into a
  second way to reach the admin trust boundary, which §F's opening paragraph forbids outright.
- Enable `fastapi.middleware.cors.CORSMiddleware`, origins from `cors_origins()` (§B). An empty
  list MUST mean no browser origin is allowed yet, not "allow all" — do not default this to `*`.
- Rate limiting is a SHOULD, not blocking for a first version (see
  `07-public-map-platform.md` §H) — a public, unauthenticated, read-only API being used heavily
  is this feature working as intended; basic flood protection is still sane hygiene before this
  is placed on the open internet, at the reverse-proxy layer or via a lightweight in-process
  limiter, whichever this deployment already has conventions for.

## K. Testing

Follow this project's existing, strict "everything real, no mocks" testing philosophy
(`instructions/05-normal-charge-flow-tests.md`'s own rules) — it applies here exactly as it does
to the OCPP test suite:

- **Real MongoDB**, the same `db`/`mongodb_available` fixtures from `tests/conftest.py`, reused
  as-is. Skip cleanly if MongoDB is unreachable, exactly like every existing test file.
- **Real Redis for the WebSocket tests**, not `fakeredis` or an in-process stub. Add a new,
  session-scoped `redis_available` fixture in `tests/conftest.py`, modelled directly on the
  existing `mongodb_available` fixture: ping it once, `pytest.skip` the tests that need it if it
  is unreachable, and never fail the whole suite just because this optional piece of
  infrastructure isn't running on a given machine.
- **`httpx.AsyncClient` with `httpx.ASGITransport(app=app)`** for REST tests — this exercises the
  real FastAPI routing, dependency injection, and Pydantic serialization without needing a real
  TCP server or `uvicorn` process running.
- **A real WebSocket client** (`httpx`'s WebSocket support, or `websockets.connect` against a
  `uvicorn` instance started the same way `tests/conftest.py`'s `server` fixture starts `main.py`
  today — on an OS-assigned port, never assuming one is free) for the WebSocket tests, driving a
  real event through Redis and asserting it arrives, in order, at the right open connection and
  not at others.
- At minimum, test: `GET /api/v1/sites`' `aggregate_status` precedence for every one of the six
  cases in §F, including a `source: "external_reference"` site reporting `"unknown"` even though
  it has zero connectors to evaluate; `GET /api/v1/sites/{id}` and
  `GET /api/v1/charge-points/{identity}` returning 404 for an unknown id; a WebSocket connection
  to a site receiving an event published for one of its charge points and *not* receiving one
  published for an unrelated charge point; a WebSocket connection to a `source:
  "external_reference"` site being refused with code 1008; a WebSocket connection closing cleanly
  on client disconnect.

## L. Acceptance criteria

- `api/app.py` starts successfully against this project's existing MongoDB setup, with no changes
  required to `main.py`'s own tests — running `python -m pytest` from the project root MUST still
  show every existing test passing (this service is additive; it changes nothing about how
  `main.py` talks to a charger, only that `main.py` now also publishes to Redis).
- Every REST endpoint in §F, and both WebSocket endpoints in §G, exist and match their documented
  shapes exactly.
- No endpoint anywhere on this service can change charger state — verify by listing this
  service's own route table and confirming every route is `GET` or `WS`.
- REST endpoints keep responding correctly with Redis stopped; only new WebSocket connections are
  refused in that state (§C, §G).
- The new tests in §K pass, and skip (not fail) cleanly when MongoDB or Redis is unreachable.

## When you are done

Write the completion brief specified in `README.md#report-when-you-finish` — same shape as every
other task in this project. The intended reader for this one is a backend engineer, not
necessarily someone thinking about the map UI yet — the "use case" section should speak to that:
what does having this service let someone build that they couldn't build safely before (a public,
read-only view of live charger state, without giving that public view any path back into
commanding real hardware).
