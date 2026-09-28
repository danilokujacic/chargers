# 07 — Public map platform

Everything built so far (`instructions/01`–`06a`) is the OCPP-facing backend: it talks to
chargers, and an operator drives it through `operate.py`. Nothing public-facing exists yet.

This file specifies a new, separate thing: a **public website with a map of Montenegro showing
every charge point, with live status**. A driver opens it, sees pins, clicks one, and watches its
status update in real time — without needing an OCPP client, an admin token, or any of the
machinery the rest of this project is built from.

Read `SYSTEM_OVERVIEW.md` first if you have not already — this file assumes you understand what
a Charge Point, a connector, and a Transaction are, and how `main.py`/`models.py` currently work.

**The backend service itself has its own, deeper spec: `08-public-api-service.md`.** This file
covers the data model (§A), why no migration framework is needed (§B), a short summary of what
that service exposes (§C) so the frontend section below has something concrete to build against,
the frontend itself (§D), deployment (§E), and the overall order (§F). If you are building the
FastAPI service, go read `08` — it is self-contained and more detailed than §C here.

## How to use this file

Same rules as `06-remaining-flows.md`: implement exactly what a section specifies; **MUST** is a
hard requirement, **SHOULD** is a strong default you may deviate from with a stated reason.
Sections are lettered so they can be built and reported on independently, but — unlike
`06-remaining-flows.md`'s sections, which were largely independent of each other — **these
sections have a real dependency order**. See §F for the order to actually build them in; do not
start the frontend (§D) before the API (§C, detailed in `08`) exists to talk to, and do not start
the API before the data model (§A) exists to read.

This is a **design document**, not existing code. Every section below is something to build.
Where it shows a JSON shape, that is the contract to implement, not a suggestion. Finish each
section by writing the completion brief described in `README.md#report-when-you-finish`, exactly
as every other task in this project does.

---

## A. Data model: a generic `Site`, not a gas-station-specific one

A charge point is not always a standalone unit at the roadside. In Montenegro (and everywhere
else) it is very often two or more chargers physically co-located at one place — most commonly a
fuel station, but just as plausibly a hotel car park, a shopping centre, or a public car park.
Modelling this as `GasStation` would be wrong the first time a hotel's chargers need the same
treatment. Build one generic concept instead.

### New document: `Site`

Add to `models.py`, next to the other `Document` subclasses, following this project's existing
style exactly (Beanie `Document`, `IndexModel`s in `Settings`, a `created_at`/`updated_at` pair
via `_utcnow`):

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Operator-facing label, e.g. `"Petrol Podgorica — Bulevar"`. |
| `site_type` | `SiteType` (new enum) | `gas_station`, `hotel`, `parking`, `other`. Not an OCPP concept — define this enum yourself in `models.py`, it does not exist in the `ocpp` library. |
| `latitude` | `float` | Required. WGS84 decimal degrees. |
| `longitude` | `float` | Required. WGS84 decimal degrees. |
| `address` | `str \| None` | Human-readable, optional. Filled by reverse geocoding (see `10-frontend-nextjs.md` §H for the geocoding approach) when an operator places a site by clicking the map, or typed by hand. |
| `source` | `SiteSource` (new enum) | `operator` (default) or `external_reference`. See "Sites we operate vs. sites we only know about" below — this is not optional, it changes what the API is allowed to claim about a site. |
| `external_id` | `str \| None` | The source platform's own id for this location (e.g. a PlugShare location id), kept only so a re-run import can recognise "already imported" and update rather than duplicate. Never used as a foreign key into anything. |
| `external_url` | `str \| None` | Optional attribution/provenance link back to the source listing. Display-only. |
| `connector_types` | `list[str] \| None` | Physical plug standards available at this site (e.g. `["CCS2", "CHAdeMO", "Type 2"]`), as plain strings — not an enum, and never a third-party numeric code (see the import section below for why). |
| `external_charge_point_count` | `int \| None` | **Only meaningful for `source: "external_reference"` sites.** How many physical chargers a third-party source reported at this location. See "Two different meanings of 'how many chargers'" below for why this can't just be derived the way it is for operator sites. |
| `created_at` / `updated_at` | `datetime` | This project's usual pattern. |

Index: nothing beyond MongoDB's default `_id` is required for the fleet sizes this project is
sized for (see §B for why a geospatial index is a documented *future* step, not something to
build now).

