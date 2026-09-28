# 01 — Connector state machine

Build `connector_state_machine.py`: a pure module encoding the connector status transitions
defined in **OCPP 1.6 s4.9**.

## Why this is its own module

The nine statuses and their legal transitions are normative and fiddly. Putting them in one
dependency-free module means the rules can be unit-tested without MongoDB, a WebSocket server or
a charger, and every other flow can consult one source of truth instead of re-deriving it.

## Hard constraints

- The module MUST NOT import `models`, `beanie`, `pymongo` or `websockets`. Its only third-party
  import is `ocpp.v16.enums`.
- It MUST NOT perform I/O and MUST NOT be async.
- States MUST be `ocpp.v16.enums.ChargePointStatus` members. Do not define a parallel enum.

## The states

`ChargePointStatus` already provides all nine: `available`, `preparing`, `charging`,
`suspended_ev`, `suspended_evse`, `finishing`, `reserved`, `unavailable`, `faulted`.

## The transition table

53 legal transitions. The code (`A2`, `B3`, …) is the spec's own label; keep it, it makes logs
traceable to the document. Descriptions are quoted from s4.9 — transcribe them, do not reword.

| Code | From | To | Event |
|---|---|---|---|
| A2 | Available | Preparing | Usage is initiated (e.g. insert plug, bay occupancy detection, present idTag, push start button, receipt of a RemoteStartTransaction.req) |
| A3 | Available | Charging | Can be possible in a Charge Point without an authorization means |
| A4 | Available | SuspendedEV | Similar to A3 but the EV does not start charging |
| A5 | Available | SuspendedEVSE | Similar to A3 but the EVSE does not allow charging |
| A7 | Available | Reserved | A Reserve Now message is received that reserves the connector |
| A8 | Available | Unavailable | A Change Availability message is received that sets the connector to Unavailable |
| A9 | Available | Faulted | A fault is detected that prevents further charging operations |
| B1 | Preparing | Available | Intended usage is ended (e.g. plug removed, bay no longer occupied, second presentation of idTag, time out (configured by the configuration key: ConnectionTimeOut) on expected user action) |
| B3 | Preparing | Charging | All prerequisites for charging are met and charging process starts |
| B4 | Preparing | SuspendedEV | All prerequisites for charging are met but EV does not start charging |
| B5 | Preparing | SuspendedEVSE | All prerequisites for charging are met but EVSE does not allow charging |
| B6 | Preparing | Finishing | Timed out. Usage was initiated (e.g. insert plug, bay occupancy detection), but idTag not presented within timeout. |
| B9 | Preparing | Faulted | A fault is detected that prevents further charging operations |
| C1 | Charging | Available | Charging session ends while no user action is required (e.g. fixed cable was removed on EV side) |
| C4 | Charging | SuspendedEV | Charging stops upon EV request (e.g. S2 is opened) |
| C5 | Charging | SuspendedEVSE | Charging stops upon EVSE request (e.g. smart charging restriction, transaction is invalidated by the AuthorizationStatus in a StartTransaction.conf) |
| C6 | Charging | Finishing | Transaction is stopped by user or a Remote Stop Transaction message and further user action is required (e.g. remove cable, leave parking bay) |
| C8 | Charging | Unavailable | Charging session ends, no user action is required and the connector is scheduled to become Unavailable |
| C9 | Charging | Faulted | A fault is detected that prevents further charging operations |
| D1 | SuspendedEV | Available | Charging session ends while no user action is required |
| D3 | SuspendedEV | Charging | Charging resumes upon request of the EV (e.g. S2 is closed) |
| D5 | SuspendedEV | SuspendedEVSE | Charging is suspended by EVSE (e.g. due to a smart charging restriction) |
| D6 | SuspendedEV | Finishing | Transaction is stopped and further user action is required |
| D8 | SuspendedEV | Unavailable | Charging session ends, no user action is required and the connector is scheduled to become Unavailable |
| D9 | SuspendedEV | Faulted | A fault is detected that prevents further charging operations |
| E1 | SuspendedEVSE | Available | Charging session ends while no user action is required |
| E3 | SuspendedEVSE | Charging | Charging resumes because the EVSE restriction is lifted |
| E4 | SuspendedEVSE | SuspendedEV | The EVSE restriction is lifted but the EV does not start charging |
| E6 | SuspendedEVSE | Finishing | Transaction is stopped and further user action is required |
| E8 | SuspendedEVSE | Unavailable | Charging session ends, no user action is required and the connector is scheduled to become Unavailable |
| E9 | SuspendedEVSE | Faulted | A fault is detected that prevents further charging operations |
| F1 | Finishing | Available | All user actions completed |
| F2 | Finishing | Preparing | User restart charging session (e.g. reconnects cable, presents idTag again), thereby creating a new Transaction |
| F8 | Finishing | Unavailable | All user actions completed and the connector is scheduled to become Unavailable |
| F9 | Finishing | Faulted | A fault is detected that prevents further charging operations |
| G1 | Reserved | Available | Reservation expires or a Cancel Reservation message is received |
| G2 | Reserved | Preparing | Reservation identity is presented |
| G8 | Reserved | Unavailable | Reservation expires or a Cancel Reservation message is received and the connector is scheduled to become Unavailable |
| G9 | Reserved | Faulted | A fault is detected that prevents further charging operations |
| H1 | Unavailable | Available | Connector is set Available by a Change Availability message |
| H2 | Unavailable | Preparing | Connector is set Available after a user had interacted with the Charge Point |
| H3 | Unavailable | Charging | Connector is set Available and no user action is required to start charging |
| H4 | Unavailable | SuspendedEV | Similar to H3 but the EV does not start charging |
| H5 | Unavailable | SuspendedEVSE | Similar to H3 but the EVSE does not allow charging |
| H9 | Unavailable | Faulted | A fault is detected that prevents further charging operations |
| I1 | Faulted | Available | Fault is resolved and status returns to the pre-fault state |
| I2 | Faulted | Preparing | Fault is resolved and status returns to the pre-fault state |
| I3 | Faulted | Charging | Fault is resolved and status returns to the pre-fault state |
| I4 | Faulted | SuspendedEV | Fault is resolved and status returns to the pre-fault state |
| I5 | Faulted | SuspendedEVSE | Fault is resolved and status returns to the pre-fault state |
| I6 | Faulted | Finishing | Fault is resolved and status returns to the pre-fault state |
| I7 | Faulted | Reserved | Fault is resolved and status returns to the pre-fault state |
| I8 | Faulted | Unavailable | Fault is resolved and status returns to the pre-fault state |

