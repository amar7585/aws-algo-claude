"""
Neon access for pattern-recorder.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo - the only database this function touches. A second Neon project
carries an `algo` schema with the same table names and incompatible column
types, so the connection string comes only from /algo/neon/connection.

READS  algo.candle_5min        the active future's and the index's 5-min bars
       algo.intraday_fno_data  the option walls, each day's active contract,
                               and the previous run's captured_at
WRITES algo.pattern_events     and nothing else - never session_observations

Every read is bounded by epoch values; there is no timezone arithmetic in SQL
(hard rule 2). connect() lives in the neon-access layer. pg8000's DB-API uses
`format` paramstyle (%s) and has no execute_values, so the multi-row upsert
builds its own VALUES tuples.

THE COLUMN LIST IS THE SOURCE OF TRUTH. The INSERT list, the placeholders and the
DO UPDATE SET are all generated from COLUMNS, as in intraday-fno-poller, so they
cannot drift apart. Keep it in step with schema.sql.
"""

import logging
import re

from config import FUTURES_INSTRUMENT_TYPE, UPSERT_BATCH_SIZE

logger = logging.getLogger()

CANDLE_5MIN = "algo.candle_5min"
FNO_DATA = "algo.intraday_fno_data"
TABLE = "algo.pattern_events"
BAR_SECONDS = 300

KEY = ("security_id", "instrument_type", "rules_version", "trigger_ts", "family",
       "subtype", "direction")
COLUMNS = KEY + (
    "fut_security_id", "first_ts", "trigger_close", "extreme",
    "level", "level_price", "level_strike", "distance_pts", "distance_pct",
    "other_levels", "candles", "prior_tests", "chart_pattern", "structure_15m",
    "prior_break_direction", "prior_break_ts",
    "vol_z", "vol_z_max", "rvol_tod", "retest_vol_ratio", "sweep_depth_pct",
    "bars_beyond", "break_strength", "index_twin_offset_pct",
    "first_seen_snapshot_ts", "first_seen_at", "created_at", "updated_at",
)
# When an event was FIRST recorded must survive every later re-write of it.
KEEP_ON_CONFLICT = ("first_seen_snapshot_ts", "first_seen_at", "created_at")
ARRAY_COLUMNS = ("other_levels", "candles")

_PLACEHOLDER = "(" + ", ".join("%s::text[]" if c in ARRAY_COLUMNS else "%s" for c in COLUMNS) + ")"
_UPSERT = (
    f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES {{values}} "
    f"ON CONFLICT ({', '.join(KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in KEY and c not in KEEP_ON_CONFLICT)
)

_SIMPLE = re.compile(r"^[a-z0-9_]+$")


def _text_array(items):
    """
    A Postgres text[] literal. Sent as a string and cast in SQL, because pg8000
    cannot type an EMPTY Python list - and most events have no other_levels. The
    elements are our own level/candle names; anything else is refused rather
    than quoted.
    """
    for item in items:
        if not _SIMPLE.match(item):
            raise ValueError(f"unexpected array element {item!r}")
    return "{" + ",".join(items) + "}"


# ---- reads ---------------------------------------------------------------------

def _rows(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def read_fno_rows(conn, security_id, instrument_type, since_ts, until_snapshot_ts):
    """The 15-min snapshots from since_ts up to and including this run's."""
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT snapshot_ts, captured_at, prev_snapshot_ts, fut_security_id, "
        f"near_max_oi_call, near_max_oi_put, near_max_pain FROM {FNO_DATA} "
        f"WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts >= %s AND snapshot_ts <= %s ORDER BY snapshot_ts",
        (str(security_id), str(instrument_type), int(since_ts), int(until_snapshot_ts)),
    )
    return _rows(cursor)