### Sites we operate vs. sites we only know about

Not every charging location on the map is one this system controls. A public map of "every
charging point in Montenegro" is far more useful if it also shows chargers run by other
operators — but this project has no OCPP relationship with those chargers and never will, so they
**MUST NOT** become `ChargePoint` documents. A `ChargePoint` requires a real
`authorization_key_hash` and an `identity` that will genuinely appear in an OCPP-J connection URL;
neither of those exist for a charger someone else operates.

`Site.source` MUST be `external_reference` for any site with no `ChargePoint` documents pointing
at it via `site_id` — a location this project knows *about* but has no live connection *to*.
`aggregate_status` (defined in `08-public-api-service.md` §F) MUST report `"unknown"` for such a
site, never `"unavailable"`: `"unavailable"` claims a live check was made and came back negative,
which would be a lie for a site this system has no way to actually check. `"unknown"` honestly
means "we don't operate this one."

`Site.source` is `operator` (the default) for every site with at least one real `ChargePoint`
pointing at it — these are the only sites `aggregate_status` computes a real live value for, per
§F's existing precedence rules.

### Two different meanings of "how many chargers"

For an `operator` site, "how many chargers does this location have" is always answered by
counting real `ChargePoint` documents with that `site_id` — it is always current, because a real
`ChargePoint` only exists when this system genuinely manages one. §B's "don't store a derivable
count" guidance applies fully here: never persist a separate number that could drift from that
count.

For an `external_reference` site, there **is no** live source of truth to count — this system has
never connected to any of these chargers and never will. The only way to know "how many chargers
are here" is whatever the third-party source said at the time it was imported, which is exactly
why `Site.external_charge_point_count` exists as a genuinely stored (not derived) field for this
case only: there is nothing to derive it *from*. It goes stale only as fast as the upstream data
does, and only ever gets refreshed by re-running the import (see `09-plugshare-import.md`) — it
is never live, and the API (`08-public-api-service.md` §F) must not present it as if it were.

### Importing third-party reference data

The concrete, field-by-field mapping for one real dataset (a PlugShare export for Montenegro) —
including which fields turned out to be constant/useless, which needed a filter instead of a
mapping, and the exact import script to write — is `09-plugshare-import.md`. The short version,
if you're mapping a *different* source and `09` doesn't apply directly:

- **Keep, real-world facts:** name, latitude/longitude, address, and a location-level
  connector-type list, if the source provides one, as plain human-readable strings.
- **Keep, for import bookkeeping only, never as live data:** the source's own id (→
  `external_id`, so a re-run recognises "already imported" instead of duplicating) and its own
  permalink (→ `external_url`, attribution only).
- **Keep, informational only, explicitly not live:** a station/outlet count, if the source
  provides one (→ `external_charge_point_count`, per the section above).
- **Discard entirely:** the source's own presentation (icons, styling codes), internal
  scoring/rating fields, and anything the source uses to mean "currently available" / "in use" /
  a live per-outlet status — these go stale the moment they're copied, often *are already stale*
  in the source's own export, and your own OCPP connection (which only exists for `operator`
  sites) is the only legitimate source of live status.
- **Never decode a third party's own numeric connector-type codes** if it has any (as opposed to
  a human-readable string list) — they are typically undocumented, and easy to confuse with this
  project's own `connector_id` (which means "which numbered socket", not "which plug standard").
- **Never create a `ChargePoint`** from a third party's individual station/outlet record — there
  is no OCPP relationship to represent for a charger this system doesn't operate.

### `ChargePoint` gets two new fields

Add to the existing `ChargePoint` document in `models.py` — do not create a second charge-point
model:

- `site_id: PydanticObjectId | None = None` — which `Site` this charger belongs to, if any.
  (Beanie re-exports `PydanticObjectId` from `beanie`; this project has not needed it before now.)
- `latitude: float | None = None` / `longitude: float | None = None` — a charger's **own**
  location, used only when it has no `site_id`.

A charge point's effective map location, in order of preference:

1. If `site_id` is set, use that `Site`'s `latitude`/`longitude`. (A charger's own
   `latitude`/`longitude` fields, if also set, are ignored in this case — a site's location is
   authoritative for every charger that belongs to it.)
2. Else, if the charger's own `latitude`/`longitude` are both set, use those (a standalone
   charger — a "site of one" with no shared location).
