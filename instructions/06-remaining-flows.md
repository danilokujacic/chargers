# 06 — Remaining flows

> **Status:** every section, A through J, is done. Read
> [06a-remaining-flows-progress.md](06a-remaining-flows-progress.md) for what each one built
> (an admin HTTP API on `main.py`, `operate.py`, a simulator idle/reactive mode, ...) and the
> traps found while building it, before extending any of them.

Everything beyond commissioning and the normal charge. Each section is independently
implementable; do them in the order listed, which is roughly by usefulness.

Depends on `04-normal-charge-flow.md` and `05-normal-charge-flow-tests.md`. Every flow here needs
tests following the same rules as 05, and every status change MUST go through
`connector_state_machine`.

## Feature profiles

OCPP 1.6 s3.3 groups messages into six profiles. **Core is mandatory; the rest are optional.**
Declare which profiles the Central System supports and do not claim one without implementing all
of its messages.

| Profile | Contains (abridged) |
|---|---|
| Core | Authorize, BootNotification, ChangeAvailability, ChangeConfiguration, ClearCache, DataTransfer, GetConfiguration, Heartbeat, MeterValues, StartTransaction, StatusNotification, StopTransaction, RemoteStart/StopTransaction, Reset, UnlockConnector |
| Firmware Management | GetDiagnostics, DiagnosticsStatusNotification, UpdateFirmware, FirmwareStatusNotification |
| Local Auth List Management | GetLocalListVersion, SendLocalList |
| Reservation | ReserveNow, CancelReservation |
| Smart Charging | SetChargingProfile, ClearChargingProfile, GetCompositeSchedule |
| Remote Trigger | TriggerMessage |

---

## A. Remote start and stop (Core) — do this first

Unlocks every app-driven use case: a driver taps "start" in a phone app, or an operator helps
someone stuck at a charger.

`RemoteStartTransaction.req(idTag, [connectorId], [chargingProfile])` →
`conf(status: Accepted | Rejected)`.

The `conf` status only says the charger **will attempt** to start. The transaction itself still
arrives as a normal `StartTransaction`. Do not treat `Accepted` as "charging started".

Behaviour hinges on a charger configuration key, per s5.11:

- `AuthorizeRemoteTxRequests = true` → the charger behaves as if a card were presented locally:
  it authorizes the idTag first (local list, cache, or `Authorize.req`) and only then starts.
- `AuthorizeRemoteTxRequests = false` → the charger starts immediately, and the Central System
  checks authorization when it processes the resulting `StartTransaction`.

Both must be supported on the simulator side so both can be tested.

Constraints:
- **Forbidden while the charger is `Pending`** (s4.2). Refuse before sending.
- Omitting `connectorId` means the charger chooses. `connectorId` 0 is not valid here.
- `RemoteStopTransaction.req(transactionId)` → the charger stops that transaction and moves the
  connector to Finishing (C6).

Add operator CLI commands (`register_charge_point.py` or a new `operate.py`) to trigger these
against a connected charger. That needs a registry of live connections — keep a module-level map
of identity → `MyChargePoint`, populated in `on_connect` and cleaned up on disconnect.

## B. Availability management (Core)

`ChangeAvailability.req(connectorId, type: Inoperative | Operative)` →
`conf(status: Accepted | Rejected | Scheduled)`.

From s5.2, each rule has a failure mode:
- **A transaction in progress means `Scheduled`**, not `Accepted`: the change happens after the
  transaction finishes. Implement the deferral, do not just report it.
- Requesting the status it is already in returns `Accepted`.
- `connectorId = 0` applies to "the Charge Point and all Connectors".
- After the change takes effect the charger sends `StatusNotification` with the new status
  (A8/C8/D8/E8/F8/G8 → Unavailable, H1 → Available).
- **Persistent across reboot:** "Connector set to Unavailable shall persist a reboot." Store it,
  and re-apply on the next boot.

## C. Reset and unlock (Core)