## Rules the table alone does not express

1. **ConnectorId 0 is restricted.** s4.9: "For ConnectorId 0, only a limited set is applicable,
   namely: Available, Unavailable and Faulted." A transition legal for a connector may be
   illegal for the main controller. Expose this as a separate check, not a duplicated table.

2. **ConnectorId 0 is independent.** "The status of ConnectorId 0 has no direct connection to the
   status of the individual Connectors (>0)." Never derive one from the other.

3. **SuspendedEVSE wins.** "If charging is suspended both by the EV and the EVSE, status
   SuspendedEVSE SHALL have precedence over status SuspendedEV."

4. **Self-transitions are not in the table.** The diagonal is empty. A repeated
   `StatusNotification` with the unchanged status is normal charger behaviour and MUST NOT be
   treated as an error. Model it as a third outcome, distinct from "legal transition" and
   "illegal transition", so callers can no-op on it.

5. **Recovery from Faulted returns to the pre-fault status.** The table permits Faulted → any
   state, but the spec's intent is narrower: it goes back to where it was. Track the pre-fault
   status so a caller can ask for the correct recovery target rather than picking one.

## Required API

```python
class IllegalTransition(ValueError):
    """Raised when a status change is not permitted by OCPP 1.6 s4.9."""


@dataclass(frozen=True)
class Transition:
    code: str          # the spec's label, e.g. "B3"
    source: ChargePointStatus
    target: ChargePointStatus
    event: str         # the spec's description


# All 53, keyed by source then target.
TRANSITIONS: dict[ChargePointStatus, dict[ChargePointStatus, Transition]]

# s4.9: the only statuses ConnectorId 0 may report.
MAIN_CONTROLLER_STATUSES: frozenset[ChargePointStatus]


def transitions_from(status) -> dict[ChargePointStatus, Transition]:
    """Every status reachable in one step, keyed by target."""

def find_transition(source, target) -> Transition | None:
    """The transition between two statuses, or None when there is none."""

def is_legal(source, target, connector_id=1) -> bool:
    """True when this status change is permitted. connector_id=0 applies the s4.9 limit."""

def check_transition(source, target, connector_id=1) -> Transition:
    """Return the transition, or raise IllegalTransition with a message naming both statuses."""

def resolve_suspension(ev_suspended, evse_suspended) -> ChargePointStatus | None:
    """Which suspended status applies, honouring the SuspendedEVSE precedence rule.

    Returns None when neither side has suspended.
    """
```