3. Else, the charger has no known location yet and **MUST NOT** appear on the map. It still
   exists and works over OCPP exactly as today; it is simply unmapped until an operator sets a
   location for it.

Write this as a small helper — e.g. `ChargePoint.map_location()` returning `(lat, lon) | None` —
rather than letting every caller re-derive the three-way fallback by hand. The API service
(`08-public-api-service.md`) is the main caller of this.

### Why a generic `Site`, not `GasStation`

- A hotel, a shopping centre car park, and a fuel station are the same *shape* of thing for this
  system's purposes: a physical location with a name and coordinates, hosting one or more
  chargers. Nothing about routing, display, or live status differs between them — only the label
  an operator sees. One document with a `site_type` field costs nothing extra today and avoids a
  second near-identical model (`Hotel`) the first time this comes up, which it will.
- Keeping `latitude`/`longitude` **on the site**, not duplicated onto every charger at that site,
  means moving a station's shared location is one write, not N, and there is no way for two
  chargers at the same physical place to disagree about where that place is.
- Keeping optional `latitude`/`longitude` **also on `ChargePoint`** (for the no-`site_id` case)
  is what lets this project's existing seeded test chargers (`CP001`, etc. — which will never
  have a `site_id`) still be placeable on a map without inventing a fake single-charger `Site`
  for each one.

---

## B. Schema evolution: why this needs no migration framework

If you are used to SQL/Django/Alembic-style migrations, the natural question is "what migration
adds these columns?" MongoDB (via Beanie) does not work that way, and understanding *why* saves
you from building machinery this project does not need.

- MongoDB collections are schemaless. Adding `site_id`, `latitude`, `longitude` to the
  `ChargePoint` `Document` class is the entire change — existing documents in the database simply
  do not have those fields until something sets them. Beanie/Pydantic reads a missing optional
  field as its declared default (`None`), not an error. **There is nothing to run against
  existing data for this specific change.**
- This project already has a working "run this on every startup, safely, every time" mechanism:
  `init_beanie(document_models=[...])`, called from `models.init_db()`, creates every declared
  `IndexModel` idempotently on every process start. Adding `Site` to that list (see
  `document_models=[...]` at the bottom of `models.py`) is the only "migration" a brand-new
  collection needs — MongoDB creates the collection itself, lazily, on first write.
- **The one real task here is not a schema migration, it is a data-entry task**: this project's
  already-seeded chargers (`CP001`, `CP002`, `CP003`) have no location and will not appear on the
  map until an operator gives them one. That is expected, not a bug to migrate around. Build a
  small operator-facing way to do this — either a new `register_charge_point.py` subcommand
  (e.g. `set-location <identity> <lat> <lon>`, or `set-site <identity> <site_id>`) or an
  equivalent admin API addition on `main.py` (following the existing `send_*`/admin-path
  pattern, per `08-public-api-service.md` §I) — so this is a one-line operator command, not a
  manual MongoDB shell edit.
- **Do not add a geospatial (`2dsphere`) index or a GeoJSON `location` field yet.** That is the
  right tool the moment this system needs *server-side* proximity queries ("chargers within 5km
  of me"). At Montenegro's scale (a single small country, realistically dozens to a few hundred
  charge points for the foreseeable future), a "return every site" endpoint is entirely
  sufficient, and the frontend can do any distance sorting it wants client-side. Document this as
  a deliberate deferral, not an oversight, if you touch this area again later.

---

## C. The public API, in brief

The full spec — process isolation, the Redis event schema, every REST/WebSocket contract,
security, and testing — lives entirely in **`08-public-api-service.md`**. What follows here is
only the summary the frontend section (§D) needs to reference:

- A second, isolated process (FastAPI, its own `api/` package), sharing nothing with `main.py`
  except MongoDB (read) and Redis (subscribe to live events `main.py` publishes).
- `GET /api/v1/sites` — every site (and every standalone charger, as a one-charger site), with a
  computed `aggregate_status` for colouring a pin, and `latitude`/`longitude` for placing it.
- `GET /api/v1/sites/{site_id}` — one site's full detail, every charge point, every connector.
- `GET /api/v1/charge-points/{identity}` — one charge point's full detail, including its
  currently open transaction's latest meter reading, if any.
- `WS /api/v1/ws/sites/{site_id}` and `WS /api/v1/ws/charge-points/{identity}` — live event
  streams, one message per change, for exactly the site or charge point subscribed to.
