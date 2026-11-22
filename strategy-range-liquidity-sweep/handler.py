"""
Range Liquidity Sweep - AWS Lambda function

Scans a range-bound session's 5-minute bars for the 15-minute opening-range
liquidity sweep - price runs a pool of resting stops, fails to hold beyond it,
closes back inside, and rotates back across the range - and reports the
candidates with entry, stop, targets and risk-reward.

INVOKED BY strategy-manager, NOT BY A SCHEDULE. The manager is a pure
router. Since 2026-11-14 it sends every 5-min tick here whatever the regime
(TICK_PLAYBOOKS); before that it invoked this function only for the
regime|bias combinations its registry allowed (registry and bias removed
2026-11-19). It hands over the snapshot
(which CARRIES its own classification), the daily read, the instrument and,
on every-tick runs, today's closed candles.

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

WHAT IT PRODUCES. Log output and Telegram messages - and, since 2026-11-14, the
alert register (below), a record nothing reads back. It places no order and sizes nothing - the playbook rules the last two out
explicitly, and the manager's regime decision is recorded here, in the
consumer, rather than by the producer. Reading this function's log is how a
session's routing and its candidates are recovered.

IT HAS NO MEMORY, AND DOES NOT NEED ANY. The playbook caps attempts at one
re-entry per side per session and wants the running cost of an idea reported,
which looks like state across invocations. It is not: a candidate is a
deterministic function of the bars, so re-deriving the day's whole sweep
sequence on every run reproduces the attempt count exactly. A re-run reaches
the same answer rather than double-counting.

SINCE 2026-11-14 IT RUNS ON EVERY 5-MIN TICK. pattern-recorder hands each tick
to strategy-manager, which sends it here whatever the regime, with today's
closed 5-min index candles (`index_bars`) - so this function reads those, not
candle_5min (which the loader refreshes only every 15 minutes). Each run
re-derives the session candle by candle (session.py) and sends to Telegram
every entry and trade step on the newest candle. (Until 2026-11-19 an older
rule set ran beside these and each message was tagged with the set that
produced it; Amar removed the old set on 2026-11-19.) The rules also follow
each trade after entry - T1 (2 of 3 lots booked at the range midpoint, since
2026-11-22), exit, watch, re-entry, end (manage.py) - which is why it must run
on every tick. Entries need price within the levels (PDH/PDL, 15-min high/low)
and a regime that is not volatile-expansion - no bias (2026-11-14), no trend
test (2026-11-15). Amar: a sweep is a level-and-volatility event.

SINCE 2026-11-14 IT ALSO RECORDS AN ALERT REGISTER (register.py), when
REGISTER_ENABLED is on: every level touched today, its
retests, and whether it was entered, blocked by the gate, missed, invalid, ended
or covered - rebuilt whole on every run and upserted into algo.playbook_alerts
after the messages. Nothing reads it back (Amar: "re-derive and record").

Modules:
    session.py  the session candle by candle, and the messages
    manage.py   the trade after entry: T1, exit, watch, re-entry
    register.py the alert register - what became of every level touched today
    config.py   every threshold the playbook states, and the ones it does not
    db.py       the Neon reads, and the register's one write
    levels.py   the liquidity pools, and which of them stack
    sweep.py    detection, acceptance, and what became of each attempt
    trade.py    entry, stop, targets, risk-reward, grade
    gate.py     the fine gate - it stops the scan, it does not soften it
    report.py   the output blocks the playbook specifies
"""

import logging
import time

from market_classifier import wilder_atr
from neon_access import connect, ist_midnight_epoch, read_neon_connection_string

import register as register_lib
import report
import session as session_lib
from notify import send_telegram
from config import (
    ATR_PERIOD,
    CANDLE_INTERVAL_MINUTES,
    CANDLE_INTERVAL_SECONDS,
    EXPECTED_CONTEXT_VERSION,
    HISTORY_5MIN_BARS,
    HISTORY_DAILY_BARS,
    REGISTER_ENABLED,
    REGISTER_PLAYBOOK,
    ROW_VISIBLE_LAG_SECONDS,
    TELEGRAM_ENABLED,
    TICK_SOURCE,
)
from db import closed_candles, daily_candles, day_rows, write_register
from levels import ist_datetime, session_bars

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
        key for key in ("snapshot_ts", "regime")
        if event["snapshot"].get(key) is None
    ]
    if missing:
        raise RuntimeError(
            f"snapshot is missing {missing} - the classification columns are "
            f"written by intraday-market-sentiment via the market-classifier "
            f"layer, and the gate cannot run without them"
        )
    bars = event.get("index_bars")
    if bars is not None and not isinstance(bars, list):
        raise RuntimeError(f"`index_bars` must be a list, got {type(bars).__name__}")
    return event


def _bars_from_payload(index_bars):
    return [{"ts": int(b["ts"]), "open": float(b["open"]), "high": float(b["high"]),
             "low": float(b["low"]), "close": float(b["close"]), "volume": 0} for b in index_bars]


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    ctx = read_context(event)
    snapshot = ctx["snapshot"]
    instrument = ctx["instrument"]
    daily = ctx.get("daily")
    snapshot_ts = int(snapshot["snapshot_ts"])
    session_day = ist_datetime(snapshot_ts).date()
    day_start = ist_midnight_epoch(session_day)
    tick = ctx.get("source") == TICK_SOURCE
    index_bars = ctx.get("index_bars")

    # ---- the reads -----------------------------------------------------------
    # Every-tick payloads carry today's CLOSED candles (pattern-recorder fetched
    # them from Dhan for this tick). Without them - an alert hand-off - the
    # stored candles closed by the snapshot are read, as before.
    #
    # ONE CONNECTION FOR THE WHOLE RUN. Since 2026-11-14 the run also writes the
    # alert register (after its messages), so the connection the reads opened
    # stays open until then instead of a second connect - Neon is in Singapore,
    # and every connect is a fresh TLS handshake across the region.
    conn = connect(read_neon_connection_string())
    try:
        return _run(conn, ctx, snapshot, instrument, daily, snapshot_ts, session_day, day_start, tick, index_bars)
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass


