"""
Range Liquidity Sweep - AWS Lambda function

Scans a range-bound session's 5-minute bars for the 15-minute opening-range
liquidity sweep - price runs a pool of resting stops, fails to hold beyond it,
closes back inside, and rotates back across the range - and reports the
candidates with entry, stop, targets and risk-reward.

INVOKED BY strategy-orchestrator, NOT BY A SCHEDULE. The orchestrator classifies
the 15-minute regime and only invokes this function when that regime is RANGE,
handing over the whole context in the payload: the snapshot, the daily read,
the classification, the candle history and a live price. This function opens no
database connection and makes no API call of its own - which is why it carries
no layers at all.

WHAT IT PRODUCES. Log output, and nothing else. It writes no table, emits no
signal, places no order and sizes nothing - the playbook rules the last two out
explicitly, and the orchestrator's regime decision is recorded here, in the
consumer, rather than by the producer. Reading this function's log is how a
session's routing and its candidates are recovered.

IT HAS NO MEMORY, AND DOES NOT NEED ANY. The playbook caps attempts at one
re-entry per side per session and wants the running cost of an idea reported,
which looks like state across invocations. It is not: a candidate is a
deterministic function of the bars, so re-deriving the day's whole sweep
sequence on every run reproduces the attempt count exactly. A re-run reaches
the same answer rather than double-counting.

Modules:
    config.py   every threshold the playbook states, and the ones it does not
    levels.py   the liquidity pools, and which of them stack
    sweep.py    detection, acceptance, and what became of each attempt
    trade.py    entry, stop, targets, risk-reward, grade
    gate.py     the fine gate - it stops the scan, it does not soften it
    report.py   the output blocks the playbook specifies
"""

import logging

import gate as gate_lib
import report
import sweep as sweep_lib
import trade as trade_lib
from config import (
    EXPECTED_CONTEXT_VERSION,
    MAX_ATTEMPTS_PER_SIDE,
)
from levels import HIGH, LOW, build_pools, ist_datetime, session_bars, stacked_with

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def read_context(event):
    """
    Validate the orchestrator's payload and pull out what this playbook needs.

    THE VERSION IS ASSERTED, NOT COPED WITH. A context that has moved on would
    have this function reading a key that is no longer there, getting None, and
    gating on it - and a gate that passes because its input vanished is the
    worst failure this playbook can have. Better to fail the invocation.
    """
    if not isinstance(event, dict):
        raise RuntimeError(
            f"expected strategy-orchestrator's context object, got "
            f"{type(event).__name__}"
        )
    version = event.get("context_version")
    if version != EXPECTED_CONTEXT_VERSION:
        raise RuntimeError(
            f"context_version {version!r} but this function is written against "
            f"v{EXPECTED_CONTEXT_VERSION} - strategy-orchestrator's payload has "
            f"changed and the gate inputs cannot be trusted. Keys present: "
            f"{sorted(event)}"
        )

    for field in ("snapshot", "classification", "candles"):
        if not isinstance(event.get(field), dict):
            raise RuntimeError(f"context carries no `{field}` object")

    bars = event["candles"].get("bars")
    if not bars:
        raise RuntimeError("context carries no candle bars to scan")
    return event


