"""
Range Liquidity Sweep - AWS Lambda function

Scans a range-bound session's 5-minute bars for the 15-minute opening-range
liquidity sweep - price runs a pool of resting stops, fails to hold beyond it,
closes back inside, and rotates back across the range - and reports the
candidates with entry, stop, targets and risk-reward.

INVOKED BY strategy-manager, NOT BY A SCHEDULE. The manager is a pure
router: it reads the regime and bias off the snapshot that
intraday-market-sentiment already classified and stored, and invokes this
function only for the combinations its registry allows. It hands over two rows
and an instrument - the snapshot (which CARRIES its own classification) and the
daily read - and nothing else.

THIS FUNCTION FETCHES ITS OWN BARS. A playbook knows which bars it needs; the
manager used to fetch a fixed window on every playbook's behalf and guess at
the size. It reads NEON, not Dhan: the only thing the manager ever called Dhan
for was a live price, and this playbook never read it - a sweep is confirmed by
a CLOSED bar reclaiming a level, so an unconfirmed live tick is precisely what
the setup must not act on. That is why there is no token here, no SSM read and
no rate-limit budget.

Reading intraday-data-loader's tables is safe HERE in a way it is not in
intraday-market-sentiment: this function persists nothing, so a stale bar costs
one scan that the next run corrects, rather than a snapshot row that is never
revisited.

WHAT IT PRODUCES. Log output, and nothing else. It writes no table, emits no
signal, places no order and sizes nothing - the playbook rules the last two out
explicitly, and the manager's regime decision is recorded here, in the
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
    db.py       the Neon reads - the bars and the daily series. No writes.
    levels.py   the liquidity pools, and which of them stack
    sweep.py    detection, acceptance, and what became of each attempt
    trade.py    entry, stop, targets, risk-reward, grade
    gate.py     the fine gate - it stops the scan, it does not soften it
    report.py   the output blocks the playbook specifies
"""

import logging

from market_classifier import wilder_atr
from neon_access import connect, ist_midnight_epoch, read_neon_connection_string

