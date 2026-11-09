"""
Pattern Recorder (v2.1) - AWS Lambda function

Runs the v2 pattern library (structure.py + patterns.py) every FIVE minutes and
records what it recognises to algo.pattern_events; the events that pass the alert
rule (alerts.py) go to Telegram and strategy-manager, ONCE each. Invoked
asynchronously by intraday-fno-poller right after it writes its 5-min row:

    intraday-fno-poller (every 5 min) -> this -> Telegram + strategy-manager
    loader -> sentiment -> market-classifier (day_trend) -> pattern-detector (v1, log only)

PLAN V3, step 4 (approved 2026-11-09). What changed from v2.0:
- the trigger: the poller's 5-min tick, not pattern-detector's 15-min hand-off;
- today's candles: from Dhan, in memory, never saved - intraday-data-loader stays
  the only candle_5min writer. Earlier sessions still come from candle_5min;
- the key levels: the INDEX's PDH/PDL/ORH/ORL, priced on the future (RULES v2.1);
- alerts: the day_trend rule at the key levels, sent once (claim, send, mark).
  ALERTS_ENABLED unset = SHADOW: alerts are decided and logged "WOULD SEND",
  nothing is sent and no mark is written.

STATELESS. Every run re-derives the whole session from closed bars and upserts
all of it, so a re-run reproduces the same rows and a missed run is filled by
the next. The sent marks are the only state, and the upsert never touches them.

Its retries are safe: the upsert is idempotent, and a mark set by a successful
send stops a resend. A failed send rolls back its claim and raises, so Lambda's
retry sends it again.

Definitions: v2-pattern-definitions.md. Reads/writes only Neon project
"AI Trader APP" (nameless-mountain-15353651), database Algo, schema algo.

Modules:
    config.py     settings
    db.py         the reads, the upsert, the send-once claim
    dhan.py       today's 5-min candles
    params.py     the Dhan token and client id
    inputs.py     one session's inputs - shared with the replay
    structure.py  the *where*: 15-min bars, swings, levels
    patterns.py   the *when*: candles, families, volume
    alerts.py     which events are alerts, and their text
    notify.py     Telegram
    dispatch.py   strategy-manager
"""

import logging
import time

from neon_access import connect, ist_datetime, read_neon_connection_string

import alerts
import db
import dispatch
import inputs
import notify
import patterns as pt
import structure as st
from config import (
    ALERTS_ENABLED,
    CANDLE_INTERVAL_MINUTES,
    HISTORY_DAYS,
    STRATEGY_MANAGER_FUNCTION_NAME,
)
from dhan import DhanClient, session_candles, to_candles
from params import read_client_id, read_token_record

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# What the poller's payload must carry. A payload without them is an old or
# foreign shape; naming the problem here beats failing deep in a query.
REQUIRED_TICK = ("security_id", "instrument_type", "snapshot_ts", "captured_at", "fut_security_id")
REQUIRED_INSTRUMENT = ("security_id", "exchange_segment", "instrument_type")


def read_event(event):
    """Validate intraday-fno-poller's payload: (tick, index instrument, future instrument)."""
    if not isinstance(event, dict):
        raise RuntimeError(f"expected intraday-fno-poller's payload, got {type(event).__name__}")
    if "fno" in event and "tick" not in event:
        raise RuntimeError(
            "this is pattern-detector's v2.0 hand-off; v2.1 runs on intraday-fno-poller's "
            "5-min tick - unset PATTERN_RECORDER_FUNCTION_NAME on pattern-detector")
    tick, index, future = event.get("tick"), event.get("index"), event.get("future")
    if not all(isinstance(x, dict) for x in (tick, index, future)):
        raise RuntimeError(f"payload needs `tick`, `index` and `future` dicts - keys present: {sorted(event)}")
    missing = [f"tick.{k}" for k in REQUIRED_TICK if tick.get(k) is None]
    missing += [f"index.{k}" for k in REQUIRED_INSTRUMENT if index.get(k) is None]
    missing += [f"future.{k}" for k in REQUIRED_INSTRUMENT if future.get(k) is None]
    if missing:
        raise RuntimeError(f"payload is missing {missing} - nothing can be recognised")
    if str(future["security_id"]) != str(tick["fut_security_id"]):
        raise RuntimeError(
            f"the tick names future {tick['fut_security_id']} but the payload's future is "
            f"{future['security_id']}")
    return tick, index, future


def _clock(ts):
    return ist_datetime(ts).strftime("%H:%M")


def _as_rows(candles, security_id):
    return [{"security_id": str(security_id), "candle_ts": c["ts"], "open": c["open"], "high": c["high"],
             "low": c["low"], "close": c["close"], "volume": c["volume"]} for c in candles]


