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

from db import read_bars, read_future_bars, read_prev_pcr
from detect import detect, detect_continuation
from dispatch import dispatch_to_manager
from notify import send_telegram

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


# ---- Telegram message bodies. ASCII only, one setup per message. ------------

def _reasons(confirmed_by):
    return ", ".join(confirmed_by) if confirmed_by else "no OI/PCR confirmation yet"


def _reversal_message(stamp, detection):
    return (
        f"TURN {detection['direction'].upper()} - {stamp}\n"
        f"reversal candle {ist_datetime(detection['candidate_ts']).strftime('%H:%M')} "
        f"(vol z {detection['volume_z']})\n"
        f"confirmed by: {_reasons(detection['confirmed_by'])}\n"
        f"level {detection['level_distance_pct']}% (at_level={detection['at_level']})"
    )


def _break_message(stamp, alert):
    return (
        f"LEVEL BREAK {alert['direction'].upper()} - {stamp}\n"
        f"{alert['level']} {alert['level_price']} broken "
        f"({ist_datetime(alert['break_ts']).strftime('%H:%M')}, vol z {alert['volume_z']})\n"
        f"data: {_reasons(alert['confirmed_by'])}"
    )


def _retest_message(stamp, alert):
    return (
        f"RETEST ENTRY {alert['direction'].upper()} - {stamp}\n"
        f"{alert['level']} {alert['level_price']} broken "
        f"({ist_datetime(alert['break_ts']).strftime('%H:%M')}), retest held, "
        f"entry {alert['entry_price']} "
        f"({ist_datetime(alert['entry_ts']).strftime('%H:%M')}, vol z {alert['volume_z']})\n"
        f"data: {_reasons(alert['confirmed_by'])}"
    )


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    fno, sentiment, instrument, daily = read_event(event or {})
    snapshot_ts = int(fno["snapshot_ts"])
    stamp = ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M")

    conn = connect(read_neon_connection_string())
    try:
        fut_bars = read_future_bars(conn, fno["fut_security_id"], snapshot_ts)
        # The index's own bars, for the continuation break/retest - the levels are
        # spot levels, so the crossing is read here, not on the future.
        index_bars = read_bars(
            conn, fno["security_id"], fno["instrument_type"], snapshot_ts
        )
        # The prior snapshot's PCR, so a continuation alert can show direction
        # (rising / falling) rather than only the current level.
        prev_pcr = read_prev_pcr(
            conn, fno["security_id"], fno["instrument_type"],
            fno.get("prev_snapshot_ts"),
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    detection = detect(fno, sentiment, fut_bars)
    alerts = detect_continuation(
        fno, sentiment, index_bars, fut_bars, daily,
        fno.get("prev_snapshot_ts"), prev_pcr,
    )

    # "Fresh" = the trigger bar closed within the last snapshot interval. The
    # detection is re-derived every run, so gating the ALERT on freshness is what
    # announces each event once instead of on every 15-minute re-derivation.
    fresh_after = int(fno.get("prev_snapshot_ts") or (snapshot_ts - 900))
    messages = []
    dispatched = None

    # ---- reversal turn: dispatch unchanged; alert only on the fresh turn ------
    if detection["turn"]:
        logger.info(
            "snapshot %s: TURN %s, reversal candle %s (volume z %s), confirmed "
            "by %s, level %s%% (at_level=%s)",
            stamp, detection["direction"],
            ist_datetime(detection["candidate_ts"]).strftime("%H:%M"),
            detection["volume_z"], ", ".join(detection["confirmed_by"]),
            detection["level_distance_pct"], detection["at_level"],
        )
        dispatched = dispatch_to_manager(fno, sentiment, instrument, detection, daily)
        if fresh_after < int(detection["candidate_ts"]) <= snapshot_ts:
            messages.append(_reversal_message(stamp, detection))
    else:
        logger.info(
            "snapshot %s: no turn - %s (%d future bars read)",
            stamp, detection.get("reason"), len(fut_bars),
        )

    # ---- continuation / breakout alerts (log-only downstream; Telegram here) --
    for alert in alerts:
        if alert["kind"] == "level_break":
            logger.info(
                "snapshot %s: LEVEL BREAK %s %s %s (%s, vol z %s), data: %s",
                stamp, alert["direction"], alert["level"], alert["level_price"],
                ist_datetime(alert["break_ts"]).strftime("%H:%M"),
                alert["volume_z"], alert["confirmed_by"] or "none",
            )
            messages.append(_break_message(stamp, alert))
        else:  # retest_entry
            logger.info(
                "snapshot %s: RETEST ENTRY %s %s %s, entry %s (%s, vol z %s), "
                "data: %s",
                stamp, alert["direction"], alert["level"], alert["level_price"],
                alert["entry_price"],
                ist_datetime(alert["entry_ts"]).strftime("%H:%M"),
                alert["volume_z"], alert["confirmed_by"] or "none",
            )
            messages.append(_retest_message(stamp, alert))

    # ---- deliver. Fail-loud: a refused alert must raise so Lambda records it. --
    for text in messages:
        send_telegram(text)

    return {
        "status": "success",
        "snapshot": stamp,
        "turn": detection["turn"],
        "direction": detection.get("direction"),
        "dispatched_to": dispatched,
        "continuation_alerts": [alert["kind"] for alert in alerts],
        "telegram_sent": len(messages),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