def _run(conn, ctx, snapshot, instrument, daily, snapshot_ts, session_day, day_start, tick, index_bars):
    if index_bars:
        all_bars = _bars_from_payload(index_bars)
    else:
        all_bars = closed_candles(conn, CANDLE_INTERVAL_MINUTES, instrument, snapshot_ts, HISTORY_5MIN_BARS)
    bars = session_bars(all_bars, session_day)
    if not bars:
        raise RuntimeError(
            f"none of the {len(all_bars)} closed bars read fall in the {session_day} session - "
            + ("the payload carried no session candles" if index_bars
               else "intraday-data-loader has not written today's candles, or has stalled"))
    as_of = int(bars[-1]["ts"]) + CANDLE_INTERVAL_SECONDS
    atr14 = wilder_atr(daily_candles(conn, instrument, day_start, HISTORY_DAILY_BARS), period=ATR_PERIOD)
    daily_atr14 = next((v for v in reversed(atr14) if v is not None), None)
    rows = day_rows(conn, instrument, day_start, as_of + ROW_VISIBLE_LAG_SECONDS)

    result = session_lib.derive(bars, rows, daily, daily_atr14)

    # ---- the log: the newest candle --------------------------------------------
    newest = result["contexts"][-1]
    stamp = ist_datetime(as_of).strftime("%Y-%m-%d %H:%M")
    if newest["entry"]:
        logger.info("%s: entries allowed - %s window, regime %s, day trend %s", stamp, newest["window"],
                    newest["row"]["regime"], newest["row"].get("day_trend"))
    else:
        logger.info("%s: no new entries - %s", stamp,
                    "; ".join(newest["cell_failures"] + newest["failures"]) or newest["window"])
    logger.info("%d pools: %s", len(newest["pools"]),
                ", ".join(f"{p['name']} {p['price']:.2f}" for p in newest["pools"]))
    if result["rejected"].get(len(bars) - 1):
        logger.info(report.rejected_block(result["rejected"][len(bars) - 1]))
    for idea in result["ideas"]:
        logger.info("new-logic idea %s %s from %s: %s, %d step(s)%s", idea["candidate"]["direction"],
                    idea["event"]["pool"]["name"], ist_datetime(bars[idea["n"]]["ts"] + 300).strftime("%H:%M"),
                    idea["state"], len(idea["steps"]),
                    "".join(f"; {s['kind']} {ist_datetime(bars[s['index']]['ts'] + 300).strftime('%H:%M')}"
                            for s in idea["steps"]))

    # ---- the messages ------------------------------------------------------------
    # Only what happened on the newest candle (every-tick runs), or within the
    # snapshot's window (an alert hand-off), is announced - each step once, on
    # the run whose newest candle it is.
    if tick:
        fresh_after = int(bars[-1]["ts"]) - 1
    else:
        # The candles that CLOSED since the previous snapshot: stamped after
        # prev_snapshot_ts minus one interval. (Before 2026-11-14 this compared
        # against prev_snapshot_ts itself, so the first candle of every 15-min
        # window - the one that closes after the previous snapshot - was never
        # announced.)
        fresh_after = int(snapshot.get("prev_snapshot_ts") or (snapshot_ts - 900)) - CANDLE_INTERVAL_SECONDS
    out = session_lib.messages(result, bars, fresh_after, int(bars[-1]["ts"]))
    for kind, text in out:
        if TELEGRAM_ENABLED:
            # Fail-loud: a refused send raises so Lambda records it.
            send_telegram(text)
            logger.info("%s sent: %s", kind, text.replace("\n", " | "))
        else:
            logger.info("WOULD SEND (telegram off) %s: %s", kind, text.replace("\n", " | "))

    # ---- the alert register - AFTER the messages ---------------------------------
    # Rebuilt whole from this run's derivation plus the same rules with the gate
    # open (for `blocked` rows), and upserted on this connection. A failed write
    # raises: the messages are already out, so a retry may repeat them - a
    # duplicate is visible, a lost signal is not.
    register_summary = {"enabled": REGISTER_ENABLED}
    if REGISTER_ENABLED:
        ungated = session_lib.derive(bars, rows, daily, daily_atr14, gated=False, contexts=result["contexts"])
        recs = register_lib.records(bars, result, ungated, daily)
        written = write_register(conn, instrument, REGISTER_PLAYBOOK, day_start, recs, int(time.time()),
                                 derived_through_ts=int(bars[-1]["ts"]))
        by_status = {}
        for r in recs:
            by_status[f"{r['logic']} {r['status']}"] = by_status.get(f"{r['logic']} {r['status']}", 0) + 1
        register_summary.update(rows=len(recs), by_status=by_status, **written)
        logger.info("register: %d alerts today - %s", len(recs),
                    ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "none yet")

    return {
        "status": "success",
        "as_of": stamp,
        "source": ctx.get("source") or "alert",
        "bars_scanned": len(bars),
        "rows_in_force": len(rows),
        "entries_allowed": newest["entry"],
        "new_ideas_today": len(result["ideas"]),
        "open_ideas": sum(1 for i in result["ideas"] if i["end"] is None),
        "messages": [{"kind": k, "first_line": x.splitlines()[0]} for k, x in out],
        "telegram": TELEGRAM_ENABLED,
        "register": register_summary,
    }