- Read-only, unauthenticated, no endpoint anywhere on it can change charger state — every write
  still goes through `main.py`'s existing admin API and `operate.py`.

Build `08` before starting §D — the frontend has nothing to render without it.

---

## D. The frontend, in brief

The full spec — the decided stack (Next.js, Tailwind CSS, the MapTiler SDK), project structure,
the Montenegro camera constraints, why pins are per-`Site` rather than per-`ChargePoint`, the
clustering/split-animation behaviour, and the detail-panel/WebSocket lifecycle — lives entirely in
**`10-frontend-nextjs.md`**. That file also settles a naming question worth knowing about even if
you're not building the frontend yourself: it recommends keeping `Site` as this project's name for
what a driver would casually call a "charging station", specifically because "Charging Station"
collides with OCPP's own "Charge Point" (CP) terminology already used everywhere else in this
project.

Build `10` after `08` — the frontend has nothing to render or subscribe to before the API exists.

---

## E. Suggested deployment topology

Five independently-runnable pieces, sharing two pieces of shared state:

| Service | What it is | Talks to |
|---|---|---|
| `mongo` | The existing MongoDB instance | Everything reads/writes here |
| `redis` | New — the pub/sub transport (`08-public-api-service.md` §E) | `main.py` publishes; the API process subscribes |
| `cs` | `main.py`, unchanged in purpose, gains one Redis publisher | `mongo`, `redis`, real chargers over OCPP-J |
| `api` | The new `api/` FastAPI service from `08-public-api-service.md` | `mongo` (read), `redis` (subscribe), browsers (REST + WS) |
| `frontend` | The static build from §D | `api` only, over HTTPS, from the browser |

`cs` and `api` MUST remain separate deployable units even if they end up running on the same
host initially — the whole point of keeping them apart (see `08-public-api-service.md`'s opening
section) was the trust and blast-radius boundary between "talks to physical chargers" and "talks
to the public internet", and that boundary should exist regardless of how many machines the
deployment happens to use on day one.

---

## F. Order of implementation, and acceptance criteria

Build in this order — each step needs the previous one to test against:

1. **§A** (data model) — can be verified with nothing but MongoDB and a Python shell: create a
   `Site`, attach a `ChargePoint` to it via `site_id`, confirm the three-way location fallback
   resolves correctly, confirm an unrelated existing test file (`tests/test_normal_charge_flow.py`
   at minimum) still passes unchanged.
2. **§B**'s operator command (the location-setting CLI addition) — small, and needed before there
   is any real data for the API to read.
3. **`08-public-api-service.md` §E's Redis publishing**, added to `main.py` — verify by
   subscribing to `charging-events` by hand (e.g. `redis-cli SUBSCRIBE charging-events`) while
   driving `simulate_charge_point.py` through a scripted session, and confirming each event
   actually appears with the right shape.
4. **The rest of `08-public-api-service.md`** (the API process itself, REST then WebSocket) —
   follow that file's own §L acceptance criteria and §K testing guidance directly.
5. **`10-frontend-nextjs.md`** (the frontend) — this is the only step that needs a browser to
   verify. Follow that file's own §J acceptance criteria directly: the map loads centred and
   bounded on Montenegro, pins are one-per-site and correctly coloured, clicking a pin shows live
   updates that visibly change when you drive the underlying charger through
   `simulate_charge_point.py` in another terminal, panning cannot leave Montenegro, and nearby
   sites cluster/decluster on zoom with the click-to-expand animation working.

Acceptance criteria for the whole platform: `08-public-api-service.md`'s and
`10-frontend-nextjs.md`'s own acceptance criteria both hold; `main.py`'s existing OCPP behaviour
and its full existing test suite (sections A–H) are unaffected — running `python -m pytest` from
the project root MUST still show every existing test passing, since none of this changes anything
about how `main.py` talks to a charger, only what else it now also tells Redis; and a person with
no other context can open the frontend, see Montenegro, click a pin, and watch a real status
change happen live.

## When you are done

Write the completion brief specified in `README.md#report-when-you-finish` for whichever
section(s) you complete — same shape as every other task in this project. Because this spans a
genuinely different audience per section (a backend engineer for §A–C, a frontend engineer for
§D), it is fine — expected, even — for the "use case" paragraph to speak to whichever audience
that section is actually for, rather than forcing one framing across all of it.