`Reset.req(type: Soft | Hard)` — soft finishes gracefully, hard is a power cycle. Either way the
charger reboots and the **commissioning flow runs again from BootNotification** (s4.2.1). Open
transactions at reset time need a policy: expect a `StopTransaction` after the reboot, and do not
assume the connector status survived.

`UnlockConnector.req(connectorId)` — for a cable physically stuck in a socket. Never valid for
connector 0. Does not stop a transaction by itself.

## D. Configuration (Core)

`GetConfiguration.req([key])` → `conf(configurationKey[], unknownKey[])`; omitting `key` returns
everything. `ChangeConfiguration.req(key, value)` → `conf(Accepted | Rejected | RebootRequired |
NotSupported)`.

Already used for `AuthorizationKey` in `03-commissioning-flow.md`. Generalise it, and store the
reported configuration per charger so the Central System knows what each supports.

Security rule from OCPP-J 1.6 s6.2.2: a charger "should not give back the authorization key in
response to a GetConfiguration request". If you implement the charger side in the simulator,
either omit `AuthorizationKey` or return an unrelated value.

`ClearCache.req` empties the charger's Authorization Cache. `DataTransfer` is the vendor-specific
escape hatch — implement as a logged no-op that answers `UnknownVendorId` unless a vendor is
registered.

## E. Reservation (Reservation profile)

`ReserveNow.req(connectorId, expiryDate, idTag, reservationId, [parentIdTag])` →
`conf(Accepted | Faulted | Occupied | Rejected | Unavailable)`.

From s3.11, a reservation ends when **any** of these happens:
- the reserved idTag is used on the reserved connector, or on any connector when `connectorId`
  was 0/unspecified;
- the `expiryDate` is reached;
- `CancelReservation.req(reservationId)` arrives.

State machine: only Available → Reserved (A7) and Faulted → Reserved (I7) are legal. Reserved
leaves to Available (G1), Preparing (G2), Unavailable (G8) or Faulted (G9) — **never directly to
Charging**. A reserved connector that a driver starts using goes Reserved → Preparing first.

Needs a `Reservation` document with an expiry sweep. `StartTransaction` carries an optional
`reservationId`; match it and release the reservation.

## F. Offline operation and local authorization (Local Auth List profile)

The charger must keep working when the network is down. s3.5: "In the event of unavailability of
the communications or even of the Central System, the Charge Point is designed to operate
stand-alone."

Two mechanisms, and they are different:
- **Authorization Cache** — the charger's own record of tags the Central System previously
  approved. Per s3.5.1 it holds "all the latest received identifiers (i.e. valid and NOT-valid)",
  is updated from every `IdTagInfo` received in `Authorize.conf`, `StartTransaction.conf` **and**
  `StopTransaction.conf`, and expires entries when their validity lapses.
- **Local Authorization List** — a whitelist the Central System pushes with
  `SendLocalList.req(listVersion, updateType: Full | Differential, localAuthorizationList[])`.
  `GetLocalListVersion` reads the current version so the Central System can tell whether an update
  is needed.

Two charger config keys govern behaviour: `LocalAuthorizeOffline` (authorize locally while
offline) and `LocalPreAuthorize` (start without waiting for the Central System even when online).

Central System consequences that must be handled — these are the ones that break naive
implementations:
- Queued transactions arrive **after reconnection with old timestamps**. Already required by
  `04-normal-charge-flow.md`; test it here in bulk.
- A transaction may be reported for a tag the Central System would now refuse. It happened; record
  it and let billing decide.
- `StopTransaction` may arrive without a matching `StartTransaction`.

## G. Fault handling (Core)

Any status → Faulted (A9/B9/C9/D9/E9/F9/G9/H9) with a `ChargePointErrorCode`:
`ConnectorLockFailure`, `EVCommunicationError`, `GroundFailure`, `HighTemperature`,
`InternalError`, `LocalListConflict`, `OtherError`, `OverCurrentFailure`, `OverVoltage`,
`PowerMeterFailure`, `PowerSwitchFailure`, `ReaderFailure`, `ResetFailure`, `UnderVoltage`,
`WeakSignal`, plus `NoError` for the normal case.

