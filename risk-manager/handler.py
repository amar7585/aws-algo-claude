"""
Risk Manager - AWS Lambda function

THE ONE RISK MANAGER, IN FRONT OF strategy-manager (Amar 2026-11-22: "it will be new lambda function which
will connect strategy manager so it can check the open position and prevent duplicate entry or execute
correct function like entry or exit function also risk manager can prevent execution of stratergy manager
flow if kill switch activated"):

    intraday-fno-poller -> pattern-recorder --(Event: each alert once, then the tick)--> THIS
    THIS --(Event: the same payload + `run`)--> strategy-manager --(Event, context v4)--> the playbooks

On every alert and tick it reads algo.risk_control and today's algo.position_master, and:
  * KILL SWITCH ON (the session it was set in, else the next): closes every open or watching position itself,
    through order-manager (KILL SWITCH EXIT, one message each), and does NOT invoke strategy-manager -
    no playbook runs. Data capture is untouched.
  * otherwise decides each playbook's mode (rules.py): entry_exit / exit_only / force_exit - the hard rules
    (no new trade after 15:00, the 15:15 time exit, the limits) - and hands the payload on with `run`.

It writes no table. It places no order. The playbooks keep their own entry and exit strategies and close
their own positions on its instruction (so a stop or target inside the 15:10 candle still comes first).

FAIL LOUDLY: a failure here means strategy-manager is not invoked and no playbook runs that tick - the safe
side for entries; the next tick's playbook run catches up the missed steps. error-notifier reports it.

THE DAILY-LOSS LIMIT IN OPTION RUPEES (Amar 2026-11-28, build plan approved): it reads the session's paper
option rows (algo.option_paper_position, written by order-manager) and, while any option is open, makes ONE
batched Dhan quote (a data read - no order, no static IP) to mark them live; a quote failure raises, so that
tick invokes no playbook (the next tick catches up).

Modules:
    rules.py     the decision (pure)
    db.py        the reads (control, positions, holidays, the paper options)
    dhan.py      the one batched quote of the open paper options
    params.py    the Dhan token and client id (auth-dhan-broker's /algo/dhan/token)
    ordermgr.py  the synchronous call to order-manager (kill-switch closes only)
    config.py    the run version, the playbook lists, the hard-rule times
"""

import json
import logging
import time

import boto3
from neon_access import connect, ist_datetime, ist_midnight_epoch, read_neon_connection_string

import db
import ordermgr
import rules
from config import (ALERT_PLAYBOOKS, ALERT_SOURCE, INVOCATION_TYPE, MAX_PAYLOAD_BYTES, STRATEGY_MANAGER_FUNCTION,
                    TICK_PLAYBOOKS, TICK_SOURCE)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
_lambda = {}


