"""Connector status transitions, as defined in OCPP 1.6 s4.9 (Status Notification).

A pure, dependency-free module: no I/O, no async, no imports beyond the ocpp enums. This lets the
53 legal transitions be tested without a MongoDB, a WebSocket server or a charger, and lets every
other flow (commissioning, normal charging, faults, reservations) consult one source of truth
instead of re-deriving the rules.

Terminology, for readers new to OCPP: a Charge Point (the physical charging unit) has one or more
Connectors (independently operated outlets). Each Connector reports its own status -- Available,
Charging, Faulted, etc. ConnectorId 0 is special: it does not address a socket but the Charge
Point's main controller, and it is restricted to a smaller set of statuses (see
MAIN_CONTROLLER_STATUSES below).
"""

from dataclasses import dataclass

from ocpp.v16.enums import ChargePointStatus

CPS = ChargePointStatus


class IllegalTransition(ValueError):
    """Raised when a status change is not permitted by OCPP 1.6 s4.9."""


@dataclass(frozen=True)
class Transition:
    """One legal status change, as listed in the OCPP 1.6 s4.9 transition table."""

    code: str  # the spec's own label, e.g. "B3" -- kept so logs stay traceable to the document
    source: ChargePointStatus
    target: ChargePointStatus
    event: str  # the spec's description of what causes this change, quoted verbatim