And a small holder for a connector's live status:

```python
class ConnectorState:
    """The current status of one connector, and the transitions it is allowed to make."""

    def __init__(self, connector_id, status=ChargePointStatus.available): ...

    @property
    def status(self) -> ChargePointStatus: ...

    @property
    def pre_fault_status(self) -> ChargePointStatus | None:
        """The status held before entering Faulted, for recovery. None when never faulted."""

    def can_change_to(self, target) -> bool: ...

    def change_to(self, target) -> Transition | None:
        """Apply a status change.

        Returns the Transition applied, or None when target equals the current status (a
        repeated notification, which is not an error). Raises IllegalTransition otherwise.
        Entering Faulted records the pre-fault status; leaving it clears the record.
        """

    def recover_from_fault(self) -> Transition:
        """Return to the pre-fault status. Raises IllegalTransition when not Faulted."""
```

`connector_id` is stored so error messages can name the connector, and so `change_to` applies
the ConnectorId 0 restriction automatically.

## Acceptance criteria

Write `test_connector_state_machine.py` alongside it (a standalone script printing PASS/FAIL per
check and exiting non-zero on failure — no MongoDB needed) proving:

1. `sum(len(t) for t in TRANSITIONS.values()) == 53`.
2. Per-source counts are exactly: Available 7, Preparing 6, Charging 6, SuspendedEV 6,
   SuspendedEVSE 6, Finishing 4, Reserved 4, Unavailable 6, Faulted 8.
3. Per-target counts are exactly: Available 8, Preparing 5, Charging 6, SuspendedEV 6,
   SuspendedEVSE 6, Finishing 5, Reserved 2, Unavailable 7, Faulted 8.
4. Every `Transition.code` is unique, and every code from the table above is present.
5. No transition has `source == target`.
6. Every status except Available can reach Available; every status except Faulted can reach
   Faulted.
7. Only Available and Faulted can reach Reserved.
8. `Charging → Preparing` is illegal; `Finishing → Charging` is illegal;
   `Preparing → Unavailable` is illegal; `Reserved → Charging` is illegal.
9. `is_legal(available, preparing, connector_id=0)` is False, while
   `is_legal(available, unavailable, connector_id=0)` is True.
10. `check_transition` raises `IllegalTransition` for an illegal pair, and the message contains
    both status names.
11. `ConnectorState.change_to` returns `None` for a repeat of the current status and leaves the
    status unchanged.
12. Faulting from Charging then `recover_from_fault()` returns to Charging; calling
    `recover_from_fault()` when not Faulted raises.
13. `resolve_suspension(True, True)` is `suspended_evse`; `(True, False)` is `suspended_ev`;
    `(False, True)` is `suspended_evse`; `(False, False)` is `None`.

Do not wire this module into `main.py` in this task. `04-normal-charge-flow.md` does that.

## When you are done

Write the completion brief specified in [README.md](README.md#report-when-you-finish): what
is done, the use case, why it is built this way, and how you verified it. Written for someone
fluent in Python but new to EV charging, so define the charging terms you use.

For this task, the use case is an operator looking at a dashboard and needing to trust what a
connector says it is doing -- and the Central System noticing when a charger reports something
impossible.