def require_key_levels(session, closed_by, stamp):
    """
    FAIL LOUDLY when the index's key levels cannot be built. Without them there
    is no alert at all, and an empty alert list would look like a quiet day.
    PDH/PDL need the previous index session; ORH/ORL need today's 09:15-09:25
    index bars, which exist once the 09:25 bar has closed (09:30).
    """
    if not session["prev_index_bars"]:
        raise RuntimeError(f"tick {stamp}: no previous index session in candle_5min - no PDH/PDL")
    orb = [b for b in session["index_bars"]
           if st.SESSION_OPEN_MINUTE <= st.ist_minute(b["ts"]) < st.ORB_END_MINUTE]
    if st.ist_minute(closed_by) >= st.ORB_END_MINUTE and len(orb) != 3:
        raise RuntimeError(
            f"tick {stamp}: {len(orb)} of the 3 opening-range index bars - no ORH/ORL")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    tick, index, future = read_event(event or {})
    security_id, instrument_type = str(tick["security_id"]), str(tick["instrument_type"])
    snapshot_ts, closed_by = int(tick["snapshot_ts"]), int(tick["captured_at"])
    contract = str(tick["fut_security_id"])
    stamp = ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M")
    day = ist_datetime(snapshot_ts).date()
    day_start = st.ist_day_start(snapshot_ts)
    since = day_start - HISTORY_DAYS * st.DAY_SECONDS

    # ---- today's candles, from Dhan (never stored) ---------------------------
    record = read_token_record()
    client = DhanClient(record["access_token"], read_client_id(record))
    idx_today = session_candles(to_candles(client.intraday_candles(index, CANDLE_INTERVAL_MINUTES, day)), day)
    fut_today = session_candles(to_candles(client.intraday_candles(future, CANDLE_INTERVAL_MINUTES, day)), day)

    conn = connect(read_neon_connection_string())
    try:
        # ---- earlier sessions, and the 15-min rows, from Neon ----------------
        fno_rows = db.read_fno_rows(conn, security_id, instrument_type, since, closed_by)
        contracts = {str(r["fut_security_id"]) for r in fno_rows if r.get("fut_security_id")} | {contract}
        fut_hist = db.read_future_bars(conn, sorted(contracts), since, day_start)
        idx_hist = db.read_index_bars(conn, security_id, instrument_type, since, day_start)

        store = inputs.Store(fut_hist + _as_rows(fut_today, contract),
                             idx_hist + _as_rows(idx_today, security_id), fno_rows)
        session = store.session_inputs(st.ist_day(snapshot_ts), contract=contract,
                                       closed_by=closed_by, snapshot_ts=snapshot_ts)
        if not session["session_bars"]:
            raise RuntimeError(f"tick {stamp}: Dhan returned no closed 5-min bars for future {contract}")
        require_key_levels(session, closed_by, stamp)
        events = inputs.detect(session, pt.detect_session)

        now = int(time.time())
        rows = [db.event_row(e, security_id, instrument_type, contract, pt.RULES_VERSION,
                             snapshot_ts, closed_by, now) for e in events]
        written = db.upsert_events(conn, rows)

        # ---- which events this run announces, and which are alerts -------------
        prev_captured = db.read_prev_tick_captured_at(conn, security_id, instrument_type, snapshot_ts)
        closed_after = prev_captured if prev_captured is not None else closed_by - st.BAR_SECONDS
        fresh = pt.fresh_events(events, closed_after, closed_by)
        fifteen = db.read_latest_fifteen(conn, security_id, instrument_type, day_start, closed_by)
        day_trend = (fifteen or {}).get("day_trend") or "none"
        alert_events = [e for e in fresh if alerts.passes(e, day_trend)]
        bar_oi = db.read_bar_oi(conn, security_id, instrument_type, day_start, closed_by) if alert_events else {}
        daily = db.read_daily(conn, security_id, instrument_type, day_start) if alert_events else None
        row_by_key = {(r["trigger_ts"], r["family"], r["subtype"], r["direction"]): r for r in rows}

        for e in fresh:
            logger.info(
                "tick %s: %s%s %s at %s %s (%+.1f pts) on %s, candles %s, %s vol z %s, rvol %s",
                stamp, e["family"], "/" + e["subtype"] if e["subtype"] else "", e["direction"],
                e["level"], e["level_price"], e["distance_pts"], _clock(e["trigger_ts"]),
                ",".join(e["candles"]) or "-", e["chart_pattern"] or "", e["vol_z"], e["rvol_tod"])

        sent_telegram = sent_manager = 0
        for e in alert_events:
            text = alerts.message(e, stamp, _clock(e["trigger_ts"]), day_trend,
                                  bar_oi.get(int(e["trigger_ts"])), fifteen)
            row = row_by_key[(int(e["trigger_ts"]), e["family"], e["subtype"] or "none", e["direction"])]
            if not ALERTS_ENABLED:
                logger.info("WOULD SEND (shadow): %s", text.replace("\n", " | "))
                continue
            if db.claim_and_mark(conn, row, "telegram_sent_at", lambda: notify.send_telegram(text), now):
                sent_telegram += 1
            if not STRATEGY_MANAGER_FUNCTION_NAME:
                continue
            if fifteen is None:
                logger.info("tick %s: no 15-min row yet today - %s %s not dispatched (nothing to route on)",
                            stamp, e["family"], e["direction"])
                continue
            payload = dispatch.manager_payload(index, fifteen, daily, e, day_trend, pt.RULES_VERSION)
            if db.claim_and_mark(conn, row, "manager_sent_at", lambda: dispatch.invoke_manager(payload), now):
                sent_manager += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info(
        "tick %s: %d fresh, %d alerts (%s, day trend %s), %d sent to Telegram, %d to the manager; "
        "%d in session, %d rows upserted (%s, future %s, %d bars, %d Dhan calls)",
        stamp, len(fresh), len(alert_events), "LIVE" if ALERTS_ENABLED else "shadow", day_trend,
        sent_telegram, sent_manager, len(events), written, pt.RULES_VERSION, contract,
        len(session["session_bars"]), client.calls)
    return {
        "status": "success",
        "tick": stamp,
        "rules_version": pt.RULES_VERSION,
        "alerts_enabled": ALERTS_ENABLED,
        "day_trend": day_trend,
        "fresh": len(fresh),
        "alerts": len(alert_events),
        "sent_telegram": sent_telegram,
        "sent_manager": sent_manager,
        "events_in_session": len(events),
        "rows_upserted": written,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
