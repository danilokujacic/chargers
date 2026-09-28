# 09 — Importing the PlugShare export into MongoDB

`07-public-map-platform.md` §A defines the `Site` document, the generic distinction between
`source: "operator"` and `source: "external_reference"` sites, and the general keep/discard
rules for third-party charging-location data. This file is the concrete migration built from
those rules, applied to one real file: **`montenegro_only.json`**, a PlugShare export of charging
locations across Montenegro, already sitting in the project root.

**Depends on `07-public-map-platform.md` §A already being implemented** — the `Site` document,
its `SiteType`/`SiteSource` enums, and the `external_id`/`external_url`/`connector_types`/
`external_charge_point_count` fields must exist in `models.py` before this file's script can run.

## What is actually in this file

These numbers come from inspecting the real file, not from PlugShare's documentation (which
isn't public) — trust them over any general assumption about what a PlugShare export "usually"
looks like:

- **135 location entries**, each shaped like the example already discussed in this project's
  design conversation (a `location` with a nested `stations[]` array, each station with a nested
  `outlets[]` array).
- `name`, `address`, `latitude`, `longitude`, `connector_types`, `access`, `coming_soon`,
  `under_repair`, `is_fast_charger`, `id`, `url`, `station_count` are present on **every** entry —
  none of these are ever missing in this file. `score` is missing on 71 of 135.
- `access` is `1` on **every single entry, with no exceptions** — it carries zero information in
  this dataset regardless of what the number means in PlugShare's own scheme. Discard it
  outright; do not even keep it as an unused field.
- `coming_soon` is `True` on exactly **1** of 135 entries — that one does not physically exist
  yet as a usable charging point and MUST NOT be imported as a `Site` at all.
- `under_repair` is `True` on **15** of 135 entries. These are real, physically existing
  locations, just reportedly out of service at export time. **Import them anyway** — do not skip
  them, and do not persist the flag (see "Why `under_repair` is imported but not stored" below).
- `connector_types` values across the whole file are exactly these seven strings: `Type 2` (112
  occurrences), `CCS2` (27), `CHAdeMO` (10), `Wall (Euro)` (10), `Three Phase` (4),
  `Caravan Mains Socket` (1), `J-1772` (1). All are clean, human-readable strings — no encoding
  issues, nothing to translate. Store the list exactly as given.
- **Zero duplicate `id` values** across all 135 entries — `id` is safe to use as the dedup key for
  re-imports.
- **Two duplicate `name` values** (`"Dukley Hotels & Resort"` appears twice, at two different real
  addresses in Budva; `"Porto Montenegro"` appears twice, at two different real addresses in
  Tivat). This is concrete proof `name` MUST NOT be used as the dedup key — only `id` is reliably
  unique here.
- Every entry's `latitude`/`longitude` falls within Montenegro's real extent — no bad
  coordinates to filter in this file, though the script should still validate defensively (a
  future re-export is not guaranteed to be this clean).
- `station_count` matches `len(stations)` exactly for all 135 entries — but derive the count from
  `len(stations)` at import time rather than trusting the separate `station_count` field, as a
  matter of not trusting a redundant summary field when the real array is right there.
- **No field in this export reliably indicates venue category** (fuel station vs. hotel vs.
  parking, etc.). `icon_type` looked like a plausible candidate but does not correlate with venue
  type at all on inspection — e.g. `"Hotel Aria"` and `"EKO Bijelo Polje"` (a fuel station) share
  the same `icon_type` value, while other hotels appear under different `icon_type` values
  entirely. **Do not guess a `site_type` from a keyword search on the name either** — a location
  named `"Renault Podgorica"` is a car dealership, which fits none of `SiteType`'s existing
  values cleanly, and silent misclassification is worse than an honest default. Set every
  imported `Site`'s `site_type` to `SiteType.other` and leave venue re-categorization to an
  operator reviewing individual sites by hand later, if it matters.

## Why `under_repair` is imported but not stored

This is the same principle `07-public-map-platform.md` §A already establishes for live-status
fields ("don't import a third party's live/derived data as if it were durable"), applied
consistently: `under_repair` is PlugShare's snapshot of a transient fact at the moment this file
was exported. Persisting it would mean this system claims to know something about current
usability it has no way to actually verify or keep up to date — and it is functionally
redundant anyway, since `aggregate_status` for every `external_reference` site already reports
`"unknown"` regardless (per `08-public-api-service.md` §F), which is the honest signal here:
"this system doesn't know this location's current status," which is just as true whether or not
it happened to be reported under repair at export time.

## The script: `import_plugshare_sites.py`

A new top-level script, in this project's existing flat layout (it is a one-off/occasionally-
rerun operator tool, the same category as `seed.py`, not a service) — `raise
SystemExit(asyncio.run(main(parse_args())))` entry point, `argparse` with long options, connects
via `models.init_db()` and fails with the same clear "MongoDB unreachable" message every other
script in this project already uses on a `PyMongoError`. No new dependency is needed —
`json`, `pathlib`, and `models.py` are everything this requires.

