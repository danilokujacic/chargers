"""Standalone checks for connector_state_machine.py against OCPP 1.6 s4.9.

No MongoDB, no server, no network -- the module under test is pure. Run directly:

    python test_connector_state_machine.py

Prints PASS/FAIL per check and exits non-zero if any check fails.
"""

from ocpp.v16.enums import ChargePointStatus as CPS

from connector_state_machine import (
    TRANSITIONS,
    ConnectorState,
    IllegalTransition,
    check_transition,
    find_transition,
    is_legal,
    resolve_suspension,
    transitions_from,
)

RESULTS = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    RESULTS.append(bool(condition))


ALL_CODES = {
    "A2", "A3", "A4", "A5", "A7", "A8", "A9",
    "B1", "B3", "B4", "B5", "B6", "B9",
    "C1", "C4", "C5", "C6", "C8", "C9",
    "D1", "D3", "D5", "D6", "D8", "D9",
    "E1", "E3", "E4", "E6", "E8", "E9",
    "F1", "F2", "F8", "F9",
    "G1", "G2", "G8", "G9",
    "H1", "H2", "H3", "H4", "H5", "H9",
    "I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8",
}

EXPECTED_SOURCE_COUNTS = {
    CPS.available: 7,
    CPS.preparing: 6,
    CPS.charging: 6,
    CPS.suspended_ev: 6,
    CPS.suspended_evse: 6,
    CPS.finishing: 4,
    CPS.reserved: 4,
    CPS.unavailable: 6,
    CPS.faulted: 8,
}

EXPECTED_TARGET_COUNTS = {
    CPS.available: 8,
    CPS.preparing: 5,
    CPS.charging: 6,
    CPS.suspended_ev: 6,
    CPS.suspended_evse: 6,
    CPS.finishing: 5,
    CPS.reserved: 2,
    CPS.unavailable: 7,
    CPS.faulted: 8,
}


