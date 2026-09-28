# 10 — The frontend (Next.js, Tailwind CSS, MapTiler)

This is the self-contained, concrete spec for the actual website — the map a driver opens. It
supersedes `07-public-map-platform.md` §D's more general library discussion with a decided
stack and a precise interaction spec; that section now just points here.

**Depends on `08-public-api-service.md` being built** — there is nothing for this frontend to
fetch or subscribe to before that service exists. Depends conceptually on `07-public-map-platform.md`
§A for what a `Site` and a `ChargePoint` actually are; read that first if you haven't.

## Stack, decided

- **Next.js**, App Router. Not the Pages Router — App Router is the current, actively-developed
  default, and its Server/Client Component split maps naturally onto this app's one real
  distinction: server-fetched initial data vs. a browser-only interactive map.
- **Tailwind CSS**, via Next.js's own official `create-next-app` integration. Utility classes in
  markup, no separate CSS-in-JS library, no hand-written stylesheet beyond `globals.css`'s
  Tailwind directives.
- **MapTiler SDK** (`@maptiler/sdk` on npm) for the map itself. This is worth being precise about:
  **MapTiler's SDK is built directly on MapLibre GL JS** — it is not a separate, competing map
  engine, it's MapLibre with MapTiler's own conveniences (API-key handling, ready-made styles)
  layered on top. Its `Map` object exposes the same `addSource`/`addLayer`/clustering API as
  MapLibre GL JS directly, because it *is* that API. Every clustering/animation instruction in §F
  below is standard MapLibre GL JS behaviour, unchanged by going through MapTiler's wrapper.
- **TypeScript** (SHOULD, not MUST, but strongly recommended) — the wire contract with
  `08-public-api-service.md` is exact and worth typing once and checking at compile time rather
  than re-verifying field names by hand in every component.

## A. Naming: keep `Site`, do not call it "Charging Station"

The parent entity — a physical place that can hold one or more chargers — already has a name in
this project's design: `Site` (`07-public-map-platform.md` §A). Recommendation: **keep it.**
Calling it "Charging Station" would collide directly with this project's own, spec-mandated
vocabulary — OCPP 1.6 itself uses "Charge Point" (abbreviated "CP" throughout `main.py`,
`models.py`, and every instruction file in this project) to mean *one individual charger*.
"Charging Station" and "Charge Point" are close enough in wording that a reader — or a future
agent skimming code — could easily mix up "the place with three chargers" and "one charger",
which is exactly the ambiguity a good name should prevent, not introduce. `Site` has no such
collision, and is already implemented across `07`, `08`, and `09` — renaming it now would mean
updating three already-detailed specs for a strictly worse name.

The full vocabulary, for this file's purposes:

| This project's term | What it means | Where it's defined |
|---|---|---|
| `Site` | A physical place — what the user's request calls a "Charging Station" (a gas station, hotel, or car park that hosts one or more chargers). One row on the map. | `07-public-map-platform.md` §A |
| `ChargePoint` (CP) | One physical charger at a `Site`. OCPP's own term; `Site.id ← ChargePoint.site_id` is the one-to-many relationship the user described — **already exactly how `07-public-map-platform.md` §A specifies it**, no further backend change needed. | `models.py`, and `SYSTEM_OVERVIEW.md` §8 |
| Connector | One socket on a `ChargePoint`. Not directly relevant to the map; relevant inside a `Site`'s detail panel (§E). | `SYSTEM_OVERVIEW.md` §2 |

"Backend should migrate current CPs" is already fully covered: `07-public-map-platform.md` §A
defines the `site_id` relationship, and §B describes the one-off operator command to assign an
existing `ChargePoint` to a `Site` (or give it its own standalone coordinates). Nothing further
is needed on the backend for this frontend to consume — this file is entirely about the client.

## B. Project structure

```
app/
  layout.tsx              Root layout: <html>/<body>, imports globals.css.
  page.tsx                The landing page (Server Component) — see §D.
  globals.css             Tailwind directives only.
components/
  MapView.tsx             "use client" — owns the MapTiler map instance, the sites GeoJSON
                          source, clustering, and click handling. See §F/§G.
  StationPanel.tsx        "use client" — the detail panel for one clicked Site. Opens/manages
                          the live WebSocket subscription. See §E.
  SearchBox.tsx           "use client", optional (SHOULD) — a geocoding search input. See §H.
lib/
  api.ts                  Typed fetch wrappers for 08-public-api-service.md §F's REST endpoints.
  types.ts                TypeScript interfaces mirroring 08's schemas exactly — Site,
                          SiteDetail, ChargePointDetail, LiveEvent, and the aggregate_status /
                          source / SiteType string-literal unions. MUST stay in lockstep with
                          08 §F/§E.2's documented shapes; a change to either without the other is
                          a contract break.
  useStationLiveStatus.ts A client-side hook: given a siteId, opens
                          WS /api/v1/ws/sites/{siteId} (08 §G), parses each incoming event into
                          the local live-state shape, and cleans up the socket on unmount or when
                          siteId changes. See §E.
  statusColors.ts         The aggregate_status → Tailwind colour token mapping, §F.2 — one place,
                          reused by both individual pins and cluster styling.
```

