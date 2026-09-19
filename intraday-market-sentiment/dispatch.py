"""
Handing the measurement snapshot to market-classifier.

WHY THIS IS AN INVOKE AND NOT A SCHEDULE. The classifier needs the snapshot,
and the snapshot exists only once this function has written it. A cron on the
classifier's side would have to guess how long that takes, read the row back
out of Postgres, and decide what to do when it is not there yet - three
problems that all disappear when the completion of the write is itself the
trigger.

ORDER: WRITE, THEN INVOKE. If the invoke fails this function raises, and the
row is already committed. That is the safe direction: write_snapshot upserts by
primary key, so a retry rewrites the identical row and re-invokes, and nothing
is double-counted. Inverting the order would risk dispatching a snapshot that
was never stored.

ASYNCHRONOUS. This function's own run must not be able to fail because the
classifier or a playbook downstream was slow, and the classifier has its own
log group with error-notifier watching it. What is reported here is only
whether the invoke was accepted.

WHAT TRAVELS. The measurement row (`fno`), today's session bars (the classifier
runs the structure read over them), the VIX baseline (the previous snapshot's
VIX - the expansion test needs it) and the run clock (`now`, for
session_elapsed). The daily read travels too, carried on to strategy-manager so
the router stays a router with no database of its own.

UNSET MEANS OFF. With no CLASSIFIER_FUNCTION_NAME configured this does nothing
and says so, so this module ships and deploys with no behavioural change; the
chain is switched on by setting one environment variable once the classifier
exists and this function has proved itself on a live session.
"""

import json
import logging

import boto3

from config import CLASSIFIER_FUNCTION_NAME, CLASSIFIER_INVOCATION_TYPE

logger = logging.getLogger()


def dispatch_to_classifier(row, instrument, session_bars, *, vix_baseline,
                           now, daily=None, client=None):
    """
    Invoke market-classifier with the measurement row just written.

    Returns the function name on success, or None when no classifier is
    configured. Raises when one is configured and the invoke does not land - a
    snapshot that reached Postgres but never reached the classifier is a silent
    hole in the session, and Lambda has to record it.
    """
    if not CLASSIFIER_FUNCTION_NAME:
        logger.info(
            "no CLASSIFIER_FUNCTION_NAME set - the measurement row is written "
            "and nothing is dispatched"
        )
        return None

    payload = {
        "source": "intraday-market-sentiment",
        "instrument": instrument,
        "fno": row,
        "session_bars": session_bars,
        "vix_baseline": vix_baseline,
        "now": now.isoformat(),
        "daily": daily,
    }
    client = client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=CLASSIFIER_FUNCTION_NAME,
        InvocationType=CLASSIFIER_INVOCATION_TYPE,
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    # 202 Accepted for an Event invoke, 200 for a synchronous one. Anything else
    # is a refusal that did not raise on its own.
    if status not in (200, 202):
        raise RuntimeError(
            f"invoking {CLASSIFIER_FUNCTION_NAME} returned StatusCode {status} "
            f"- the snapshot is written but no classification was reached"
        )
    if response.get("FunctionError"):
        raise RuntimeError(
            f"{CLASSIFIER_FUNCTION_NAME} reported {response['FunctionError']}"
        )
    logger.info(
        "dispatched snapshot %s to %s (%s)",
        row["snapshot_ts"], CLASSIFIER_FUNCTION_NAME, CLASSIFIER_INVOCATION_TYPE,
    )
    return CLASSIFIER_FUNCTION_NAME