def walk_session(bars, pools, orb, vwap, sma100, bias, previous_day, window):
    """
    Replay the session's sweeps in order, applying the attempt rules as it goes.

    In order, because the rules are sequential: whether a sweep is a first
    attempt or a re-entry depends on what happened to the earlier one, and
    whether it is allowed at all depends on whether the range has since broken.
    Evaluating them independently would let a session produce three first
    attempts on the same side.
    """
    events = sweep_lib.detect(bars, pools)

    # Where the range itself broke, if it did. Measured on the opening range's
    # own boundaries - those two levels ARE the range - and independently of
    # whether an attempt was ever taken, because the stand-down applies either
    # way.
    broke_at = None
    if orb:
        for level, side in ((orb["high"], HIGH), (orb["low"], LOW)):
            at = sweep_lib.acceptance_at(bars, level, side)
            if at is not None:
                broke_at = at if broke_at is None else min(broke_at, at)

    attempts = {HIGH: [], LOW: []}
    candidates, rejections = [], []

    for event in events:
        if event["reject_reason"]:
            rejections.append((event, event["reject_reason"]))
            continue

        index = event.get("confirm_index", event["sweep_index"])
        if broke_at is not None and index >= broke_at:
            rejections.append(
                (
                    event,
                    f"the range broke at "
                    f"{ist_datetime(bars[broke_at]['ts']).strftime('%H:%M')} - "
                    f"a level was accepted through, so neither side is a range "
                    f"trade any more",
                )
            )
            continue

        side = event["side"]
        price = event["level"]
        stacked = stacked_with(event["pool"], pools, price)

        # The late window takes a setup only with a stacked pool and a near
        # target.
        if window == gate_lib.WINDOW_LATE and not stacked:
            rejections.append(
                (event, "after 14:30 only a stacked-pool sweep qualifies")
            )
            continue

        attempt_no = len(attempts[side]) + 1
        if attempt_no > MAX_ATTEMPTS_PER_SIDE:
            rejections.append(
                (
                    event,
                    f"attempt {attempt_no} on the {side} side - the cap is "
                    f"{MAX_ATTEMPTS_PER_SIDE} per side per session, and a second "
                    f"failure closes that side for the day",
                )
            )
            continue

        if attempt_no == 1:
            candidate, reason = trade_lib.build(
                event, bars, orb, vwap, sma100, bias, stacked, previous_day
            )
        else:
            previous = attempts[side][-1]
            if previous["outcome"]["outcome"] != sweep_lib.CASE_B:
                rejections.append(
                    (
                        event,
                        f"the first attempt on this side ended "
                        f"{previous['outcome']['outcome']}, and a re-entry is "
                        f"allowed only after Case B - an extended sweep with no "
                        f"acceptance",
                    )
                )
                continue
            candidate, reason = trade_lib.re_entry(
                event, bars, orb, vwap, sma100, bias, stacked,
                previous["candidate"]["t2"], previous_day,
            )

        if candidate is None:
            rejections.append((event, reason))
            continue

        if window == gate_lib.WINDOW_LATE and candidate["t1"] is None:
            rejections.append(
                (event, "after 14:30 a near target is required and there is no T1")
            )
            continue

        result = sweep_lib.outcome(bars, event, candidate)
        record = {
            "event": event,
            "candidate": candidate,
            "stacked": stacked,
            "outcome": result,
            "attempt": attempt_no,
            # The first attempt on THIS side, for the re-entry report's
            # running cost. Not the first of the session: with a sweep of the
            # low faded before a sweep of the high, the session's first
            # candidate belongs to the other side entirely and reporting its
            # entry as "first attempt" would state the wrong number.
            "first_on_side": attempts[side][0] if attempts[side] else None,
        }
        attempts[side].append(record)
        candidates.append(record)

    return candidates, rejections, broke_at


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    ctx = read_context(event)

    snapshot = ctx["snapshot"]
    classification = ctx["classification"]
    daily = ctx.get("daily")
    snapshot_ts = int(snapshot["snapshot_ts"])
    session_day = ist_datetime(snapshot_ts).date()

    bars = session_bars(ctx["candles"]["bars"], session_day)
    if not bars:
        raise RuntimeError(
            f"none of the {len(ctx['candles']['bars'])} bars supplied fall in "
            f"the {session_day} session - the orchestrator and this function "
            f"disagree about which day is being scanned"
        )

    pools, orb = build_pools(bars, previous_day=daily, snapshot=snapshot)

    passed, failures, details = gate_lib.evaluate(
        snapshot, classification, ctx.get("daily_atr14"), orb, snapshot_ts
    )
    if not passed:
        logger.info(report.stand_down_block(failures, details))
        return {
            "status": "stood-down",
            "snapshot": ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
            "gate": "failed",
            "failures": failures,
            "details": details,
            "candidates": 0,
        }

    logger.info(
        "gate passed: %s window, regime %s/%s, adr %s of atr14 %s (%s), "
        "opening range %s (%s%%)",
        details["window"], details["regime"], details["bias"], details["adr"],
        details["atr14"], details["range_used"], details["orb_range"],
        details["orb_range_pct"],
    )
    logger.info(
        "%d pools: %s",
        len(pools),
        ", ".join(f"{pool['name']} {pool['price']:.2f}" for pool in pools),
    )

    candidates, rejections, broke_at = walk_session(
        bars,
        pools,
        orb,
        snapshot.get("vwap"),
        classification.get("sma100"),
        classification.get("bias"),
        daily,
        details["window"],
    )

    # The live ones are the attempts the following bars have not resolved yet.
    # A resolved attempt is history - useful for the attempt count and the
    # running cost, not something to act on.
    live = [
        record
        for record in candidates
        if record["outcome"]["outcome"] == sweep_lib.OPEN
    ]

    for record in candidates:
        block = report.candidate_block(
            record["event"],
            record["candidate"],
            record["stacked"],
            report.invalidation(record["event"]),
        )
        logger.info(
            "%s\nOutcome so far: %s (%d bars on)\n",
            block,
            record["outcome"]["outcome"],
            record["outcome"]["bars_after"],
        )
        if record["attempt"] > 1 and record["first_on_side"]:
            first = record["first_on_side"]["candidate"]
            logger.info(
                report.re_entry_block(
                    record["event"], record["candidate"], record["stacked"],
                    first, record["first_on_side"]["outcome"],
                    first["risk"] + record["candidate"]["risk"],
                )
            )

    logger.info(report.rejected_block(rejections))
    if not candidates:
        logger.info(
            "No setup today so far. %d attempt(s) examined against %d pools.",
            len(rejections), len(pools),
        )

    return {
        "status": "success",
        "snapshot": ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        "gate": "passed",
        "window": details["window"],
        "regime": details["regime"],
        "bias": details["bias"],
        "bars_scanned": len(bars),
        "pools": [pool["name"] for pool in pools],
        "range_broke_at": (
            ist_datetime(bars[broke_at]["ts"]).strftime("%H:%M")
            if broke_at is not None
            else None
        ),
        "candidates": len(candidates),
        "live_candidates": [
            {
                "direction": record["candidate"]["direction"],
                "time": record["event"]["sweep_time"],
                "pool": record["event"]["pool"]["name"],
                "entry": record["candidate"]["entry"],
                "stop": record["candidate"]["stop"],
                "t1": record["candidate"]["t1"],
                "t2": record["candidate"]["t2"],
                "risk_reward": record["candidate"]["risk_reward"],
                "grade": record["candidate"]["grade"],
                "attempt": record["attempt"],
            }
            for record in live
        ],
        "rejected": len(rejections),
    }
