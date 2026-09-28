# 04 — Normal charge flow

The everyday path: a driver presents an RFID card, charges, and leaves. This is the flow that
produces billable transactions, so correctness here matters more than anywhere else.

Depends on `01-connector-state-machine.md` and `03-commissioning-flow.md`.

## The flow

```
  connector: Available
        │
        │  driver presents card / plugs in
        ├─ Authorize.req(idTag) ──────────► idTagInfo{status, [expiryDate], [parentIdTag]}
        │       └─ not Accepted ─► refuse; connector returns to Available (B1)
        │
        ├─ StatusNotification(connectorId=n, Preparing)                        A2
        │
        ├─ StartTransaction.req(connectorId, idTag, meterStart, timestamp,
        │                       [reservationId])
        │       └──────────────► StartTransaction.conf(transactionId, idTagInfo)
        │
        ├─ StatusNotification(connectorId=n, Charging)                         B3
        │
        ├─ MeterValues.req(connectorId, transactionId, meterValue[])   ...repeating
        │
        │  driver presents card again / presses stop
        ├─ StopTransaction.req(transactionId, meterStop, timestamp,
        │                      [idTag], [reason], [transactionData])
        │       └──────────────► StopTransaction.conf([idTagInfo])
        │
        ├─ StatusNotification(connectorId=n, Finishing)                        C6
        │  driver unplugs
        └─ StatusNotification(connectorId=n, Available)                        F1
```

Transition codes refer to the table in `01-connector-state-machine.md`. Every status change in
this flow MUST be validated through that module.

## Concepts to keep straight

- **Charging Session** (s2.2) starts at "first interaction with user or EV" — card swipe, cable
  insertion, bay sensor. Wider than a transaction.
- **Transaction** starts "when all relevant preconditions (e.g. authorization, plug inserted) are
  met". This is the billable unit and the thing with a `transactionId`.
- **Energy Transfer Period** is when the car actually draws power. "Multiple Energy Transfer
  Periods are possible during a Transaction" — a car that pauses and resumes stays one
  transaction, moving Charging → SuspendedEV → Charging.

## What to build

### 1. `IdTag` document (`models.py`)

This is the **driver's** credential — the thing `Authorize.req` carries. Distinct from the charge
point's authorization key.

| Field | Notes |
|---|---|
| `id_tag` | unique index. CiString20. s3.9: MAY contain any data — usually an RFID UID as 8 or 14 hex chars, but may be a virtual app token. **Do not validate its shape.** |
| `status` | `ocpp.v16.enums.AuthorizationStatus`: accepted / blocked / expired / invalid / concurrent_tx |
| `parent_id_tag` | optional group identifier, see s3.10 |
| `expiry_date` | optional `datetime`; past means expired |
| `created_at`, `updated_at` | |

Add a method that answers an authorization request:

```python
async def authorize(cls, id_tag) -> IdTagInfo:
    """Resolve an idTag to the IdTagInfo that goes in Authorize.conf.

    Unknown tags return status Invalid. A tag whose expiry_date has passed returns Expired
    regardless of its stored status. A tag already running a transaction elsewhere returns
    ConcurrentTx.
    """
```

s3.10, for `parent_id_tag`: two idTags are in the same group when their parent matches, and any
token in the group may stop a transaction another started. Also: "the ParentId value SHOULD NOT
be used for comparison against a presented Token value" — never treat a parent id as an idTag.

### 2. `Transaction` document (`models.py`)

| Field | Notes |
|---|---|
| `transaction_id` | **integer**, unique index. See allocation below. |
| `charge_point_identity` | indexed |
| `connector_id` | integer ≥ 1. A transaction never belongs to connector 0. |
| `id_tag` | the tag that started it |
| `stopped_by_id_tag` | the tag that stopped it, may differ within a parent group |
| `meter_start`, `meter_stop` | Wh integers; `meter_stop` null while running |
| `started_at`, `stopped_at` | timestamps **as reported by the charger**, not server time |
| `stop_reason` | `ocpp.v16.enums.Reason`, optional |
| `meter_values` | list of readings collected during the transaction |
| `is_open` | or derive from `stopped_at is None`; index whichever you choose |

Indexes: unique on `transaction_id`; compound on `(charge_point_identity, connector_id, is_open)`
to find the open transaction for a connector.

**`transaction_id` allocation.** It MUST be a positive integer, unique across the Central System,
and stable — a charger sends it back in every `MeterValues` and in `StopTransaction`. Do not use
`ObjectId`, do not use a random number, and do not use a count of documents. Use an atomic
counter document:

```python
await db["counters"].find_one_and_update(
    {"_id": "transaction_id"}, {"$inc": {"value": 1}},
    upsert=True, return_document=ReturnDocument.AFTER,
)
```

