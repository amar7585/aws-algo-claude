"""
Strategy: trend-day pullback - AWS Lambda function

On a trend day, a key level broken in the trend's direction and then HELD:
this function turns v2's hold alert into a candidate with entry, stop and T1,
and - since P2b (Amar 2026-11-22 / 11-23) - FOLLOWS the trade to its end.
Rules: pullback.py (the setup), exits.py (the decided exit, "Previous-candle
trail"). Reference session 2026-11-06 (Amar's PDH hold).

TRIGGER. strategy-manager (context v4), asynchronously, on two kinds of run:
  * an ALERT on a day whose classifier day_trend is up or down (entry mode):
    the candidate becomes an ENTRY through order-manager - 3 lots at the trigger
    close, stop on the trend-off line, T1 = entry + 2R (2 of 3 lots sold there) -
    or, when the risk manager allows no entry, a REJECTED signal;
  * a TICK while it holds a position (exit mode): the risk manager passes its
    open positions in `run`, and the decided rule is re-derived over today's
    closed candles; what order-manager does not have yet is sent (T1, stop to
    entry, trail moves, the stop) - and on force_exit, the time exit at the
    15:10 close or the daily-loss exit.

READS NOTHING. The context carries everything: v2's alert, today's closed index
candles, the snapshot, the daily read, and the risk manager's `run` (with this
playbook's position rows on a tick). No database, no broker API, no layers.

WRITES NOTHING ITSELF. Every entry and step goes to order-manager (its own
Lambda, synchronous, one signal per call), which records it in
algo.position_master and sends this playbook's text to Telegram, once.

TELEGRAM_ENABLED unset = SHADOW: order-manager records, and the text is only
logged ("WOULD SEND").

Modules:
    config.py    tunables (all env-overridable)
    pullback.py  the gate and the candidate (pure)
    exits.py     the decided exit rule (pure)
    record.py    the signals for order-manager, read-then-catch-up (pure)
    report.py    the candidate's Telegram block
    ordermgr.py  the synchronous call to order-manager
"""

import datetime
import logging
import time

import ordermgr
import record
from config import EXPECTED_CONTEXT_VERSION, ORDER_MANAGER_FUNCTION, POSITION_PLAYBOOK, TELEGRAM_ENABLED, TICK_SOURCE
from report import render
from pullback import clock, evaluate

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REQUIRED_DETECTION = ("day_trend", "family", "direction", "trigger_ts", "level")
MODES = ("entry_exit", "exit_only", "force_exit")


def read_event(event):
    if not isinstance(event, dict):
        raise RuntimeError(f"expected strategy-manager's context object, got {type(event).__name__}")
    version = event.get("context_version")
    if version != EXPECTED_CONTEXT_VERSION:
        raise RuntimeError(f"context_version {version!r} but this function is written against "
                           f"v{EXPECTED_CONTEXT_VERSION} - keys present: {sorted(event)}")
    if not ORDER_MANAGER_FUNCTION:
        raise RuntimeError("ORDER_MANAGER_FUNCTION is unset - from context v4 every entry and step goes through order-manager")
    run = event.get("run")
    if not isinstance(run, dict) or run.get("mode") not in MODES:
        raise RuntimeError(f"context carries no valid `run` from the risk manager: {run!r}")
    if run["mode"] == "force_exit" and run.get("force") not in ("TIME EXIT", "DAILY LOSS EXIT"):
        raise RuntimeError(f"force_exit without a known reason: {run.get('force')!r}")
    for field in ("snapshot", "instrument"):
        if not isinstance(event.get(field), dict):
            raise RuntimeError(f"context carries no `{field}` object - keys present: {sorted(event)}")
    bars = event.get("index_bars")
    if not isinstance(bars, list) or not bars:
        raise RuntimeError("context carries no `index_bars` - pattern-recorder must hand over today's closed index candles")
    bars = sorted(({k: (int(v) if k == "ts" else float(v)) for k, v in b.items() if k in ("ts", "open", "high", "low", "close")}
                   for b in bars), key=lambda b: b["ts"])
    tick = event.get("source") == TICK_SOURCE
    if tick:
        if not isinstance(run.get("positions"), list):
            raise RuntimeError("a tick run carries no `positions` - the risk manager passes this playbook's open positions")
        return event, run, bars, None, tick
    detection = event.get("detection")
    if not isinstance(detection, dict):
        raise RuntimeError(f"an alert run carries no `detection` - keys present: {sorted(event)}")
    missing = [k for k in REQUIRED_DETECTION if detection.get(k) is None]
    if missing:
        raise RuntimeError(f"detection is missing {missing} - keys present: {sorted(detection)}")
    return event, run, bars, detection, tick


