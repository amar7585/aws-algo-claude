"""
The strategy-manager hand-off for a v2 alert (plan v3: the manager is triggered
by v2 alerts only).

Asynchronous (Event): a slow playbook must not sit inside this function's
timeout. UNSET MEANS OFF: with no STRATEGY_MANAGER_FUNCTION_NAME nothing is
invoked. A configured invoke that does not land RAISES - a detected alert that
never reached the manager is a silent hole.

The manager routes on regime|bias, which live on the 15-min sentiment row, so the
payload carries the latest 15-min snapshot (measurement + judgement merged, as
pattern-detector sent it), the daily read, the instrument, and the alert as
`detection`. It reads nothing else from `detection`.
"""

import json
import logging

import boto3

from config import STRATEGY_MANAGER_FUNCTION_NAME

logger = logging.getLogger()

_lambda = None


def manager_payload(instrument, snapshot, daily, event, day_trend, rules_version):
    return {
        "source": "pattern-recorder",
        "instrument": instrument,
        "snapshot": snapshot,
        "daily": daily,
        "detection": {
            "rules_version": rules_version,
            "day_trend": day_trend,
            "family": event["family"],
            "subtype": event["subtype"],
            "direction": event["direction"],
            "trigger_ts": event["trigger_ts"],
            "trigger_close": event["trigger_close"],
            "level": event["level"],
            "level_price": event["level_price"],
            "level_strike": event["level_strike"],
            "other_levels": event["other_levels"],
            "candles": event["candles"],
            "vol_z": event["vol_z"],
            "rvol_tod": event["rvol_tod"],
        },
    }


def invoke_manager(payload, client=None):
    """Invoke strategy-manager (Event); raises unless Lambda answers 202."""
    global _lambda
    if not STRATEGY_MANAGER_FUNCTION_NAME:
        raise RuntimeError("invoke_manager called with no STRATEGY_MANAGER_FUNCTION_NAME set")
    client = client or _lambda or boto3.client("lambda")
    _lambda = _lambda or client
    response = client.invoke(
        FunctionName=STRATEGY_MANAGER_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload, default=float).encode(),
    )
    status = response.get("StatusCode")
    if status != 202:
        raise RuntimeError(
            f"invoking {STRATEGY_MANAGER_FUNCTION_NAME} returned StatusCode {status} - "
            f"an alert was sent but no strategy was reached")
    logger.info("alert dispatched to %s", STRATEGY_MANAGER_FUNCTION_NAME)