New dependencies: `next`, `react`, `react-dom` (from `create-next-app`); `tailwindcss` (from the
same, via its official Next.js integration); `@maptiler/sdk`. A data-fetching library
(`@tanstack/react-query` / `swr`) is optional (SHOULD, not MUST) — this app's live data arrives
over the WebSocket, not by re-polling REST, so the caching/retry machinery those libraries exist
for isn't doing much real work here; plain `fetch` plus component state is enough.

## C. Configuration

Environment variables, Next.js convention: anything read in browser code MUST be prefixed
`NEXT_PUBLIC_` (Next.js only inlines prefixed variables into the client bundle; anything else
stays server-only and would read as `undefined` in a Client Component).

| Variable | Used by | Notes |
|---|---|---|
| `NEXT_PUBLIC_MAPTILER_API_KEY` | `MapView.tsx` | MapTiler's client-side SDK key is *meant* to be public — it will be visible in the browser's network requests and JS bundle regardless of how it's stored. Restrict it by HTTP referrer/domain on MapTiler's own dashboard; do not treat it as a backend secret or try to proxy it server-side. |
| `NEXT_PUBLIC_API_BASE_URL` | `lib/api.ts` | e.g. `https://api.example.com` — the `08-public-api-service.md` service's REST base. |
| `NEXT_PUBLIC_WS_BASE_URL` | `lib/useStationLiveStatus.ts` | e.g. `wss://api.example.com` — same service, WebSocket base. |

## D. The landing page

One route, `/`. No navigation, no marketing content, no separate pages for v1 — the map *is* the
product. Structure:

- `app/page.tsx` is a **Server Component**. It fetches `GET /api/v1/sites` server-side at
  request time (`cache: "no-store"` — this data is meant to be current, not cached across
  requests; revisit only if traffic volume ever makes that a real cost, which it will not at this
  project's scale) and passes the result as the initial prop to `<MapView>`. This avoids a blank
  map flashing before the first client-side fetch resolves.
- The page's only markup is a single container that fills the viewport
  (`h-screen w-screen`-equivalent Tailwind classes) with no padding/margin competing with it, and
  `<MapView>` fills that container edge-to-edge. Read "the centered div that contains the map in
  the middle" as describing this: a page with nothing else on it, the map as its sole, dominant,
  full-bleed content — not a small decorative widget in a corner. The map's own initial camera
  (§F.1) is what's actually "centered on Montenegro"; the page's simplicity is what makes that
  centred view the only thing the user sees on load.
- `<MapView>` is a **Client Component** (`"use client"` at the top of `MapView.tsx`) — it must
  be, since it touches `window`/the DOM directly through the MapTiler SDK, which Server
  Components cannot do.

## E. Clicking a Site: the detail panel and its WebSocket

This is exactly `07-public-map-platform.md` §D.2's interaction, made concrete in React terms:

1. Clicking an individual (non-cluster — see §G) pin sets a `selectedSiteId` state value and
   renders `<StationPanel siteId={selectedSiteId} />`.
2. `StationPanel` immediately renders from `GET /api/v1/sites/{id}` (08 §F) — fetch this on
   mount, show a lightweight loading state only for the brief gap before it resolves, never a
   blank panel.
3. In the same mount, call `useStationLiveStatus(siteId)`. That hook:
   - Opens `WS /api/v1/ws/sites/{siteId}` (08 §G) in a `useEffect`.
   - On each message, parses the event (08 §E.2's envelope) and merges it into local state keyed
     by `connector_id` (or `transaction_id` for `meter_value`/`transaction_*` events) — the panel
     re-renders with each update.
   - **Cleans up the socket in the `useEffect`'s cleanup function** — both on unmount (panel
     closed) and whenever `siteId` changes (user clicked a different pin without closing the
     panel first). Never leave a previous site's socket open after switching to a new one.
4. If the site has exactly one `ChargePoint` (`charge_points.length === 1` in the REST response),
   skip straight to that charger's own live view within the panel — no pointless one-item list
   (07 §D.2, point 3).
5. If a `source: "external_reference"` site is clicked (07-public-map-platform.md §A), **do not**
   open a WebSocket at all — 08 §G specifies the server refuses these with close code 1008
   anyway. Render the panel from the REST response alone (name, address, `connector_types`,
   `"unknown"` status), and skip step 3 entirely for this case.

## F. The map: MapTiler SDK, clustering, and colour

### F.1 Camera constraints (restated from `07-public-map-platform.md` §D.1 — this file is
authoritative going forward; update both together if these change)