```
python import_plugshare_sites.py [--file montenegro_only.json] [--dry-run]
```

- `--file` (default `montenegro_only.json`, the path it already sits at in the project root):
  the export to read.
- `--dry-run`: parse and report exactly what would happen — created / updated / skipped counts,
  and the name of each skipped entry with its reason — without writing anything to MongoDB. Run
  this first before ever running the real import.

### Per-entry logic

For each entry in the JSON array, in order:

1. **Skip if `coming_soon` is `True`.** Report it as `skipped (coming soon): <name>`. (Exactly 1
   entry in the current file.)
2. **Validate `name`, `latitude`, `longitude` are present and not null.** If any is missing,
   `skipped (invalid): <name or id>` and move on — do not let one bad row abort the whole import.
   (0 entries fail this in the current file; this guard is for whatever the *next* export looks
   like.)
3. **Look up an existing `Site` by `external_id == str(entry["id"])`.**
   - **Found** → update it with `Document.set({...})` (this project's usual atomic-`$set`
     pattern for a targeted field update), setting `name`, `latitude`, `longitude`, `address`,
     `connector_types`, `external_url`, `external_charge_point_count` (= `len(entry["stations"])`),
     and `updated_at`. **Do not touch `site_type` on an update.** An operator may have manually
     corrected it away from the `other` default after the first import; a re-import silently
     reverting that correction would be a real, avoidable regression. Report
     `updated: <name>`.
   - **Not found** → create a new `Site(source=SiteSource.external_reference,
     site_type=SiteType.other, external_id=str(entry["id"]), ...)` with the same fields as above,
     and `.insert()` it. Report `created: <name>`.

This script has no concurrent writers — it processes one JSON array sequentially in a single
process. Unlike the atomic-update patterns this project's OCPP handlers need (documented in
`06a-remaining-flows-progress.md`, where multiple concurrent async handlers can race on the same
document), there is no race to guard against here; a plain find-then-write is correct and there
is no need to add `DuplicateKeyError` handling or an upsert-with-retry pattern for this script.

### Explicitly discarded, and why (matching the analysis above)

| Source field | Verdict |
|---|---|
| `access` | Discard — constant `1` across every entry in this file, zero information. |
| `available_station_count`, `in_use_station_count` | Discard — PlugShare's own live-status fields, `null` for every entry in this file regardless. |
| `score` | Discard — missing on over half the entries even in the source, and meaningless outside PlugShare's own review community even when present. |
| `icon`, `icon_type` | Discard — presentation-only, and (checked directly) not even a reliable proxy for venue category. |
| `is_fast_charger`, `station_count` | Discard as stored fields — both are derivable from data already kept (`connector_types` and `len(stations)` respectively); never store a fact you can already compute from what you kept. |
| `under_repair` | Used only as documented above (imported anyway, never persisted) — see "Why `under_repair` is imported but not stored". |
| `stations[].id`, `stations[].outlets[]` (`id`, `connector`, `is_dc`, `power_type`, `kilowatts`, `power`, `status`) | Discard entirely. No `ChargePoint` is created (§A of `07`); the only fact worth keeping from this nested structure is the count, already captured as `external_charge_point_count`. |
| `url` | Keep, as `Site.external_url` — attribution only. |

## Testing

Follow this project's real-infrastructure testing convention (no mocks): add
`tests/test_plugshare_import.py`, reusing the existing `db` fixture from `tests/conftest.py`, with
a small synthetic fixture (3–4 entries inline in the test file, not the real 135-entry file) that
specifically covers:

- A normal entry creates a `Site` with `source: "external_reference"`, the right
  `external_charge_point_count`, and `site_type: "other"`.
- An entry with `coming_soon: true` creates nothing.
- Running the import twice against the same entry creates it once and updates it the second time
  (assert the total document count doesn't grow on the second run).
- A `Site` whose `site_type` was manually changed away from `"other"` after the first import
  keeps that value after a second import run — this is the one behaviour that would be genuinely
  easy to get wrong and expensive to discover late.

## Acceptance criteria

- `python import_plugshare_sites.py --dry-run` against the real `montenegro_only.json` reports
  134 would-be-created and 1 would-be-skipped (coming soon), with zero would-be-invalid.
- Running it for real creates exactly 134 `Site` documents, every one with
  `source: "external_reference"` and a unique `external_id`.
- Running it a second time creates zero new documents and updates all 134.
- `montenegro_only.json` is committed to the repository (it is not covered by `.gitignore`) —
  this is now real input this project's tooling depends on, not a scratch file.
- `python -m pytest` from the project root still passes, including the new test file.

## When you are done

Write the completion brief specified in `README.md#report-when-you-finish`. The "use case" here
is squarely operational: before this script exists, the public map (`07`/`08`) has nothing to
show beyond whatever chargers this system happens to operate itself, which for a new deployment
is close to zero — this is what actually populates it with the ~134 real, named, located
charging points a driver in Montenegro would recognise.
