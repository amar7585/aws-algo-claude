"""
The strategy-manager gate.

The detector invokes the manager ONLY on a confirmed turn - so from day one the
manager runs on a turn, not on every snapshot. Same shape as the other
dispatches: asynchronous (Event), write-nothing-first-there-is-nothing-to-write,
UNSET means off.

The manager routes on regime|bias, which live on the sentiment row; it is handed
the measurement and judgement MERGED as `snapshot` (so a playbook has the full
picture and fetches only its own bars), with the detection alongside so a
playbook can see what turned and which way.
"""

import json
import logging

import boto3

from config import (
    STRATEGY_MANAGER_FUNCTION_NAME,
    STRATEGY_MANAGER_INVOCATION_TYPE,
)

logger = logging.getLogger()


def dispatch_to_manager(fno, sentiment, instrument, detection, daily=None, client=None):
    """
    Invoke strategy-manager for a confirmed turn.

    Returns the function name on success, or None when no manager is configured.
    Raises when one is configured and the invoke does not land - a detected turn
    that never reached the manager is a silent hole, and Lambda has to record it.
    """
    if not STRATEGY_MANAGER_FUNCTION_NAME:
        logger.info(
            "no STRATEGY_MANAGER_FUNCTION_NAME set - turn detected, nothing "
            "dispatched"
        )
        return None

    # The manager reads regime|bias from `snapshot`; give it measurement +
    # judgement merged (identical keys carry identical values), so a playbook
    # has the full read and fetches only bars.
    snapshot = {**fno, **sentiment}
    payload = {
        "source": "pattern-detector",
        "instrument": instrument,
        "snapshot": snapshot,
        "detection": detection,
        "daily": daily,
    }
    client = client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=STRATEGY_MANAGER_FUNCTION_NAME,
        InvocationType=STRATEGY_MANAGER_INVOCATION_TYPE,
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    if status not in (200, 202):
        raise RuntimeError(
            f"invoking {STRATEGY_MANAGER_FUNCTION_NAME} returned StatusCode "
            f"{status} - a turn was detected but no strategy was reached"
        )
    if response.get("FunctionError"):
        raise RuntimeError(
            f"{STRATEGY_MANAGER_FUNCTION_NAME} reported {response['FunctionError']}"
        )
    logger.info(
        "turn (%s) dispatched to %s (%s)",
        detection.get("direction"),
        STRATEGY_MANAGER_FUNCTION_NAME,
        STRATEGY_MANAGER_INVOCATION_TYPE,
    )
    return STRATEGY_MANAGER_FUNCTION_NAME