Set on the MapTiler `Map` constructor: `center` ≈ `[19.3, 42.7]` (`[lon, lat]`), `zoom` ≈ `8`,
`maxBounds` ≈ south-west `[18.40, 41.85]` / north-east `[20.40, 43.60]`, `minZoom` ≈ `7`,
`maxZoom` ≈ `18`–`19`. **Approximate — verify Montenegro's real extent before shipping.**

### F.2 Pin colour: `aggregate_status` → a fixed palette

One place (`lib/statusColors.ts`), used by both individual-pin styling and cluster styling
(§G.3) so the two are never inconsistent with each other:

| `aggregate_status` | Colour | Meaning conveyed |
|---|---|---|
| `available` | Green (Tailwind `green-500`, `#22c55e`) | You can probably plug in here right now. |
| `occupied` | Amber (`amber-500`, `#f59e0b`) | In use, not broken — a normal, common state. |
| `reserved` | Violet (`violet-500`, `#8b5cf6`) | Booked, distinct from merely busy. |
| `faulted` | Red (`red-500`, `#ef4444`) | Something is actually wrong here. |
| `unavailable` | Grey (`gray-500`, `#6b7280`) | Known, operator-managed, currently off. |
| `unknown` | Light grey, outline-only (`gray-300`, `#d1d5db`) | Deliberately the least visually prominent — this system has no live relationship with this site at all (07 §A); it should read as "informational", not compete visually with sites you can actually get real-time status from. |

