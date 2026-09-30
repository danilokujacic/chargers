# 12 — Removing the demo fleet

`11-demo-fleet.md` turned 134 PlugShare sites into simulated sites, each with a simulated charger
per station. This file removes **all of that data** and puts the system back exactly where 09 left
it: 134 `external_reference` sites showing `unknown`, plus the operator sites and chargers that
existed before (CP001 at "Petrol Podgorica — Bulevar", and Hotel Budva).

This removes **data only**. The code from 11 stays: the seed and fleet scripts, multi-connector
support, the API's connector specs, and the frontend's pin refresh and keyed live state. With no
simulated data left, all of it is inert or simply an improvement. Running `seed_demo_fleet.py`
again recreates the demo, and that round trip must work.

**Depends on 11.**

## What counts as demo data

These are the only selectors. Never select anything by name, by the `PS-` prefix alone, or by
"looks like PlugShare".

| Data | Selector | Action |
|---|---|---|
| Mock chargers | `ChargePoint.simulated == True` | delete, **last** (see Order) |
| Everything a mock charger generated | `charge_point_identity` ∈ mock identities, in every document model that has that field (see below) | delete |
| Demo idTags | `IdTag.parent_id_tag == "DEMO-FLEET"` (covers `DEMO-<station>-<connector>` and `DEMO-REMOTE`) | delete |
| Converted sites | `Site.source == "simulated"` | set `source = "external_reference"` and `updated_at`; touch nothing else |
| Local files | `demo_fleet_manifest.json`, `demo_fleet_state.json` (paths from the flags) | delete |

**The per-identity collections.** Today nine models have a `charge_point_identity` field:

- `Transaction`
- `ConnectorStatus`
- `ConfigurationEntry`
- `Reservation`
- `LocalListState`
- `FaultEvent`
- `FirmwareUpdate`
- `DiagnosticsRequest`
- `InstalledChargingProfile`

Do not hard-code this list in the script. Instead:

1. Move the inline `document_models=[...]` list in `models.init_db` into a module-level
   `DOCUMENT_MODELS` constant, and have `init_db` use it.
2. In the removal script, select every model in `DOCUMENT_MODELS` whose `model_fields` contains
   `charge_point_identity`.
3. Add a test asserting that the derived set is exactly those nine. A model added later then fails
   that test, and whoever adds it must decide consciously whether demo data can land in it.

**Must never be touched:**

- any charger with `simulated=False`, and everything keyed to its identity
- `operator` sites
- idTags outside the `DEMO-FLEET` group
- `charge_point_credentials.json`
- the `counters` collection

On the counters: the `transaction_id` counter must never go backwards. OCPP 1.6 s4.8 needs
transaction ids to be unique across the Central System, so deleting demo transactions must not
free their ids for reuse.

## The script: `remove_demo_fleet.py`

A top-level operator script in the same style as `seed_demo_fleet.py`: `argparse` with long
options, the `__main__` guard, and a clear message on `PyMongoError`.

```
python remove_demo_fleet.py [--dry-run] [--force] [--manifest demo_fleet_manifest.json] [--state-file demo_fleet_state.json]
```

1. **Refuse while the fleet is running.** Read the state file. If `fleet_alive_at` is less than
   30 s old, exit 1 with "stop run_demo_fleet.py first (Ctrl+C), then re-run", unless `--force` is
   given.
   - **Why:** a connected mock charger keeps sending StatusNotification and MeterValues, and
     `main.py` would recreate `ConnectorStatus` rows for a charger this script just deleted,
     leaving orphans.
   - A stale timestamp (a hard-killed fleet) or a missing file is fine to proceed with.
2. **Collect** the mock identities and print what will happen, as a count per collection, for
   example:

   ```
   chargers 195
   transactions 1432
   connector_statuses 255
   fault_events 44
   id_tags 256
   sites to revert 134
   files 2
   ```

   `--dry-run` stops here and writes nothing.
3. **Delete and revert in this order:**
   1. Per-identity collections.
   2. idTags.
   3. Site revert.
   4. Chargers.
   5. Local files.

   **Why chargers go last:** their `simulated` flag is the selector for everything else. A crash
   partway through must leave the selector intact, so that simply re-running the script finishes
   the job. The script MUST be idempotent: a second run reports zeros everywhere and exits 0.
4. **Sanity check before reverting sites.** Every `simulated` site must have an `external_id`,
   since 11 only ever converted imported sites. If one doesn't, skip it and report it. Never revert
   something this file cannot explain.

## Testing

Add `tests/test_demo_fleet_remove.py`. Use the real-infrastructure convention and unique ids per
test, as in 11 §I.

1. **Setup.**
   - Run `seed_demo_fleet` on a small synthetic export.
   - For one mock charger, insert at least one document into **each** of the nine per-identity
     collections.
   - Do the same for a **control** charger with `simulated=False`.
   - Add a control operator site and a control idTag outside the `DEMO-FLEET` group.
2. **Run the removal, then assert:**
   - every mock document is gone
   - every control document is intact
   - the synthetic sites are back to `external_reference` with their other fields unchanged
   - the manifest and state files are deleted
   - the `transaction_id` counter's value is unchanged
3. **Second run:** a second run reports zeros.
4. **`--dry-run`:** changes nothing.
5. **Fleet-running guard:**
   - a state file with a fresh `fleet_alive_at` makes the script exit 1 and write nothing
   - with `--force`, it proceeds
6. **Derived model list:** the derived per-identity model set is exactly the nine listed above.
7. **Round trip:** seed, then remove, then seed again produces a working demo. The chargers are
   recreated as new documents with new keys in a new manifest.

## Acceptance criteria

1. With the fleet stopped, `python remove_demo_fleet.py --dry-run` against the demo from 11
   reports 195 chargers, 256 idTags and 134 sites to revert, and writes nothing.
2. With the fleet still running (within 30 s of its last refresh), the script refuses and exits 1.
3. The real run leaves 0 `ChargePoint{simulated: true}`, 0 documents in any per-identity
   collection for a `PS-…` identity, 0 `IdTag{parent_id_tag: "DEMO-FLEET"}` and 0
   `Site{source: "simulated"}`, and both local files are gone.
4. `GET /api/v1/sites` returns exactly what it returned before 11:
   - 136 sites
   - 134 `external_reference` sites, each `unknown` and with `charge_point_count` equal to its
     PlugShare station count
   - "Petrol Podgorica — Bulevar" and Hotel Budva unchanged

   On the map, the simulated pins are gone and the faint grey reference pins are back. Pin refresh
   picks this up without a reload.
5. CP001, its transactions and connector statuses, `charge_point_credentials.json`, and the
   `transaction_id` counter are unchanged.
6. A second run reports zeros and exits 0.
7. `python seed_demo_fleet.py` followed by `python run_demo_fleet.py` brings the demo back and
   meets 11's acceptance criteria 2 and 4 again.
8. `python -m pytest` passes.

## When you are done

Write the completion brief specified in `README.md#report-when-you-finish`. The use case: the demo
is over, and the public map must stop claiming live knowledge of chargers this system has never
talked to. One command takes it back to honest reference data, and nothing real is lost along the
way.
