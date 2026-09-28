# 03 — Commissioning flow

Bring a brand-new charge point from "installed on site" to "accepted and ready to charge".

Depends on `02-charge-point-authentication.md`.

## The two commissioning routes

OCPP-J 1.6 s6.2.2 describes both. Support both; they differ only in where the key comes from.

**Route A — key installed before or during installation** (the spec calls this "the desired,
secure situation"). The operator generates the key, it reaches the installer or factory out of
band, and the charger arrives already holding it. The charger is `Accepted` on first boot.
`seed.py` implements this route.

**Route B — key set over OCPP.** Used when the operator does not control manufacture and sale.
Chargers leave the factory with a shared master key, or one derived from the identity. The
Central System replaces it with a unique key during onboarding. `register_charge_point.py`
implements the registration half of this route; the push is what you are building.

s6.2.2 recommends Route B be driven by the `Pending` registration status:

> A newly-connecting Charge Point will first get a Pending registration status on its first
> BootNotification.conf. The Central System will then set the Charge Point's unique authorization
> key with a ChangeConfiguration.req. Only when this ChangeConfiguration.req has been responded
> to with a ChangeConfiguration.conf with a status of Accepted, will the Central System respond to
> a boot notification with an Accepted registration status.

## The flow

```
  charger powers on
        │
        ├─ opens WebSocket to ws(s)://<host>/<identity>, subprotocol ocpp1.6,
        │  Authorization: Basic base64(identity ":" key)
        │       └─ rejected 401/403 ──► no OCPP at all; charger retries
        │
        ├─ BootNotification.req(chargePointVendor, chargePointModel, [7 optional fields])
        │
        │   Central System looks up the registry document and answers:
        │     Accepted  ─► commissioning done, go to StatusNotification
        │     Pending   ─► Route B: set the key, then accept
        │     Rejected  ─► charger must go silent for `interval` seconds
        │
        ├─ [Pending only] Central System sends ChangeConfiguration(AuthorizationKey, <new key>)
        │       ├─ conf Accepted        ─► store the new key hash, set status Accepted
        │       └─ conf Rejected/NotSupported ─► keep accepting the OLD key; stay Pending
        │
        ├─ [Pending only] charger re-sends BootNotification ─► now answered Accepted
        │
        ├─ StatusNotification(connectorId=0, Available)     ← main controller
        ├─ StatusNotification(connectorId=1..n, Available)  ← each connector
        └─ Heartbeat every `interval` seconds when otherwise idle
```

## Rules you must honour

From **OCPP 1.6 s4.2**, quoted because each has a failure mode:

1. > Between the physical power-on/reboot and the successful completion of a BootNotification,
   > where Central System returns Accepted or Pending, the Charge Point SHALL NOT send any other
   > request to the Central System. This includes cached messages that are still present in the
   > Charge Point from before.

2. On `Accepted`, `interval` is the **heartbeat interval**. On anything else it is "the minimum
   wait time before sending a next BootNotification request". Same field, two meanings. If the
   value is zero the charger picks its own wait.

3. `Rejected` means silence: the charger "SHALL NOT send any OCPP message to the Central System
   until the aforementioned retry interval has expired", must not answer Central System requests,
   and either side may close the channel.

4. `Pending` means the channel stays open: "the communication channel SHOULD NOT be closed by
   either the Charge Point or the Central System. The Central System MAY send request messages to
   retrieve information from the Charge Point or change its configuration." The charger answers
   but initiates nothing unless told to via `TriggerMessage`.

5. **While Pending, `RemoteStartTransaction.req` and `RemoteStopTransaction.req` are forbidden.**
   Enforce this server-side.

6. It is RECOMMENDED the charger sync its clock from `currentTime`. Send a correct UTC timestamp.

7. A reboot for any reason — remote reset, power cut, firmware update, crash — starts this flow
   again from the top.

## What to build

### `ChargePoint` document additions (`models.py`)

Nothing required for Route A; it already works. For Route B add:

- `key_rotation_pending: bool = False` — a `ChangeConfiguration` is in flight.
- `pending_authorization_key_hash: str | None = None` — hash of the **candidate** key.

Both exist so a failed or unanswered `ChangeConfiguration` cannot lock the charger out. The
rule from s6.2.2 is asymmetric and must be implemented exactly:

- `conf` status `Accepted` → promote the candidate hash to `authorization_key_hash`, clear the
  candidate, set `registration_status = accepted`.
- `conf` status `Rejected` or `NotSupported` → **discard the candidate and keep the old key
  working.** "the Central System SHALL keep accepting the old credentials." Leave the charger
  `Pending`. The spec permits treating it differently in other ways, e.g. "by not accepting the
  Charge Point's boot notifications", but never by locking it out.
- No response / timeout → same as Rejected. Never leave both keys invalid.

### `commissioning.py`

```python
async def onboard(charge_point, record) -> RegistrationStatus:
    """Drive Route B for a Pending charger: push a fresh key, then accept it.

    charge_point is the live MyChargePoint (used to send the call); record is its registry
    document. Returns the status to report in the NEXT BootNotification.conf.
    """

async def boot_status(record) -> RegistrationStatus:
    """The status to answer a BootNotification with, given the registry document."""
```

Keep the decision logic here rather than in the `on_boot` handler, so it is testable
independently of a live connection.

### `main.py` changes

- `on_boot` stays responsible for persisting the reported fields (it already does, via
  `record_boot`). It MUST answer with `boot_status(record)`.
- When the answer is `Pending`, schedule `onboard()` to run **after** the
  `BootNotification.conf` has been sent. Do not `await` it inside the handler: the conf must
  reach the charger before the Central System sends `ChangeConfiguration`. Use
  `asyncio.create_task` and log failures.
- Reject `RemoteStartTransaction` / `RemoteStopTransaction` while the charger is `Pending`.

### `simulate_charge_point.py` changes

Add the charger side so the flow can be exercised end to end:

- Handle `ChangeConfiguration`. For key `AuthorizationKey`, validate the value is 40 hex
  characters, store it as the charger's new key, and answer `Accepted`. For unknown keys answer
  `NotSupported`.
- Add `--reject-key-change` to answer `Rejected` instead, so the failure branch is testable.
- On receiving `Pending`, re-send `BootNotification` once rather than aborting. Today the
  simulator marks a non-`Accepted` boot as FAIL and continues; that is correct for a plain run
  but cannot exercise Route B.
- Persist the rotated key back to the credentials file, so the next run authenticates.

## Acceptance criteria

1. Route A: a charger seeded by `seed.py` boots and is answered `Accepted` first time, with
   `interval` equal to the heartbeat interval.
2. Route B happy path: a charger registered `Pending` is answered `Pending`, receives
   `ChangeConfiguration(AuthorizationKey, …)`, answers `Accepted`, and its **next**
   `BootNotification` is answered `Accepted`. The new key authenticates a fresh connection; the
   old key does not.
3. Route B refusal: with `--reject-key-change`, the charger stays `Pending` and **the original
   key still authenticates**. Assert this explicitly — it is the dangerous branch.
4. Route B with no answer at all (kill the simulator mid-flow): the original key still
   authenticates.
5. `Rejected`: the `BootNotification.conf` carries a non-zero `interval` and the server sends no
   further requests on that connection.
6. `RemoteStartTransaction` is refused while `Pending`.
7. After `Accepted`, `StatusNotification` for connector 0 and each connector is accepted and
   persisted, and connector 0 only ever reports Available, Unavailable or Faulted
   (see `01-connector-state-machine.md`).
8. A second boot from an already-commissioned charger is answered `Accepted` without any
   `ChangeConfiguration` being sent.

## When you are done

Write the completion brief specified in [README.md](README.md#report-when-you-finish): what
is done, the use case, why it is built this way, and how you verified it. Written for someone
fluent in Python but new to EV charging, so define the charging terms you use.

For this task, the use case is an electrician finishing an installation on site and the charger
having to become usable without anyone typing a key into it by hand.
