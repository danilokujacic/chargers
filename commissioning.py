"""Route B onboarding: push a unique authorization key to a Pending charge point over OCPP.

See OCPP-J 1.6 s6.2.2 ("Setting the key over OCPP") and instructions/03-commissioning-flow.md.
Route A -- the key installed before or during installation, as seed.py does -- needs none of
this: a charger seeded with its key already stored is Accepted on its first boot.

Terminology, for readers new to OCPP: "commissioning" is bringing a newly installed Charge Point
from powered-on to fully usable. A charger that a Central System does not yet trust gets a
`Pending` registration status in BootNotification.conf; only once it has a unique authorization
key installed does the Central System answer `Accepted`.
"""

import logging

from ocpp.v16 import call
from ocpp.v16.enums import ConfigurationStatus, RegistrationStatus

logger = logging.getLogger(__name__)

# The ChangeConfiguration key that carries the authorization key, per OCPP-J 1.6 s6.2.2: "a
# ChangeConfiguration.req message with the key AuthorizationKey and as the value a 40-character
# hexadecimal representation of the 20-byte authorization key."
AUTHORIZATION_KEY_CONFIG_KEY = "AuthorizationKey"


class PendingChargerError(RuntimeError):
    """A Central-System-initiated action was attempted on a charger that is still Pending.

    OCPP 1.6 s4.2: "While in pending state, the following Central System initiated messages
    are not allowed: RemoteStartTransaction.req and RemoteStopTransaction.req."
    """


def ensure_commissioned(record, action_name):
    """Raise PendingChargerError when `record` has not finished onboarding.

    Call this before sending any Central-System-initiated message that OCPP forbids while a
    charger is Pending. It is not itself a full RemoteStartTransaction implementation -- see
    main.py's send_remote_start_transaction / send_remote_stop_transaction, and
    instructions/06-remaining-flows.md section A for the complete remote-start flow.
    """
    if record.registration_status == RegistrationStatus.pending:
        raise PendingChargerError(
            f"{record.identity} is Pending onboarding: {action_name} is forbidden while "
            f"Pending (OCPP 1.6 s4.2)"
        )


async def boot_status(record):
    """The RegistrationStatus to answer a BootNotification with, given the registry document.

    A charger's status can be Accepted (fully commissioned), Pending (mid Route-B onboarding,
    or never onboarded), or Rejected (an operator has explicitly refused it). The document's
    stored value is authoritative; this function exists so the decision has one place to live,
    independent of the live connection, as instructed.
    """
    return record.registration_status


async def onboard(charge_point, record):
    """Drive Route B for a Pending charger: push a fresh key, then accept it on success.

    Must be called only after the BootNotification.conf that reported Pending has actually
    reached the charger -- see MyChargePoint.after_boot in main.py, which uses the ocpp
    library's @after hook rather than a bare asyncio.create_task from inside the @on handler,
    specifically so this cannot race ahead of that response on the wire (a plain create_task
    can start running at the handler's first await, which happens before the response is sent).

    Implements the asymmetric rule from OCPP-J 1.6 s6.2.2: only an explicit Accepted promotes
    the candidate key and accepts the charger. A Rejected or NotSupported answer, an exception
    while sending the request, or a response that never arrives, all fall back to the SAME safe
    outcome -- the candidate is discarded and the OLD key keeps working -- so nothing about this
    exchange can ever lock a charger out of its own Central System.

    Returns the RegistrationStatus to report on the charger's NEXT BootNotification. The
    registry document is already updated to match by the time this returns.
    """
    key = await record.begin_key_rotation()
    try:
        response = await charge_point.call(
            call.ChangeConfiguration(key=AUTHORIZATION_KEY_CONFIG_KEY, value=key),
            suppress=False,
        )
    except Exception:
        # Covers a CallError, a timeout with no response, and the connection dropping mid
        # exchange. All three must be treated exactly like an explicit Rejected.
        logger.exception(
            "onboarding %s: ChangeConfiguration did not complete; keeping the old key",
            record.identity,
        )
        await record.cancel_key_rotation()
        return record.registration_status

    if response.status == ConfigurationStatus.accepted:
        await record.confirm_key_rotation()
        logger.info("onboarded %s: new authorization key accepted", record.identity)
    else:
        await record.cancel_key_rotation()
        logger.warning(
            "onboarding %s: charger answered %s to ChangeConfiguration; keeping the old key",
            record.identity,
            response.status,
        )
    return record.registration_status
