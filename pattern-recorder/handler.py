"""
Pattern Recorder - AWS Lambda function

Runs the v2 pattern library (structure.py + patterns.py) on every live 15-min
snapshot and records what it recognises to algo.pattern_events. Invoked
asynchronously by pattern-detector as that function's LAST step, with the
payload market-classifier gave it:

    ... -> market-classifier -> pattern-detector -> (its own work, then) -> this

RECORDS ONLY. No Telegram, no strategy-manager dispatch, no effect on
pattern-detector's own decisions: a failure here stays in this function's log
group (error-notifier reports it) and cannot touch the live gate. Its retries
are harmless - the write is an idempotent upsert and nothing is announced.

STATELESS. Every run re-derives the whole session from stored closed bars and
upserts all of it (every event is a deterministic function of closed bars), so a
re-run reproduces the same rows and a missed run is filled by the next. The
replay (replay/replay.py) builds its inputs through the same inputs.py, so
"live == replay" is a check on one code path.

Definitions: v2-pattern-definitions.md. Results: v2-phase-a-replay.md.
Reads/writes only Neon project "AI Trader APP" (nameless-mountain-15353651),
database Algo, schema algo.

Modules:
    config.py     read/write settings
    db.py         the reads and the one upsert
    inputs.py     one session's inputs - shared with the replay
    structure.py  the *where*: 15-min bars, swings, levels
    patterns.py   the *when*: candles, families, volume
"""

import logging
import time

from neon_access import connect, ist_datetime, read_neon_connection_string

import db
import inputs
import patterns as pt
import structure as st
from config import HISTORY_DAYS

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The keys this function reads off the fno row. A payload without them is an old
# or foreign shape; naming the problem here beats failing deep in a query.
REQUIRED_FNO = ("security_id", "instrument_type", "snapshot_ts", "captured_at", "fut_security_id")


def read_event(event):
    """Validate the payload pattern-detector forwards and return its fno row."""
    if not isinstance(event, dict):
        raise RuntimeError(f"expected pattern-detector's payload, got {type(event).__name__}")
    fno = event.get("fno")
    if not isinstance(fno, dict):
        raise RuntimeError(f"payload needs an `fno` dict - keys present: {sorted(event)}")
    missing = [k for k in REQUIRED_FNO if fno.get(k) is None]
    if missing:
        raise RuntimeError(f"payload is missing {missing} - nothing can be recorded")
    return fno


def _clock(ts):
    return ist_datetime(ts).strftime("%H:%M")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    fno = read_event(event or {})
    security_id, instrument_type = str(fno["security_id"]), str(fno["instrument_type"])
    snapshot_ts, closed_by = int(fno["snapshot_ts"]), int(fno["captured_at"])
    contract = str(fno["fut_security_id"])
    stamp = ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M")
    since = st.ist_day_start(snapshot_ts) - HISTORY_DAYS * st.DAY_SECONDS

    conn = connect(read_neon_connection_string())
    try:
        fno_rows = db.read_fno_rows(conn, security_id, instrument_type, since, snapshot_ts)
        contracts = {str(r["fut_security_id"]) for r in fno_rows if r.get("fut_security_id")} | {contract}
        fut_rows = db.read_future_bars(conn, sorted(contracts), since, closed_by)
        idx_rows = db.read_index_bars(conn, security_id, instrument_type, since, closed_by)

        store = inputs.Store(fut_rows, idx_rows, fno_rows)
        session = store.session_inputs(st.ist_day(snapshot_ts), contract=contract,
                                       closed_by=closed_by, snapshot_ts=snapshot_ts)
        if not session["session_bars"]:
            raise RuntimeError(
                f"snapshot {stamp}: no closed 5-min bars for future {contract} today - "
                f"the loader's bars are missing, so nothing can be recognised")
        events = inputs.detect(session, pt.detect_session)

        now = int(time.time())
        rows = [db.event_row(e, security_id, instrument_type, contract, pt.RULES_VERSION,
                             snapshot_ts, closed_by, now) for e in events]
        written = db.upsert_events(conn, rows)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # "Fresh" = the trigger bar closed since the previous run - the same rule as
    # pattern-detector. On a session's first run the previous snapshot is the
    # previous day's last, so the morning is fresh even when the first run is late.
    by_ts = {int(r["snapshot_ts"]): r for r in fno_rows}
    prev = by_ts.get(int(fno["prev_snapshot_ts"])) if fno.get("prev_snapshot_ts") else None
    closed_after = int(prev["captured_at"]) if prev and prev.get("captured_at") else closed_by - 900
    fresh = pt.fresh_events(events, closed_after, closed_by)
    for e in fresh:
        logger.info(
            "snapshot %s: %s%s %s at %s %s (%+.1f pts) on %s, candles %s, %s vol z %s, rvol %s",
            stamp, e["family"], "/" + e["subtype"] if e["subtype"] else "", e["direction"],
            e["level"], e["level_price"], e["distance_pts"], _clock(e["trigger_ts"]),
            ",".join(e["candles"]) or "-", e["chart_pattern"] or "", e["vol_z"], e["rvol_tod"])
    logger.info(
        "snapshot %s: %d fresh, %d in session, %d rows upserted (%s, future %s, %d bars)",
        stamp, len(fresh), len(events), written, pt.RULES_VERSION, contract,
        len(session["session_bars"]))
    return {
        "status": "success",
        "snapshot": stamp,
        "rules_version": pt.RULES_VERSION,
        "fresh": len(fresh),
        "events_in_session": len(events),
        "rows_upserted": written,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