Recovery (I1–I8) returns to the **pre-fault status** — use
`ConnectorState.recover_from_fault()`, do not guess a target. Persist `pre_fault_status` so it
survives a Central System restart.

Store a fault history (connector, error code, `info`, `vendor_error_code`, entered/cleared
timestamps). A fault on connector 0 means the whole unit.

## H. Firmware and diagnostics (Firmware Management profile)

`UpdateFirmware.req(location, retrieveDate, [retries], [retryInterval])` — the charger fetches
the image itself from the URL, so the Central System must host or presign it. Progress arrives as
`FirmwareStatusNotification`: Downloaded, DownloadFailed, Downloading, Idle, InstallationFailed,
Installing, Installed.

A firmware install reboots the charger, so expect commissioning to run again.

`GetDiagnostics.req(location, …)` → the charger uploads a log archive (typically FTP) and reports
via `DiagnosticsStatusNotification`: Idle, Uploaded, UploadFailed, Uploading.

## I. Smart charging (Smart Charging profile)

The largest optional profile; leave it last. `SetChargingProfile.req(connectorId,
csChargingProfiles)` imposes limits over time; `ClearChargingProfile` removes them;
`GetCompositeSchedule` asks the charger for the **result** of combining every active profile with
its local limits.

Vocabulary from s2.2:
- **Charging Profile** — holds a Charging Schedule plus metadata.
- **Charging Schedule** — "a block of charging Power or Current limits", optionally with a start
  time and duration.
- **Composite Charging Schedule** — "as calculated by the Charge Point… the result of the
  calculation of all active schedules and possible local limits".
- **Control Pilot** — the physical signal carrying the limit to the car (IEC 61851-1).

A profile that cuts supply moves the connector Charging → SuspendedEVSE (C5), and lifting it
moves SuspendedEVSE → Charging (E3). Remember SuspendedEVSE outranks SuspendedEV.

## J. Remote trigger (Remote Trigger profile)

`TriggerMessage.req(requestedMessage, [connectorId])` →
`conf(Accepted | Rejected | NotImplemented)`, asking the charger to send a message now:
BootNotification, DiagnosticsStatusNotification, FirmwareStatusNotification, Heartbeat,
MeterValues, StatusNotification.

Small but disproportionately useful: it is the sanctioned way to get a status refresh from a
charger whose state you doubt, and the only way to make a `Pending` charger send something.

---

## Cross-cutting requirements

1. **Connector 0 is not a connector.** For Central System → charger messages it addresses the
   whole unit; for charger → Central System reports it is the main controller. It only ever holds
   Available, Unavailable or Faulted, and its status has no relationship to the connectors'.
2. **Live connection registry.** Every Central System → charger flow needs the open
   `MyChargePoint` for an identity. Build this once, in `main.py`, and reuse it. Handle the
   charger being offline with a clear error rather than a timeout.
3. **Nothing Central-System-initiated while `Pending`** except configuration reads and writes.
   `RemoteStartTransaction` and `RemoteStopTransaction` are explicitly forbidden.
4. **Nothing at all while `Rejected`.** The charger will not answer and the channel may be closed.
5. **Persist across restart** anything the spec calls persistent, at minimum connector
   availability and connector status.
6. Every new document type gets indexes and a `Settings.name`, following `models.py`.

## Acceptance criteria

Per flow: the messages exist, the status changes are validated through the state machine, the
spec-mandated statuses (`Scheduled`, `Occupied`, `NotSupported`, …) are actually returned in the
conditions that require them, state persists across a Central System restart, and there are tests
following `05-normal-charge-flow-tests.md`.

For the suite as a whole: `python -m pytest` stays green, and the normal charge flow from 04 still
passes unchanged after every addition.

## When you are done

Write the completion brief specified in [README.md](README.md#report-when-you-finish): what
is done, the use case, why it is built this way, and how you verified it. Written for someone
fluent in Python but new to EV charging, so define the charging terms you use.

For this task, each flow has its own use case; give one per flow you implemented, in the driver's
or operator's words.