def _send(sig, answers):
    if not TELEGRAM_ENABLED and sig.get("message"):
        logger.info("WOULD SEND (shadow) %s: %s", sig["kind"], sig["message"].replace(chr(10), " | "))
        sig = dict(sig, message=None)
    a = ordermgr.send(sig)
    answers.append((sig, a))
    logger.info("order-manager: %s %s entry %s candle %s -> %s (%s)%s", sig["kind"], sig["direction"], clock(sig["entry_ts"]),
                clock(sig["candle_ts"]), a["result"], a.get("why"), " - message sent" if a.get("sent") else "")
    return a


def _session_start(ts):
    d = (datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).date()
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).timestamp())


def _entry(event, run, bars, detection):
    trigger_ts = int(detection["trigger_ts"])
    label = (f"{detection['family']}{'/' + detection['subtype'] if detection.get('subtype') else ''} "
             f"{detection['direction']} @{detection['level']} candle {clock(trigger_ts)}, day trend {detection['day_trend']}")
    result = evaluate(detection, bars, event.get("daily"))
    if result["status"] != "candidate":
        for reason in result["reasons"]:
            logger.info("stand down (%s): %s", label, reason)
        return {"status": "stand_down", "alert": label, "reasons": result["reasons"], "mode": run["mode"]}
    day = _session_start(trigger_ts)
    date = datetime.datetime.fromtimestamp(day + 19800, datetime.timezone.utc).date().isoformat()
    text = render(result, detection, event["snapshot"], f"{date} {clock(trigger_ts)}")
    sig = record.entry_signal(result, detection, event["instrument"], day, POSITION_PLAYBOOK, text, run["mode"],
                              run.get("why") or run.get("force") or "")
    answers = []
    a = _send(sig, answers)
    return {"status": "candidate", "mode": run["mode"], "kind": sig["kind"], "result": a["result"], "sent": a.get("sent"),
            "alert": label, "side": result["side"], "entry": result["entry"], "stop_line": result["off_line"],
            "risk": round(result["risk"], 2), "t1": sig["t1_target"]}


def _exits(run, bars):
    newest = int(bars[-1]["ts"])
    answers = []
    forced = 0
    for row in run["positions"]:
        status = row["status"]
        for sig in record.pending(row, bars, newest, closing=run["mode"] == "force_exit"):
            status = _send(sig, answers).get("status") or status
        if run["mode"] == "force_exit":
            close = record.forced_close(row, bars, run["force"], run.get("why"), status)
            if close is not None:
                _send(close, answers)
                forced += 1
    by = {}
    for _, a in answers:
        by[a["result"]] = by.get(a["result"], 0) + 1
    return {"status": "exit_mode", "mode": run["mode"], "force": run.get("force"), "positions": len(run["positions"]),
            "sent": len(answers), "forced": forced, "results": by}


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    event, run, bars, detection, tick = read_event(event or {})
    out = _exits(run, bars) if tick else _entry(event, run, bars, detection)
    logger.info("done in %.2fs: %s (%s, %d index candles)", time.monotonic() - started, out,
                event["instrument"].get("trading_symbol"), len(bars))
    return out
