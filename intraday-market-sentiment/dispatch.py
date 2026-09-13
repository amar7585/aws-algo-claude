"""
Handing the snapshot to strategy-manager.

WHY THIS IS AN INVOKE AND NOT A SCHEDULE. The manager needs the snapshot,
and the snapshot exists only once this function has written it. A cron on the
manager's side would have to guess how long that takes, read the row back
out of Postgres, and decide what to do when it is not there yet - three
problems that all disappear when the completion of the write is itself the
trigger.

ORDER: WRITE, THEN INVOKE. If the invoke fails this function raises, and the
row is already committed. That is the safe direction: write_snapshot upserts by
primary key, so a retry rewrites the identical row and re-invokes, and nothing
is double-counted. Inverting the order would risk dispatching a snapshot that
was never stored.

ASYNCHRONOUS. This function's own run must not be able to fail because a
downstream playbook was slow, and the manager has its own log group with
error-notifier watching it. What is reported here is only whether the invoke
was accepted.

UNSET MEANS OFF. With no STRATEGY_MANAGER_FUNCTION_NAME configured this does
nothing and says so. That is deliberate: it lets this module ship and be
deployed with no behavioural change at all, so the chain is switched on by
setting one environment variable once the manager exists and this
function has proved itself on a live session - rather than by a code change at
the moment of cutover.
"""

import json
import logging

import boto3

from config import STRATEGY_MANAGER_FUNCTION_NAME, STRATEGY_MANAGER_INVOCATION_TYPE

logger = logging.getLogger()


def dispatch_snapshot(row, instrument, client=None):
    """
    Invoke strategy-manager with the row just written.

    Returns the function name on success, or None when no manager is
    configured. Raises when one is configured and the invoke does not land -
    a snapshot that reached Postgres but never reached the strategies is a
    silent hole in the session, and Lambda has to record it.
    """
    if not STRATEGY_MANAGER_FUNCTION_NAME:
        logger.info(
            "no STRATEGY_MANAGER_FUNCTION_NAME set - the snapshot is written and "
            "nothing is dispatched"
        )
        return None

    payload = {
        "source": "intraday-market-sentiment",
        "instrument": instrument,
        "snapshot": row,
    }
    client = client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=STRATEGY_MANAGER_FUNCTION_NAME,
        InvocationType=STRATEGY_MANAGER_INVOCATION_TYPE,
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    # 202 Accepted is the success code for an Event invoke, 200 for a
    # synchronous one. Anything else is a refusal that did not raise on its own.
    if status not in (200, 202):
        raise RuntimeError(
            f"invoking {STRATEGY_MANAGER_FUNCTION_NAME} returned StatusCode "
            f"{status} - the snapshot is written but no strategy was reached"
        )
    if response.get("FunctionError"):
        raise RuntimeError(
            f"{STRATEGY_MANAGER_FUNCTION_NAME} reported "
            f"{response['FunctionError']}"
        )
    logger.info(
        "dispatched snapshot %s to %s (%s)",
        row["snapshot_ts"],
        STRATEGY_MANAGER_FUNCTION_NAME,
        STRATEGY_MANAGER_INVOCATION_TYPE,
    )
    return STRATEGY_MANAGER_FUNCTION_NAME