def read_event(event):
    if not isinstance(event, dict):
        raise ValueError(f"expected pattern-recorder's payload object, got {type(event).__name__}")
    source = event.get("source")
    if source not in (TICK_SOURCE, ALERT_SOURCE):
        raise ValueError(f"source {source!r} - risk-manager takes pattern-recorder's alerts and ticks only")
    if "run" in event:
        raise ValueError("the payload already carries `run` - risk-manager must be the only one to add it")
    inst = event.get("instrument")
    if not isinstance(inst, dict) or not inst.get("security_id") or not inst.get("instrument_type"):
        raise ValueError("payload carries no usable `instrument`")
    bars = event.get("index_bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("payload carries no `index_bars` - the marks and the hard-rule clock need today's closed candles")
    return source, inst, sorted(bars, key=lambda b: int(b["ts"]))


def invoke_manager(payload, client=None):
    lam = client or _lambda.setdefault("c", boto3.client("lambda"))
    body = json.dumps(payload, default=float).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise RuntimeError(f"payload {len(body)} bytes exceeds the {MAX_PAYLOAD_BYTES} async limit")
    resp = lam.invoke(FunctionName=STRATEGY_MANAGER_FUNCTION, InvocationType=INVOCATION_TYPE, Payload=body)
    if resp.get("StatusCode") not in (200, 202) or resp.get("FunctionError"):
        raise RuntimeError(f"invoking {STRATEGY_MANAGER_FUNCTION} returned {resp.get('StatusCode')} {resp.get('FunctionError')}")


def make_dhan():
    """A Dhan data client on auth-dhan-broker's token (read from SSM only when an open option needs a mark)."""
    from dhan import DhanClient
    from params import read_client_id, read_token_record
    record = read_token_record()
    return DhanClient(record["access_token"], read_client_id(record))


def option_marks(options, dhan_factory=make_dhan):
    """ONE batched live quote of every open paper option (2026-11-28) - none when no option is open. Raises on failure."""
    open_ = [o for o in options if int(o["open_units"]) > 0]
    if not open_:
        return {}
    by_seg = {}
    for o in open_:
        by_seg.setdefault(o["exchange_segment"], []).append(o["option_security_id"])
    client = dhan_factory()
    marks = {}
    for seg, ids in by_seg.items():
        marks.update(client.last_prices(seg, ids))
    return marks


def decide(event, now, conn, send=None, dhan_factory=make_dhan):
    """The whole decision on an open connection (closed by the caller). Returns (summary, payload_or_None, closes)."""
    source, inst, bars = read_event(event)
    session_start = ist_midnight_epoch(ist_datetime(bars[-1]["ts"]).date())
    control = db.read_control(conn, now)
    holidays = db.read_holidays(conn)
    positions = db.read_positions(conn, inst, session_start)
    prev_close = rules.previous_session_close(session_start, holidays)
    if rules.kill_switch_on(control, prev_close):
        closes = rules.kill_switch_closes(positions, bars)
        return {"kill_switch": True, "control_set_at": control["set_at"], "positions": len(positions),
                "closing": len(closes)}, None, closes
    # The daily-loss limit in option rupees: the session's paper option rows + live marks of the open ones.
    options = db.read_options(conn, [p["position_id"] for p in positions if p["status"] != rules.REFUSED])
    marks = option_marks(options, dhan_factory)
    a = rules.assess(positions, bars, control, options, marks)
    run = rules.build_run(source, a, positions, TICK_PLAYBOOKS, ALERT_PLAYBOOKS)
    payload = dict(event, run=run)
    summary = {"kill_switch": False, "control_set_at": control["set_at"], "assessment": a,
               "modes": {k: v["mode"] + (f" ({v['force']})" if v.get("force") else "") for k, v in run["playbooks"].items()}}
    return summary, payload, []


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    now = int(time.time())
    conn = connect(read_neon_connection_string())
    try:
        summary, payload, closes = decide(event, now, conn)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    stamp = ist_datetime(int(sorted(event["index_bars"], key=lambda b: int(b["ts"]))[-1]["ts"])).strftime("%Y-%m-%d %H:%M")
    if summary["kill_switch"]:
        answers = [ordermgr.send(s) for s in closes]
        logger.info("%s: KILL SWITCH ON (row set_at %s) - %d position(s) closed %s; strategy-manager NOT invoked",
                    stamp, summary["control_set_at"], len(closes), [a["result"] for a in answers])
        summary["results"] = [a["result"] for a in answers]
    else:
        invoke_manager(payload)
        a = summary["assessment"]
        logger.info("%s %s: %s | open %d, trades %d, pnl %+.1f point-lots, option %+.0f rupees%s%s -> %s", stamp,
                    event.get("source"), summary["modes"], a["open"], a["trades"], a["pnl_point_lots"], a["option_rupees"],
                    f", force {a['force']}" if a["force"] else "", f", why: {'; '.join(a['why'])}" if a["why"] else "",
                    STRATEGY_MANAGER_FUNCTION)
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return summary