Apply this as a MapLibre **data-driven style expression** (a `match`/`case` expression on the
`aggregate_status` property of each GeoJSON feature) on the pin layer's `circle-color` (or
equivalent icon-color property) — not per-marker JavaScript. This is the reason MapTiler
SDK/MapLibre was the right choice over a DOM-marker library in the first place (see
`07-public-map-platform.md` §D's original reasoning): updating colour for many pins after a live
event is one `source.setData(...)` call with refreshed GeoJSON, not manually finding and
recolouring N marker DOM nodes.

### F.3 Data source and refresh

One GeoJSON source, built from the `GET /api/v1/sites` response (§D), one feature per `Site`
(each feature's `properties` carrying `id`, `name`, `aggregate_status`, `source`,
`connector_types`, etc. — everything §F of `08` returns). `cluster: true` on this same source
(§G) — clustering and live-colour styling apply to the same source, they are not two separate
concerns needing two sources.

## G. Clustering and the split/group animation

This section is the concrete answer to "points on a map should be animated: zoom out they group,
zoom in they split; clicking a group zooms in and splits it." Two genuinely different mechanisms
answer the two halves of that sentence — know which is which before implementing.

### G.1 Zoom-driven grouping/splitting: native, automatic, no custom code

Set `cluster: true`, `clusterRadius` (start around `50`, in pixels — tune once real data is on
the map), and `clusterMaxZoom` (the zoom level above which points always render individually —
start around `14`) on the sites GeoJSON source. This is Supercluster, built into MapLibre GL JS
(and therefore into the MapTiler SDK, per this file's opening note): **it recomputes which points
are grouped purely from the current zoom level, automatically, on every zoom/pan.** Scrolling out
regroups nearby pins into a cluster circle; scrolling in past `clusterMaxZoom` (or past whatever
zoom level a specific cluster's own points would separate at) reveals them individually. This is
the entire mechanism behind "user zooms out the group, user zooms in they split" — there is
nothing else to build for that half of the requirement.

At this project's real data volume (~134 sites, per `09-plugshare-import.md`'s import), cluster
sizes will generally be small — a handful to a few dozen per cluster in the densest areas (the
Budva/Kotor coast, Podgorica). Do not design cluster styling for hundreds-per-cluster; it will
never happen at this scale.

### G.2 Click-on-cluster: the standard "zoom to expansion" interaction

"When user clicks on a group, group zooms in and points split" is the well-documented MapLibre
GL JS cluster-click pattern — implement it exactly this way, it is not something to invent from
scratch:

1. On a map click, check whether the clicked point hit a cluster feature (query rendered
   features on the cluster layer at the click point).
2. If it did: read that cluster's id, call the source's `getClusterExpansionZoom(clusterId)` —
   this returns the exact zoom level at which *this specific cluster* would first break apart
   into smaller clusters or individual points.
3. Animate the camera there with `map.easeTo({ center: <the cluster's coordinates>, zoom:
   <that expansion zoom>, duration: ~500ms })`. The animated pan/zoom, followed by the
   already-automatic re-clustering from §G.1 revealing the now-separated points, together *is*
   "the group zooms in and points split."
4. If the clicked point hit an individual (non-cluster) feature instead, that's §E's "open the
   detail panel" path, not this one.

### G.3 Optional enhancement: colouring clusters by what's inside them (SHOULD, not required for v1)

A cluster circle can be more useful than a bare count: MapLibre's clustering supports
`clusterProperties` — custom aggregation expressions computed across every point folded into a
cluster (e.g., counting how many contained sites have `aggregate_status === "available"`,
`"faulted"`, etc.). If built, apply the **same precedence rule** `08-public-api-service.md` §F
uses for a single site's `aggregate_status` (any faulted → red-tinted cluster; else any
available → green-tinted; and so on) to the aggregated counts, so a cluster's colour answers "is
there likely something available inside this group" before the user even zooms in — consistent
with, not a separate rule from, §F.2's palette. This is a genuine, supported MapLibre capability,
not a hack — but it is explicitly optional for a first version; a plain neutral-coloured count
circle (the MapLibre/Mapbox default pattern) is a perfectly acceptable v1.

### G.4 What NOT to build for v1

A literal animation of individual pins visibly flying outward from a cluster's exact former pixel
position when it breaks apart is a known but meaningfully more involved technique (typically
built by hand with DOM markers interpolating position over a timed transition, since Supercluster
itself has no concept of "where a point was a moment ago"). **Do not build this for v1.** §G.1's
automatic re-clustering plus §G.2's animated camera move already read, to a user, as "the group
opened up" — the extra engineering cost of a true fly-apart animation buys a nicer flourish, not
a materially different user experience, and isn't worth its complexity until everything else here
is working end to end.

## H. Search (optional, SHOULD): Nominatim, with a caveat

A search box (`SearchBox.tsx`) that geocodes a typed place name and pans/zooms the map there is a
reasonable, small addition — implement it against **Nominatim**, as specified, if built.

One operational caveat worth knowing before relying on it in production: the public
`nominatim.openstreetmap.org` instance enforces a strict usage policy (an identifying
`User-Agent`/referrer and roughly one request per second) intended for light, non-commercial use
— it is not meant to back unlimited traffic from a public product. If this ever becomes a real
constraint, MapTiler (which this project is already paying for/keyed into for tiles) also offers
its own geocoding API under the same account and API key, which sidesteps a second vendor's rate
limits entirely. Start with Nominatim as specified; know this alternative exists if usage grows
enough to need it.

## I. Testing

An End-to-end smoke test (Playwright, SHOULD) covering: the page loads and the map renders
bounded on Montenegro; at least one pin is visible; clicking an individual pin opens the panel and
the panel eventually shows a status (mock or hit a real `08` instance with seeded data); clicking
a cluster changes the camera's zoom level. Full behavioural coverage of live WebSocket updates is
better exercised against a real running `08` instance manually (drive a charger through
`simulate_charge_point.py` and watch the open panel update) than faked in an automated test — this
mirrors the same "real infrastructure over mocks" preference the rest of this project holds,
applied as far as is practical to a frontend.

## J. Acceptance criteria

- Loading `/` shows a full-bleed map, camera constrained and initially centred on Montenegro per
  §F.1, with no other page content.
- Pins are coloured per §F.2's exact palette, one per `Site` (never one per `ChargePoint`).
- Zooming in/out regroups/splits pins with no additional interaction (§G.1).
- Clicking a cluster animates the camera to that cluster's expansion zoom (§G.2).
- Clicking an individual pin opens a panel that first renders from REST, then updates live via
  WebSocket (§E) — verified by driving a real charger through `simulate_charge_point.py` against
  a real `08` instance while the panel is open and watching it change.
- Closing the panel, or clicking a different pin, closes the previous WebSocket connection —
  verify this by inspecting open connections in the browser's devtools network panel, not just by
  reading the code.
- A `source: "external_reference"` site's panel never attempts a WebSocket connection.

## When you are done

Write the completion brief specified in `README.md#report-when-you-finish`. The intended reader
is a frontend engineer with no prior context on OCPP or this project's backend — define `Site`/
`ChargePoint` the first time you use them, exactly as this file does.