# The 53 transitions OCPP 1.6 s4.9 permits. Grouped by source status, in the spec's own order.
# Descriptions are transcribed from the spec, not reworded, so they can be traced back to it.
_TRANSITION_LIST = [
    # Available
    Transition(
        "A2",
        CPS.available,
        CPS.preparing,
        "Usage is initiated (e.g. insert plug, bay occupancy detection, present idTag, push "
        "start button, receipt of a RemoteStartTransaction.req)",
    ),
    Transition(
        "A3",
        CPS.available,
        CPS.charging,
        "Can be possible in a Charge Point without an authorization means",
    ),
    Transition(
        "A4",
        CPS.available,
        CPS.suspended_ev,
        "Similar to A3 but the EV does not start charging",
    ),
    Transition(
        "A5",
        CPS.available,
        CPS.suspended_evse,
        "Similar to A3 but the EVSE does not allow charging",
    ),
    Transition(
        "A7",
        CPS.available,
        CPS.reserved,
        "A Reserve Now message is received that reserves the connector",
    ),
    Transition(
        "A8",
        CPS.available,
        CPS.unavailable,
        "A Change Availability message is received that sets the connector to Unavailable",
    ),
    Transition(
        "A9",
        CPS.available,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # Preparing
    Transition(
        "B1",
        CPS.preparing,
        CPS.available,
        "Intended usage is ended (e.g. plug removed, bay no longer occupied, second "
        "presentation of idTag, time out (configured by the configuration key: "
        "ConnectionTimeOut) on expected user action)",
    ),
    Transition(
        "B3",
        CPS.preparing,
        CPS.charging,
        "All prerequisites for charging are met and charging process starts",
    ),
    Transition(
        "B4",
        CPS.preparing,
        CPS.suspended_ev,
        "All prerequisites for charging are met but EV does not start charging",
    ),
    Transition(
        "B5",
        CPS.preparing,
        CPS.suspended_evse,
        "All prerequisites for charging are met but EVSE does not allow charging",
    ),
    Transition(
        "B6",
        CPS.preparing,
        CPS.finishing,
        "Timed out. Usage was initiated (e.g. insert plug, bay occupancy detection), but "
        "idTag not presented within timeout.",
    ),
    Transition(
        "B9",
        CPS.preparing,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # Charging
    Transition(
        "C1",
        CPS.charging,
        CPS.available,
        "Charging session ends while no user action is required (e.g. fixed cable was "
        "removed on EV side)",
    ),
    Transition(
        "C4",
        CPS.charging,
        CPS.suspended_ev,
        "Charging stops upon EV request (e.g. S2 is opened)",
    ),
    Transition(
        "C5",
        CPS.charging,
        CPS.suspended_evse,
        "Charging stops upon EVSE request (e.g. smart charging restriction, transaction is "
        "invalidated by the AuthorizationStatus in a StartTransaction.conf)",
    ),
    Transition(
        "C6",
        CPS.charging,
        CPS.finishing,
        "Transaction is stopped by user or a Remote Stop Transaction message and further "
        "user action is required (e.g. remove cable, leave parking bay)",
    ),
    Transition(
        "C8",
        CPS.charging,
        CPS.unavailable,
        "Charging session ends, no user action is required and the connector is scheduled "
        "to become Unavailable",
    ),
    Transition(
        "C9",
        CPS.charging,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # SuspendedEV
    Transition(
        "D1",
        CPS.suspended_ev,
        CPS.available,
        "Charging session ends while no user action is required",
    ),
    Transition(
        "D3",
        CPS.suspended_ev,
        CPS.charging,
        "Charging resumes upon request of the EV (e.g. S2 is closed)",
    ),
    Transition(
        "D5",
        CPS.suspended_ev,
        CPS.suspended_evse,
        "Charging is suspended by EVSE (e.g. due to a smart charging restriction)",
    ),
    Transition(
        "D6",
        CPS.suspended_ev,
        CPS.finishing,
        "Transaction is stopped and further user action is required",
    ),
    Transition(
        "D8",
        CPS.suspended_ev,
        CPS.unavailable,
        "Charging session ends, no user action is required and the connector is scheduled "
        "to become Unavailable",
    ),
    Transition(
        "D9",
        CPS.suspended_ev,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # SuspendedEVSE
    Transition(
        "E1",
        CPS.suspended_evse,
        CPS.available,
        "Charging session ends while no user action is required",
    ),
    Transition(
        "E3",
        CPS.suspended_evse,
        CPS.charging,
        "Charging resumes because the EVSE restriction is lifted",
    ),
    Transition(
        "E4",
        CPS.suspended_evse,
        CPS.suspended_ev,
        "The EVSE restriction is lifted but the EV does not start charging",
    ),
    Transition(
        "E6",
        CPS.suspended_evse,
        CPS.finishing,
        "Transaction is stopped and further user action is required",
    ),
    Transition(
        "E8",
        CPS.suspended_evse,
        CPS.unavailable,
        "Charging session ends, no user action is required and the connector is scheduled "
        "to become Unavailable",
    ),
    Transition(
        "E9",
        CPS.suspended_evse,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # Finishing
    Transition("F1", CPS.finishing, CPS.available, "All user actions completed"),
    Transition(
        "F2",
        CPS.finishing,
        CPS.preparing,
        "User restart charging session (e.g. reconnects cable, presents idTag again), "
        "thereby creating a new Transaction",
    ),
    Transition(
        "F8",
        CPS.finishing,
        CPS.unavailable,
        "All user actions completed and the connector is scheduled to become Unavailable",
    ),
    Transition(
        "F9",
        CPS.finishing,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # Reserved
    Transition(
        "G1",
        CPS.reserved,
        CPS.available,
        "Reservation expires or a Cancel Reservation message is received",
    ),
    Transition(
        "G2", CPS.reserved, CPS.preparing, "Reservation identity is presented"
    ),
    Transition(
        "G8",
        CPS.reserved,
        CPS.unavailable,
        "Reservation expires or a Cancel Reservation message is received and the connector "
        "is scheduled to become Unavailable",
    ),
    Transition(
        "G9",
        CPS.reserved,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # Unavailable
    Transition(
        "H1",
        CPS.unavailable,
        CPS.available,
        "Connector is set Available by a Change Availability message",
    ),
    Transition(
        "H2",
        CPS.unavailable,
        CPS.preparing,
        "Connector is set Available after a user had interacted with the Charge Point",
    ),
    Transition(
        "H3",
        CPS.unavailable,
        CPS.charging,
        "Connector is set Available and no user action is required to start charging",
    ),
    Transition(
        "H4",
        CPS.unavailable,
        CPS.suspended_ev,
        "Similar to H3 but the EV does not start charging",
    ),
    Transition(
        "H5",
        CPS.unavailable,
        CPS.suspended_evse,
        "Similar to H3 but the EVSE does not allow charging",
    ),
    Transition(
        "H9",
        CPS.unavailable,
        CPS.faulted,
        "A fault is detected that prevents further charging operations",
    ),
    # Faulted -- recovery returns to the pre-fault status (see ConnectorState.recover_from_fault)
    Transition(
        "I1",
        CPS.faulted,
        CPS.available,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I2",
        CPS.faulted,
        CPS.preparing,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I3",
        CPS.faulted,
        CPS.charging,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I4",
        CPS.faulted,
        CPS.suspended_ev,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I5",
        CPS.faulted,
        CPS.suspended_evse,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I6",
        CPS.faulted,
        CPS.finishing,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I7",
        CPS.faulted,
        CPS.reserved,
        "Fault is resolved and status returns to the pre-fault state",
    ),
    Transition(
        "I8",
        CPS.faulted,
        CPS.unavailable,
        "Fault is resolved and status returns to the pre-fault state",
    ),
]

# All 53, keyed by source then target, for O(1) lookup.
TRANSITIONS: dict[ChargePointStatus, dict[ChargePointStatus, Transition]] = {}
for _t in _TRANSITION_LIST:
    TRANSITIONS.setdefault(_t.source, {})[_t.target] = _t
del _t

# OCPP 1.6 s4.9: "For ConnectorId 0, only a limited set is applicable, namely: Available,
# Unavailable and Faulted." ConnectorId 0 addresses the Charge Point's main controller, not a
# socket, and "has no direct connection to the status of the individual Connectors (>0)".
MAIN_CONTROLLER_STATUSES: frozenset = frozenset({CPS.available, CPS.unavailable, CPS.faulted})


def transitions_from(status):
    """Every status reachable in one step from `status`, keyed by target."""
    return dict(TRANSITIONS.get(status, {}))


def find_transition(source, target):
    """The Transition from `source` to `target`, or None when there is none."""
    return TRANSITIONS.get(source, {}).get(target)


def is_legal(source, target, connector_id=1):
    """True when this status change is permitted.

    connector_id=0 applies the s4.9 restriction to Available/Unavailable/Faulted only, since
    ConnectorId 0 addresses the Charge Point's main controller rather than a socket.
    """
    if connector_id == 0 and target not in MAIN_CONTROLLER_STATUSES:
        return False
    return find_transition(source, target) is not None


def check_transition(source, target, connector_id=1):
    """Return the Transition for source -> target, or raise IllegalTransition.

    Raises IllegalTransition when connector_id=0 is asked to report a status outside
    MAIN_CONTROLLER_STATUSES, or when no such transition exists in the s4.9 table.
    """
    if connector_id == 0 and target not in MAIN_CONTROLLER_STATUSES:
        raise IllegalTransition(
            f"connector 0 (the main controller) cannot report {target.value}: OCPP 1.6 s4.9 "
            f"limits it to Available, Unavailable and Faulted"
        )
    transition = find_transition(source, target)
    if transition is None:
        raise IllegalTransition(
            f"{source.value} -> {target.value} is not a legal transition per OCPP 1.6 s4.9"
        )
    return transition


def resolve_suspension(ev_suspended, evse_suspended):
    """Which suspended status applies when the EV and/or EVSE have suspended charging.

    OCPP 1.6 s4.9: "If charging is suspended both by the EV and the EVSE, status
    SuspendedEVSE SHALL have precedence over status SuspendedEV." Returns None when neither
    side has suspended -- charging should continue or has not started.
    """
    if evse_suspended:
        return CPS.suspended_evse
    if ev_suspended:
        return CPS.suspended_ev
    return None


class ConnectorState:
    """The current status of one connector, and the transitions it is allowed to make.

    connector_id is stored so error messages can name the connector, and so change_to applies
    the ConnectorId 0 restriction automatically (see MAIN_CONTROLLER_STATUSES).
    """

    def __init__(self, connector_id, status=CPS.available, pre_fault_status=None):
        self.connector_id = connector_id
        self._status = status
        # Lets a caller restore a connector's fault-recovery point after a restart (see
        # 04-normal-charge-flow.md's ConnectorStatus persistence), rather than reaching into
        # this attribute directly from outside the class.
        self._pre_fault_status = pre_fault_status

    @property
    def status(self):
        return self._status

    @property
    def pre_fault_status(self):
        """The status held immediately before entering Faulted. None when never faulted."""
        return self._pre_fault_status

    def can_change_to(self, target):
        return is_legal(self._status, target, connector_id=self.connector_id)

    def change_to(self, target):
        """Apply a status change.

        Returns the Transition applied, or None when target equals the current status -- a
        repeated StatusNotification, which OCPP chargers send routinely and which is not an
        error (the s4.9 transition table has no diagonal entries). Raises IllegalTransition for
        any other change the table or the ConnectorId 0 restriction does not permit.

        Entering Faulted records the current status as pre_fault_status, so recover_from_fault
        can return to it. Leaving Faulted clears that record.
        """
        if target == self._status:
            return None
        transition = check_transition(self._status, target, connector_id=self.connector_id)
        if target == CPS.faulted:
            self._pre_fault_status = self._status
        elif self._status == CPS.faulted:
            self._pre_fault_status = None
        self._status = target
        return transition

    def recover_from_fault(self):
        """Return to the status held before entering Faulted.

        Raises IllegalTransition when the connector is not currently Faulted, or (which should
        not happen in practice) when no pre-fault status was recorded.
        """
        if self._status != CPS.faulted:
            raise IllegalTransition(
                f"connector {self.connector_id} is {self._status.value}, not Faulted: "
                f"nothing to recover from"
            )
        if self._pre_fault_status is None:
            raise IllegalTransition(
                f"connector {self.connector_id} has no recorded pre-fault status"
            )
        return self.change_to(self._pre_fault_status)

    def force_status(self, target):
        """Set the status directly, bypassing the s4.9 legality check.

        For recording what a charger actually reports when it takes a transition the table
        does not list: real hardware occasionally disagrees with the model (a firmware quirk,
        a message lost and replayed out of order), and the Central System's job is to record
        the charger's reality, not to silently override it or reject the message --
        StatusNotification.conf carries no status field, so there is no way to tell a charger
        it was wrong. Pre-fault bookkeeping stays consistent with change_to.
        """
        if target == CPS.faulted and self._status != CPS.faulted:
            self._pre_fault_status = self._status
        elif self._status == CPS.faulted and target != CPS.faulted:
            self._pre_fault_status = None
        self._status = target