def read_future_bars(conn, contracts, since_ts, closed_by):
    """Closed 5-min bars of the named futures, on their FULL identity (hard rule #4)."""
    contracts = [str(c) for c in contracts]
    if not contracts:
        return []
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT security_id, candle_ts, open, high, low, close, volume FROM {CANDLE_5MIN} "
        f"WHERE instrument_type = %s AND security_id IN ({', '.join(['%s'] * len(contracts))}) "
        f"AND candle_ts >= %s AND candle_ts + %s <= %s ORDER BY security_id, candle_ts",
        (FUTURES_INSTRUMENT_TYPE, *contracts, int(since_ts), BAR_SECONDS, int(closed_by)),
    )
    return _rows(cursor)


def read_index_bars(conn, security_id, instrument_type, since_ts, closed_by):
    """Closed 5-min bars of the underlying index."""
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT security_id, candle_ts, open, high, low, close, volume FROM {CANDLE_5MIN} "
        f"WHERE security_id = %s AND instrument_type = %s "
        f"AND candle_ts >= %s AND candle_ts + %s <= %s ORDER BY candle_ts",
        (str(security_id), str(instrument_type), int(since_ts), BAR_SECONDS, int(closed_by)),
    )
    return _rows(cursor)


# ---- the write -------------------------------------------------------------------

def event_row(event, security_id, instrument_type, fut_security_id, rules_version,
              snapshot_ts, captured_at, now):
    """One patterns.detect_session event as a pattern_events row."""
    brk = event.get("prior_break") or {}
    return {
        "security_id": str(security_id),
        "instrument_type": str(instrument_type),
        "rules_version": rules_version,
        "trigger_ts": int(event["trigger_ts"]),
        "family": event["family"],
        "subtype": event["subtype"] or "none",
        "direction": event["direction"],
        "fut_security_id": str(fut_security_id),
        "first_ts": int(event["first_ts"]),
        "trigger_close": event["trigger_close"],
        "extreme": event["extreme"],
        "level": event["level"],
        "level_price": event["level_price"],
        "level_strike": event["level_strike"],
        "distance_pts": event["distance_pts"],
        "distance_pct": event["distance_pct"],
        "other_levels": _text_array(event["other_levels"]),
        "candles": _text_array(event["candles"]),
        "prior_tests": event["prior_tests"],
        "chart_pattern": event["chart_pattern"],
        "structure_15m": event["structure_15m"],
        "prior_break_direction": brk.get("direction"),
        "prior_break_ts": brk.get("ts"),
        "vol_z": event["vol_z"],
        "vol_z_max": event["vol_z_max"],
        "rvol_tod": event["rvol_tod"],
        "retest_vol_ratio": event["retest_vol_ratio"],
        "sweep_depth_pct": event["sweep_depth_pct"],
        "bars_beyond": event["bars_beyond"],
        "break_strength": event["break_strength"],
        "index_twin_offset_pct": event["index_twin_offset_pct"],
        "first_seen_snapshot_ts": int(snapshot_ts),
        "first_seen_at": int(captured_at),
        "created_at": int(now),
        "updated_at": int(now),
    }


def upsert_events(conn, rows):
    """
    Upsert every row in ONE transaction and return how many were written.

    FAIL LOUDLY: Postgres reports one affected row per inserted-or-updated row,
    and anything other than len(rows) raises - a schema change must never
    degrade into a silent 0-row write (hard rule 5). Zero rows is a legitimate
    answer only when there were zero events.
    """
    cursor = conn.cursor()
    try:
        written = 0
        for start in range(0, len(rows), UPSERT_BATCH_SIZE):
            batch = rows[start:start + UPSERT_BATCH_SIZE]
            params = []
            for row in batch:
                missing = [c for c in COLUMNS if c not in row]
                if missing:
                    raise RuntimeError(f"event row is missing {missing} - refusing to write")
                params.extend(row[c] for c in COLUMNS)
            cursor.execute(_UPSERT.format(values=", ".join([_PLACEHOLDER] * len(batch))), params)
            if cursor.rowcount != len(batch):
                raise RuntimeError(
                    f"upsert into {TABLE} affected {cursor.rowcount} rows, expected {len(batch)}")
            written += len(batch)
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
