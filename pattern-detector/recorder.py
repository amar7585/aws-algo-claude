"""
The hand-off to pattern-recorder (pattern-detector v2).

As the LAST step of every run, after the reversal turn, the continuation alerts
and Telegram are all done, the detector forwards the payload market-classifier
gave it to pattern-recorder, asynchronously (Event). The recorder runs the v2
pattern library and writes algo.pattern_events; it alerts nothing and dispatches
nothing.

WHY A SEPARATE FUNCTION, AND WHY LAST. This function has no EventInvokeConfig,
so Lambda retries a failed async invocation twice. Had v2 run in here, a v2
failure after the existing branches would re-send their Telegram alerts and
re-dispatch the manager on every retry. Invoked last and asynchronously, a
recorder failure stays in the recorder's own log group.

UNSET MEANS OFF - the same shape as the strategy-manager gate. With no
PATTERN_RECORDER_FUNCTION_NAME nothing is invoked and a log line says so; that
env var is the kill switch, no deploy needed. Setting it needs this function's
role to allow lambda:InvokeFunction on the recorder's ARN, not a wildcard.
"""

import json
import logging

import boto3

from config import PATTERN_RECORDER_FUNCTION_NAME

logger = logging.getLogger()


def invoke_recorder(event, client=None):
    """
    Forward the run's payload to pattern-recorder. Returns its name, or None when
    unset. Raises when configured and the invoke does not land - a recorder that
    silently stopped receiving snapshots is a hole nobody would notice.
    """
    if not PATTERN_RECORDER_FUNCTION_NAME:
        logger.info("no PATTERN_RECORDER_FUNCTION_NAME set - nothing recorded")
        return None
    payload = {"source": "pattern-detector", **event}
    client = client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=PATTERN_RECORDER_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    if status != 202:
        raise RuntimeError(
            f"invoking {PATTERN_RECORDER_FUNCTION_NAME} returned StatusCode {status} - "
            f"this snapshot was not recorded"
        )
    logger.info("snapshot forwarded to %s", PATTERN_RECORDER_FUNCTION_NAME)
    return PATTERN_RECORDER_FUNCTION_NAME
