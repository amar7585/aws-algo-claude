"""
Strategy: trend-day pullback - AWS Lambda function

On a trend day, a key level broken in the trend's direction and then HELD:
this function turns v2's hold alert into a candidate with entry, stop, T1 and T2,
logs it, and sends it to Telegram. Rules: pullback.py. Reference session
2026-11-06 (Amar's PDH hold). It is the "bullish trending - pullback
continuation" slot of the strategy registry, with the bearish mirror.

TRIGGER. strategy-manager, asynchronously, when a v2 alert arrives on a day
whose classifier day_trend is up or down. Sweeps and no-trend days go to the
regime|bias registry instead (the sweep playbook), never here.

READS NOTHING. The context carries everything: v2's alert (`detection`), today's
closed index candles up to the alert (`index_bars`, handed over by v2 - the
stored candles lag up to 15 minutes), the 15-min snapshot and the daily read.
No database, no broker API, no layers.

WRITES NOTHING. Its log and its Telegram message are its output. No orders, no
sizing, no strikes.

TELEGRAM_ENABLED unset = SHADOW: the candidate is logged "WOULD SEND".

Modules:
    config.py   tunables (all env-overridable)
    pullback.py the gate and the candidate (pure)
    report.py   the Telegram block
    notify.py   Telegram delivery
"""

import datetime
import logging
import time

from config import EXPECTED_CONTEXT_VERSION, TELEGRAM_ENABLED
from report import render
from pullback import clock, evaluate

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REQUIRED_DETECTION = ("day_trend", "family", "direction", "trigger_ts", "level")


def read_event(event):
    if not isinstance(event, dict):
        raise RuntimeError(f"expected strategy-manager's context object, got {type(event).__name__}")
    version = event.get("context_version")
    if version != EXPECTED_CONTEXT_VERSION:
        raise RuntimeError(f"context_version {version!r} but this function is written against "
                           f"v{EXPECTED_CONTEXT_VERSION} - keys present: {sorted(event)}")
    for field in ("snapshot", "instrument", "detection"):
        if not isinstance(event.get(field), dict):
            raise RuntimeError(f"context carries no `{field}` object - this playbook runs only on a v2 alert "
                               f"routed by day_trend. Keys present: {sorted(event)}")
    detection = event["detection"]
    missing = [k for k in REQUIRED_DETECTION if detection.get(k) is None]
    if missing:
        raise RuntimeError(f"detection is missing {missing} - keys present: {sorted(detection)}")
    bars = event.get("index_bars")
    if not isinstance(bars, list) or not bars:
        raise RuntimeError("context carries no `index_bars` - pattern-recorder must hand over today's "
                           "closed index candles with the alert")
    return event["snapshot"], event["instrument"], detection, bars, event.get("daily")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    snapshot, instrument, detection, bars, daily = read_event(event or {})
    trigger_ts = int(detection["trigger_ts"])
    day = (trigger_ts + 19800) // 86400
    label = (f"{detection['family']}{'/' + detection['subtype'] if detection.get('subtype') else ''} "
             f"{detection['direction']} @{detection['level']} candle {clock(trigger_ts)}, day trend {detection['day_trend']}")

    result = evaluate(detection, bars, daily)
    if result["status"] != "candidate":
        for reason in result["reasons"]:
            logger.info("stand down (%s): %s", label, reason)
        out = {"status": "stand_down", "alert": label, "reasons": result["reasons"]}
    else:
        date = (datetime.date(1970, 1, 1) + datetime.timedelta(days=day)).isoformat()
        text = render(result, detection, snapshot, f"{date} {clock(trigger_ts)}")
        if TELEGRAM_ENABLED:
            from notify import send_telegram
            send_telegram(text)
            logger.info("CANDIDATE sent: %s", text.replace("\n", " | "))
        else:
            logger.info("WOULD SEND (shadow): %s", text.replace("\n", " | "))
        out = {"status": "candidate", "sent": TELEGRAM_ENABLED, "alert": label, "side": result["side"],
               "entry": result["entry"], "stop_line": result["off_line"], "stop_nominal": round(result["stop_nominal"], 2),
               "risk": round(result["risk"], 2), "t1": result["t1"], "rr": None if result["rr"] is None else round(result["rr"], 2)}
    logger.info("done in %.2fs: %s -> %s (%s, %d index candles)", time.monotonic() - started, label, out["status"],
                instrument.get("trading_symbol"), len(bars))
    return out