This must be atomic because two chargers can start transactions in the same millisecond.

### 3. `ConnectorStatus` persistence (`models.py`)

The state machine from 01 is in-memory and pure. Status must survive a restart, so persist it:

| Field | Notes |
|---|---|
| `charge_point_identity`, `connector_id` | compound unique index |
| `status` | `ChargePointStatus` |
| `error_code` | `ChargePointErrorCode`, `no_error` normally |
| `info`, `vendor_id`, `vendor_error_code` | optional, straight from `StatusNotification` |
| `pre_fault_status` | for recovery, mirrors `ConnectorState.pre_fault_status` |
| `updated_at` | |

Load these into `ConnectorState` objects when a charger connects; write back on every accepted
status change.

### 4. Handler changes (`main.py`)

**`on_authorize`** — replace the stub. Look the tag up and return the real `IdTagInfo`. Never
return `Accepted` for an unknown tag.

**`on_status`** — replace the stub. Feed the change through the state machine:
- `change_to()` returning `None` (repeat of current status) → accept, no write beyond `updated_at`.
- `IllegalTransition` → **log it clearly and still persist the reported status.** The charger is
  the authority on its own hardware; the Central System records reality and flags the anomaly. Do
  not return a CALLError, and do not refuse the message: `StatusNotification.conf` has no status
  field, so there is no way to tell the charger it was wrong.
- Apply the ConnectorId 0 restriction (Available / Unavailable / Faulted only).

**`on_start`** — replace the hardcoded `transaction_id=1`:
1. Authorize the idTag. If not `Accepted`, still allocate a transaction (the charger has already
   started charging) but return the refusing `idTagInfo`; per s4.9 code C5 the charger then moves
   to SuspendedEVSE because "transaction is invalidated by the AuthorizationStatus in a
   StartTransaction.conf".
2. Allocate `transaction_id` atomically.
3. Create the `Transaction` with the charger's `timestamp` and `meterStart`.
4. Return `transaction_id` and `idTagInfo`.

**`on_meter_values`** — append readings to the open transaction when `transactionId` is present.
A `MeterValues` with no `transactionId` is a standalone clock-driven reading, not transaction
data; store it against the connector, not the transaction.

**`on_stop`** — close the transaction: set `meter_stop`, `stopped_at`, `stop_reason`,
`stopped_by_id_tag`. Reject nothing, but log loudly if `transaction_id` is unknown or already
closed — that is a duplicate delivery, which happens after a charger reconnects. **Make it
idempotent:** re-delivering the same `StopTransaction` must not corrupt the stored record.
Per s4.10 the response `idTagInfo` is optional and is only meaningful when the request carried an
`idTag`.

### 5. Offline-delivered transactions

A charger that was offline delivers `StartTransaction` and `StopTransaction` late, with
timestamps hours in the past. Therefore:

- Always trust the charger's `timestamp` fields for billing; use server time only for audit
  columns like `updated_at`.
- Never reject a transaction because its timestamp is old.
- A `StopTransaction` may arrive for a transaction whose `StartTransaction` you have not seen.
  Handle it: create the record from what you have and flag it as incomplete.

## Acceptance criteria

1. The full flow above completes with real documents: one `Transaction`, correct `meter_start` /
   `meter_stop`, correct timestamps taken from the charger's messages.
2. `transaction_id` values from two concurrent chargers are distinct and increasing; no duplicates
   under 50 interleaved starts.
3. An unknown idTag gets `Invalid` from `Authorize`; a blocked one gets `Blocked`; one past its
   `expiry_date` gets `Expired` even if stored as accepted.
4. A tag in the same parent group as the starting tag can stop the transaction; an unrelated tag
   cannot.
5. Every status change is validated through `connector_state_machine`; an illegal one is logged
   and still persisted.
6. `StatusNotification(connectorId=0, Charging)` is logged as illegal, per the s4.9 restriction.
7. Charging → SuspendedEV → Charging leaves exactly one transaction open and does not create a
   second.
8. Re-delivering an identical `StopTransaction` leaves the stored transaction unchanged.
9. A `StopTransaction` for an unseen `transaction_id` is recorded rather than dropped.
10. Restarting the Central System preserves connector statuses and open transactions.
11. `register_charge_point.py` and `seed.py` still work unchanged.

## When you are done

Write the completion brief specified in [README.md](README.md#report-when-you-finish): what
is done, the use case, why it is built this way, and how you verified it. Written for someone
fluent in Python but new to EV charging, so define the charging terms you use.

For this task, the use case is a driver arriving, tapping a card, charging, and driving away --
and the operator being able to bill for exactly the energy delivered.
