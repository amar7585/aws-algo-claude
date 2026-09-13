"""
Handing the session on to intraday-market-sentiment.

WHY THE LOADER STARTS THE CHAIN. This function is the only scheduled thing in
the intraday plane. Everything downstream - the snapshot, the routing, the
playbooks - is triggered by the step before it:

    EventBridge -> intraday-data-loader -> intraday-market-sentiment
                -> strategy-manager -> playbooks

One schedule, not four. The alternative was a cron on each, every one of them
guessing how long the step before it takes; the completion of this function's
commit is the only honest trigger for the next step, so it is the trigger.

WHAT THE SNAPSHOT DOES NOT INHERIT. Being invoked from here is not a promise
that intraday-market-sentiment may read candle_5min - it still fetches its own
candles from Dhan. This invoke says "a run is due", nothing more. The loader
having committed says nothing about whether its write covered the bar the
snapshot will describe, and a snapshot row is never revisited.

ORDER: COMMIT, THEN INVOKE. If the invoke fails this function raises with the
candles already stored. That is the safe direction - the upsert makes a retry
rewrite identical rows - whereas invoking first could hand the sentiment
function a session whose candles were never stored.

ASYNCHRONOUS. This run must not fail because something downstream was slow,
and every function in the chain has its own log group with error-notifier
watching it. What is reported here is only whether the invoke was accepted.

UNSET MEANS OFF. With no INTRADAY_SENTIMENT_FUNCTION_NAME configured this does
nothing and says so, so the module ships and deploys with no behavioural
change: the chain is switched on by setting one environment variable, not by a
code change at the moment of cutover.

Setting it also needs one IAM change - this function's execution role must
allow lambda:InvokeFunction on the sentiment function's ARN, and NOT on a
wildcard. Borrowing another function's role fails SILENTLY here; see CLAUDE.md.
"""

import json
import logging

import boto3

from config import (
    INTRADAY_SENTIMENT_FUNCTION_NAME,
    INTRADAY_SENTIMENT_INVOCATION_TYPE,
)

logger = logging.getLogger()


def dispatch_session(summary, client=None):
    """
    Invoke intraday-market-sentiment now the candles are committed.

    Returns the function name on success, or None when nothing is configured.
    Raises when one is configured and the invoke does not land - a session
    whose candles were stored but whose snapshot never ran is a hole nothing
    else would report, because the snapshot function never starts and so never
    logs.
    """
    if not INTRADAY_SENTIMENT_FUNCTION_NAME:
        logger.info(
            "no INTRADAY_SENTIMENT_FUNCTION_NAME set - candles are committed "
            "and nothing is dispatched"
        )
        return None

    payload = {"source": "intraday-data-loader", "run": summary}
    client = client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=INTRADAY_SENTIMENT_FUNCTION_NAME,
        InvocationType=INTRADAY_SENTIMENT_INVOCATION_TYPE,
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    # 202 Accepted is the success code for an Event invoke, 200 for a
    # synchronous one. Anything else is a refusal that did not raise on its own.
    if status not in (200, 202):
        raise RuntimeError(
            f"invoking {INTRADAY_SENTIMENT_FUNCTION_NAME} returned StatusCode "
            f"{status} - the candles are committed but no snapshot was taken"
        )
    if response.get("FunctionError"):
        raise RuntimeError(
            f"{INTRADAY_SENTIMENT_FUNCTION_NAME} reported "
            f"{response['FunctionError']}"
        )
    logger.info(
        "dispatched to %s (%s)",
        INTRADAY_SENTIMENT_FUNCTION_NAME,
        INTRADAY_SENTIMENT_INVOCATION_TYPE,
    )
    return INTRADAY_SENTIMENT_FUNCTION_NAME