import gate as gate_lib
import report
import sweep as sweep_lib
import trade as trade_lib
from notify import send_telegram
from config import (
    ATR_PERIOD,
    CANDLE_INTERVAL_MINUTES,
    EXPECTED_CONTEXT_VERSION,
    HISTORY_5MIN_BARS,
    HISTORY_DAILY_BARS,
    MAX_ATTEMPTS_PER_SIDE,
)
from db import closed_candles, daily_candles
from levels import HIGH, LOW, build_pools, ist_datetime, session_bars, stacked_with

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def read_context(event):
    """
    Validate the manager's payload and pull out what this playbook needs.

    THE VERSION IS ASSERTED, NOT COPED WITH. A context that has moved on would
    have this function reading a key that is no longer there, getting None, and
    gating on it - and a gate that passes because its input vanished is the
    worst failure this playbook can have. Better to fail the invocation.
    """
    if not isinstance(event, dict):
        raise RuntimeError(
            f"expected strategy-manager's context object, got "
            f"{type(event).__name__}"
        )
    version = event.get("context_version")
    if version != EXPECTED_CONTEXT_VERSION:
        raise RuntimeError(
            f"context_version {version!r} but this function is written against "
            f"v{EXPECTED_CONTEXT_VERSION} - strategy-manager's payload has "
            f"changed and the gate inputs cannot be trusted. Keys present: "
            f"{sorted(event)}"
        )

    for field in ("snapshot", "instrument"):
        if not isinstance(event.get(field), dict):
            raise RuntimeError(f"context carries no `{field}` object")

    # THE CLASSIFICATION IS ON THE SNAPSHOT NOW, not in a block beside it.
    # intraday-market-sentiment runs the shared market-classifier layer and
    # stores the result as columns, so a snapshot without them is a row
    # written before that wiring - and the gate below would read None for
    # regime and stand down for the wrong reason.
    missing = [
        key for key in ("snapshot_ts", "regime", "bias", "sma100")
        if event["snapshot"].get(key) is None
    ]
    if missing:
        raise RuntimeError(
            f"snapshot is missing {missing} - the classification columns are "
            f"written by intraday-market-sentiment via the market-classifier "
            f"layer, and the gate cannot run without them"
        )
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
    # The classification IS the snapshot - regime, bias and sma100 are columns
    # on the row, not a separate block. Passed under its own name so the gate
    # and the walk read from one object rather than two views of it.
    classification = snapshot
    instrument = ctx["instrument"]
    daily = ctx.get("daily")
    snapshot_ts = int(snapshot["snapshot_ts"])
    session_day = ist_datetime(snapshot_ts).date()

    # ---- the reads ---------------------------------------------------------
    #
    # as_of IS snapshot_ts, AND THAT IS EXACTLY RIGHT under the current grain.
    # snapshot_ts names the bar that was still FORMING when the snapshot was
    # taken, so `candle_ts + interval <= snapshot_ts` selects every bar that
    # had CLOSED at that instant and excludes the forming one. A sweep is
    # confirmed by a closed bar reclaiming a level, so the forming bar is
    # precisely what must not be scanned.
    #
    # Bounding on the snapshot rather than the wall clock is what makes a
    # re-run meaningful: two runs over the same snapshot read the same bars and
    # reach the same answer, rather than merely repeating.
    conn = connect(read_neon_connection_string())
    try:
        all_bars = closed_candles(
            conn, CANDLE_INTERVAL_MINUTES, instrument, snapshot_ts,
            HISTORY_5MIN_BARS,
        )
        # True ATR over daily bars, for the "how much of the day's range is
        # already spent" gate. Bounded before today's midnight: today's daily
        # candle does not exist yet at any point during the session.
        atr14 = wilder_atr(
            daily_candles(
                conn, instrument, ist_midnight_epoch(session_day),
                HISTORY_DAILY_BARS,
            ),
            period=ATR_PERIOD,
        )
        daily_atr14 = next((v for v in reversed(atr14) if v is not None), None)
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass

    bars = session_bars(all_bars, session_day)
    if not bars:
        raise RuntimeError(
            f"none of the {len(all_bars)} closed bars read fall in the "
            f"{session_day} session - intraday-data-loader has not written "
            f"today's candles, or has stalled"
        )

    pools, orb = build_pools(bars, previous_day=daily, snapshot=snapshot)

    passed, failures, details = gate_lib.evaluate(
        snapshot, classification, daily_atr14, orb, snapshot_ts
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

    # ---- the signal reaches the phone -------------------------------------
    #
    # A live candidate is a real trade signal. Push it to Telegram, but ONLY
    # when its trigger bar is FRESH - closed within the last snapshot interval -
    # so a signal that stays live across several 15-minute re-derivations is
    # announced once, not on every run. Fail-loud: a refused send raises so
    # Lambda records it, matching "definitely send on signal generation".
    fresh_after = int(snapshot.get("prev_snapshot_ts") or (snapshot_ts - 900))
    for record in live:
        event = record["event"]
        index = event.get("confirm_index", event["sweep_index"])
        trigger_ts = int(bars[index]["ts"])
        if not (fresh_after < trigger_ts <= snapshot_ts):
            continue
        candidate = record["candidate"]
        send_telegram(
            f"SWEEP SIGNAL {candidate['direction'].upper()} "
            f"{event['pool']['name']} - "
            f"{ist_datetime(snapshot_ts).strftime('%Y-%m-%d %H:%M')}\n"
            f"sweep {event['sweep_time']}, entry {candidate['entry']} "
            f"stop {candidate['stop']}\n"
            f"T1 {candidate['t1']} T2 {candidate['t2']} "
            f"RR {candidate['risk_reward']} ({candidate['grade']})"
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