def main():
    all_transitions = [t for targets in TRANSITIONS.values() for t in targets.values()]

    # 1. Total count is exactly 53.
    check(
        "total transition count is 53", len(all_transitions) == 53, f"got {len(all_transitions)}"
    )

    # 2. Per-source counts.
    for status, expected in EXPECTED_SOURCE_COUNTS.items():
        got = len(transitions_from(status))
        detail = f"expected {expected}, got {got}"
        check(f"source count for {status.value}", got == expected, detail)

    # 3. Per-target counts.
    target_counts = {}
    for t in all_transitions:
        target_counts[t.target] = target_counts.get(t.target, 0) + 1
    for status, expected in EXPECTED_TARGET_COUNTS.items():
        got = target_counts.get(status, 0)
        detail = f"expected {expected}, got {got}"
        check(f"target count for {status.value}", got == expected, detail)

    # 4. Every code is unique, and every code from the spec table is present.
    codes = [t.code for t in all_transitions]
    check("all transition codes unique", len(codes) == len(set(codes)))
    check(
        "all 53 spec codes present",
        set(codes) == ALL_CODES,
        f"missing={ALL_CODES - set(codes)} extra={set(codes) - ALL_CODES}",
    )

    # 5. No self-transitions.
    self_transitions = [t for t in all_transitions if t.source == t.target]
    check("no transition has source == target", not self_transitions, self_transitions)

    # 6. Every status except Available can reach Available; every status except Faulted can
    #    reach Faulted.
    all_statuses = set(CPS)
    reach_available = {t.source for t in all_transitions if t.target == CPS.available}
    check(
        "every status but Available can reach Available",
        reach_available == all_statuses - {CPS.available},
        f"missing={all_statuses - {CPS.available} - reach_available}",
    )
    reach_faulted = {t.source for t in all_transitions if t.target == CPS.faulted}
    check(
        "every status but Faulted can reach Faulted",
        reach_faulted == all_statuses - {CPS.faulted},
        f"missing={all_statuses - {CPS.faulted} - reach_faulted}",
    )

    # 7. Only Available and Faulted can reach Reserved.
    reach_reserved = {t.source for t in all_transitions if t.target == CPS.reserved}
    check(
        "only Available and Faulted can reach Reserved",
        reach_reserved == {CPS.available, CPS.faulted},
        f"got {reach_reserved}",
    )

    # 8. Specific illegal pairs.
    for source, target in [
        (CPS.charging, CPS.preparing),
        (CPS.finishing, CPS.charging),
        (CPS.preparing, CPS.unavailable),
        (CPS.reserved, CPS.charging),
    ]:
        check(
            f"{source.value} -> {target.value} is illegal",
            find_transition(source, target) is None,
        )

    # 9. ConnectorId 0 restriction.
    check(
        "connector 0 cannot go Available -> Preparing",
        is_legal(CPS.available, CPS.preparing, connector_id=0) is False,
    )
    check(
        "connector 0 can go Available -> Unavailable",
        is_legal(CPS.available, CPS.unavailable, connector_id=0) is True,
    )

    # 10. check_transition raises IllegalTransition with both status names in the message.
    try:
        check_transition(CPS.charging, CPS.preparing)
        check("check_transition raises on illegal pair", False)
    except IllegalTransition as exc:
        message = str(exc)
        check(
            "check_transition raises on illegal pair",
            "charging" in message.lower() or "Charging" in message,
        )
        check(
            "IllegalTransition message names both statuses",
            "Charging" in message and "Preparing" in message,
            message,
        )

    # 11. ConnectorState.change_to on a repeat is a no-op returning None.
    state = ConnectorState(connector_id=1)
    check("new ConnectorState starts Available", state.status == CPS.available)
    result = state.change_to(CPS.available)
    check("change_to same status returns None", result is None)
    check("change_to same status leaves status unchanged", state.status == CPS.available)

    # 12. Fault and recover.
    state = ConnectorState(connector_id=1, status=CPS.charging)
    state.change_to(CPS.faulted)
    check("pre_fault_status recorded", state.pre_fault_status == CPS.charging)
    transition = state.recover_from_fault()
    check("recover_from_fault returns to Charging", state.status == CPS.charging)
    check("recover_from_fault returns the Transition applied", transition.code == "I3")
    check("pre_fault_status cleared after recovery", state.pre_fault_status is None)

    fresh_state = ConnectorState(connector_id=1)
    try:
        fresh_state.recover_from_fault()
        check("recover_from_fault raises when not Faulted", False)
    except IllegalTransition:
        check("recover_from_fault raises when not Faulted", True)

    # 13. resolve_suspension precedence.
    check(
        "resolve_suspension(True, True) is SuspendedEVSE",
        resolve_suspension(True, True) == CPS.suspended_evse,
    )
    check(
        "resolve_suspension(True, False) is SuspendedEV",
        resolve_suspension(True, False) == CPS.suspended_ev,
    )
    check(
        "resolve_suspension(False, True) is SuspendedEVSE",
        resolve_suspension(False, True) == CPS.suspended_evse,
    )
    check(
        "resolve_suspension(False, False) is None", resolve_suspension(False, False) is None
    )

    # Extra: ConnectorState honours the connector-0 restriction through change_to too, and
    # raises IllegalTransition (not silently ignoring) for a genuinely illegal move.
    main_state = ConnectorState(connector_id=0)
    try:
        main_state.change_to(CPS.preparing)
        check("ConnectorState(0) rejects Preparing via change_to", False)
    except IllegalTransition:
        check("ConnectorState(0) rejects Preparing via change_to", True)

    illegal_state = ConnectorState(connector_id=1, status=CPS.finishing)
    try:
        illegal_state.change_to(CPS.charging)
        check("ConnectorState raises IllegalTransition for Finishing -> Charging", False)
    except IllegalTransition:
        check("ConnectorState raises IllegalTransition for Finishing -> Charging", True)

    print()
    passed = sum(RESULTS)
    total = len(RESULTS)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


raise SystemExit(main())
