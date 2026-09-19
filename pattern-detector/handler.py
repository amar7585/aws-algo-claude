"""
Pattern Detector - AWS Lambda function

The two-clock turn detector, and the gate on strategy-manager. Invoked by
market-classifier once the judgement row is written; it reads the future's
recent 5-min bars from candle_5min, applies the abnormal-volume-reversal
(same-tick) + buildup/option-OI (+1-tick) rule, LOGS the result either way, and
invokes strategy-manager ONLY when a turn is confirmed.

    loader -> intraday-market-sentiment -> market-classifier -> this -> strategy-manager

EVERY THRESHOLD IS PROVISIONAL - 5 turns over 2 sessions. See config.py and the
observations findings. The structure is fixed; the numbers get calibrated as
sessions accumulate, so on early sessions the manager will fire rarely.

STATELESS. The candidate is re-derived from the stored future bars each run; no
pending turn is remembered between invokes, matching
strategy-range-liquidity-sweep.

WHAT IT WRITES. Nothing yet - the detection is its log output, the way the sweep
playbook's is. A table comes once the rule is proven on live sessions.

Reads only Neon project "AI Trader APP" (nameless-mountain-15353651), database
Algo, schema algo.

Modules:
    config.py    provisional thresholds and the manager gate
    db.py        the future's recent 5-min bars from candle_5min
    detect.py    the two-clock turn rule
    dispatch.py  the strategy-manager gate
"""

import logging
import time

from neon_access import connect, ist_datetime, read_neon_connection_string

from db import read_future_bars
from detect import detect
from dispatch import dispatch_to_manager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The keys the detector itself reads off each row. Everything else is carried
# through. A payload without these means market-classifier sent an old shape;
# raising here names the real problem rather than failing deep in the scan.
REQUIRED_FNO = ("security_id", "instrument_type", "snapshot_ts", "fut_security_id", "spot")
REQUIRED_SENTIMENT = ("regime", "bias")


def read_event(event):
    """Validate market-classifier's payload and return its parts."""
    if not isinstance(event, dict):
        raise RuntimeError(
            f"expected market-classifier's payload, got {type(event).__name__}"
        )
    fno = event.get("fno")
    sentiment = event.get("sentiment")
    if not isinstance(fno, dict) or not isinstance(sentiment, dict):
        raise RuntimeError(
            f"payload needs `fno` and `sentiment` dicts - keys present: "
            f"{sorted(event)}"
        )
    missing = [k for k in REQUIRED_FNO if fno.get(k) is None]
    missing += [k for k in REQUIRED_SENTIMENT if sentiment.get(k) is None]
    if missing:
        raise RuntimeError(f"payload is missing {missing} - it cannot be scanned")
    instrument = event.get("instrument")
    if not isinstance(instrument, dict):
        raise RuntimeError("payload carries no `instrument` object")
    return fno, sentiment, instrument, event.get("daily")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    fno, sentiment, instrument, daily = read_event(event or {})
    snapshot_ts = int(fno["snapshot_ts"])
    stamp = ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M")

    conn = connect(read_neon_connection_string())
    try:
        fut_bars = read_future_bars(conn, fno["fut_security_id"], snapshot_ts)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    detection = detect(fno, sentiment, fut_bars)

    if not detection["turn"]:
        logger.info(
            "snapshot %s: no turn - %s (%d future bars read)",
            stamp, detection.get("reason"), len(fut_bars),
        )
        return {
            "status": "success", "snapshot": stamp, "turn": False,
            "reason": detection.get("reason"),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    logger.info(
        "snapshot %s: TURN %s, reversal candle %s (volume z %s), confirmed by "
        "%s, level %s%% (at_level=%s)",
        stamp, detection["direction"],
        ist_datetime(detection["candidate_ts"]).strftime("%H:%M"),
        detection["volume_z"], ", ".join(detection["confirmed_by"]),
        detection["level_distance_pct"], detection["at_level"],
    )
    dispatched = dispatch_to_manager(fno, sentiment, instrument, detection, daily)

    return {
        "status": "success",
        "snapshot": stamp,
        "turn": True,
        "direction": detection["direction"],
        "reversal_candle": ist_datetime(detection["candidate_ts"]).strftime("%H:%M"),
        "volume_z": detection["volume_z"],
        "confirmed_by": detection["confirmed_by"],
        "level_distance_pct": detection["level_distance_pct"],
        "at_level": detection["at_level"],
        "dispatched_to": dispatched,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
