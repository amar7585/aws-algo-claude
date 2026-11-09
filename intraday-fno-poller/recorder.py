"""
The hand-off to pattern-recorder (v2.1, plan v3 step 4).

As the LAST step of every run, after the 5-min row and its legs are committed,
the poller hands the tick to pattern-recorder, asynchronously (Event). The
recorder fetches today's candles itself, detects the v2 patterns, and sends the
alerts. This function does not wait for it and is unaffected by what it finds.

WHY HERE. The tick is the 5-min clock v2 runs on, and it is written about 100 s
after each candle closes (measured p50 99 s, max 107 s over 663 ticks), so by
the time the recorder runs, the trigger candle's own OI change is already stored
and can go in the alert.

UNSET MEANS OFF - with no PATTERN_RECORDER_FUNCTION_NAME nothing is invoked and a
log line says so; that env var is the kill switch, no deploy needed. Setting it
needs this function's role to allow lambda:InvokeFunction on the recorder's ARN,
not a wildcard.

A CONFIGURED INVOKE THAT DOES NOT LAND RAISES. This function has retries off
(EventInvokeConfig and the schedule both 0), so the error is recorded once, the
already-committed row stands, and error-notifier reports it.
"""

import json
import logging

import boto3

from config import PATTERN_RECORDER_FUNCTION_NAME

logger = logging.getLogger()

# The tick fields the recorder reads; the rest of the row stays in the table.
TICK_FIELDS = ("security_id", "instrument_type", "snapshot_ts", "captured_at", "prev_snapshot_ts",
               "fut_security_id", "fut_symbol", "fut_bar_ts", "spot", "fut_price")


def invoke_recorder(row, index, future, client=None):
    """Returns the recorder's name, or None when unset. Raises unless Lambda answers 202."""
    if not PATTERN_RECORDER_FUNCTION_NAME:
        logger.info("no PATTERN_RECORDER_FUNCTION_NAME set - pattern-recorder not invoked")
        return None
    payload = {
        "source": "intraday-fno-poller",
        "tick": {k: row.get(k) for k in TICK_FIELDS},
        "index": index,
        "future": future,
    }
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
            f"this tick was not handed to the recorder")
    logger.info("tick handed to %s", PATTERN_RECORDER_FUNCTION_NAME)
    return PATTERN_RECORDER_FUNCTION_NAME
