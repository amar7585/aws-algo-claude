"""
Handing the judgement to pattern-detector.

Same shape as intraday-market-sentiment's dispatch: write, then invoke,
asynchronously (Event), UNSET means off. pattern-detector runs the two-clock
turn detection and, when it fires, invokes strategy-manager - so from day one
the manager runs only on a detected turn. The completion of the judgement write
is the honest trigger for the detector.

WHAT TRAVELS. The judgement row (`sentiment`), the measurement row (`fno`) it was
built from - the detector's volume/OI two-clock read needs the futures deltas and
buildup - the instrument, and the daily read carried on for the manager.
"""

import json
import logging

import boto3

from config import (
    PATTERN_DETECTOR_FUNCTION_NAME,
    PATTERN_DETECTOR_INVOCATION_TYPE,
)

logger = logging.getLogger()


def dispatch_to_pattern_detector(sentiment, fno, instrument, daily=None, client=None):
    """
    Invoke pattern-detector with the judgement just written.

    Returns the function name on success, or None when no detector is configured.
    Raises when one is configured and the invoke does not land - a judgement that
    reached Postgres but never reached the detector is a silent hole, and Lambda
    has to record it.
    """
    if not PATTERN_DETECTOR_FUNCTION_NAME:
        logger.info(
            "no PATTERN_DETECTOR_FUNCTION_NAME set - the judgement is written "
            "and nothing is dispatched"
        )
        return None

    payload = {
        "source": "market-classifier",
        "instrument": instrument,
        "fno": fno,
        "sentiment": sentiment,
        "daily": daily,
    }
    client = client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=PATTERN_DETECTOR_FUNCTION_NAME,
        InvocationType=PATTERN_DETECTOR_INVOCATION_TYPE,
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    if status not in (200, 202):
        raise RuntimeError(
            f"invoking {PATTERN_DETECTOR_FUNCTION_NAME} returned StatusCode "
            f"{status} - the judgement is written but no detector was reached"
        )
    if response.get("FunctionError"):
        raise RuntimeError(
            f"{PATTERN_DETECTOR_FUNCTION_NAME} reported {response['FunctionError']}"
        )
    logger.info(
        "dispatched sentiment %s to %s (%s)",
        sentiment["snapshot_ts"],
        PATTERN_DETECTOR_FUNCTION_NAME,
        PATTERN_DETECTOR_INVOCATION_TYPE,
    )
    return PATTERN_DETECTOR_FUNCTION_NAME
