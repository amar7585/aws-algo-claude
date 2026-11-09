"""
The strategy-manager hand-off for a v2 alert (plan v3: the manager is triggered
by v2 alerts only).

Asynchronous (Event): a slow playbook must not sit inside this function's
timeout. UNSET MEANS OFF: with no STRATEGY_MANAGER_FUNCTION_NAME nothing is
invoked. A configured invoke that does not land RAISES - a detected alert that
never reached the manager is a silent hole.

The manager routes a trend day (detection.day_trend up/down) to the trend
playbooks and everything else on regime|bias, which live on the 15-min sentiment
row. So the payload carries the latest 15-min snapshot (measurement + judgement
merged, as pattern-detector sent it), the daily read, the instrument, the alert
as `detection`, and - since 2026-11-09 - today's CLOSED index candles up to this
tick as `index_bars`: a playbook judging a 5-min alert cannot use candle_5min,
which the loader refreshes only every 15 minutes. About 75 candles x 5 numbers,
a few KB against the 256 KB async limit.
"""

import json
import logging

import boto3

from config import STRATEGY_MANAGER_FUNCTION_NAME

logger = logging.getLogger()

_lambda = None


def manager_payload(instrument, snapshot, daily, event, day_trend, rules_version, index_bars):
    return {
        "source": "pattern-recorder",
        "instrument": instrument,
        "snapshot": snapshot,
        "daily": daily,
        "index_bars": [{k: b[k] for k in ("ts", "open", "high", "low", "close")} for b in index_bars],
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
