"""
Neon access for pattern-recorder.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo - the only database this function touches. A second Neon project
carries an `algo` schema with the same table names and incompatible column
types, so the connection string comes only from /algo/neon/connection.

READS  algo.candle_5min            earlier sessions' 5-min bars (today's come from Dhan)
       algo.intraday_fno_data      the option walls, each day's active contract,
                                   and the latest 15-min snapshot (for the manager)
       algo.intraday_sentiments    the latest day_trend, regime and bias
       algo.fno_5m_snapshot        the previous tick's captured_at, each bar's OI change
       algo.daily_market_sentiment the daily read handed to the manager
WRITES algo.pattern_events         and nothing else - never session_observations.
                                   The upsert never touches the sent marks; only
                                   claim_and_mark() sets them.

Every read is bounded by epoch values; there is no timezone arithmetic in SQL
(hard rule 2). connect() lives in the neon-access layer. pg8000's DB-API uses
`format` paramstyle (%s) and has no execute_values, so the multi-row upsert
builds its own VALUES tuples.

THE COLUMN LIST IS THE SOURCE OF TRUTH. The INSERT list, the placeholders and the
DO UPDATE SET are all generated from COLUMNS, as in intraday-fno-poller, so they
cannot drift apart. Keep it in step with schema.sql.
"""

import decimal
import logging
import re

from config import FUTURES_INSTRUMENT_TYPE, UPSERT_BATCH_SIZE

logger = logging.getLogger()

CANDLE_5MIN = "algo.candle_5min"
FNO_DATA = "algo.intraday_fno_data"
SENTIMENTS = "algo.intraday_sentiments"
FNO_5M = "algo.fno_5m_snapshot"
DAILY = "algo.daily_market_sentiment"
TABLE = "algo.pattern_events"
BAR_SECONDS = 300
SENT_MARKS = ("telegram_sent_at", "manager_sent_at")

# The daily read exactly as intraday-market-sentiment hands it on (its db.py), so
# strategy-manager and its playbooks see the same shape from either sender.
DAILY_COLUMNS = (
    "trade_date", "bias", "structure", "regime", "volatility",
    "score", "max_score", "confidence",
    "swing_direction", "swing_high", "swing_low", "structure_determined",
    "range_used", "volatility_expanding", "gap_pct",
    "pd_high", "pd_low", "pd_close",
    "rsi", "sma9", "sma50", "sma100", "sma200",
    "prev_volume", "avg_volume_50", "vix",
    "price", "expected_move", "upper_volatility", "lower_volatility",
    "min15_high", "min15_low", "created_at",
)

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


def _plain(row):
    """Decimals to floats, so a row can be logged and JSON-encoded as-is."""
    return {k: float(v) if isinstance(v, decimal.Decimal) else v for k, v in row.items()}


def read_fno_rows(conn, security_id, instrument_type, since_ts, captured_by):
    """
    The 15-min snapshots from since_ts that were WRITTEN by this run's clock
    (captured_at <= captured_by). Bounding on the write clock, not on
    snapshot_ts, keeps a Lambda retry minutes later on the same inputs.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT snapshot_ts, captured_at, prev_snapshot_ts, fut_security_id, "
        f"near_max_oi_call, near_max_oi_put, near_max_pain FROM {FNO_DATA} "
        f"WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts >= %s AND captured_at <= %s ORDER BY snapshot_ts",
        (str(security_id), str(instrument_type), int(since_ts), int(captured_by)),
    )
    return _rows(cursor)


def read_latest_fifteen(conn, security_id, instrument_type, day_start, captured_by):
    """
    Today's newest 15-min snapshot that this run can read, as ONE dict: the
    intraday_fno_data row merged with its intraday_sentiments row (sentiment
    wins on a shared key - identical values), the shape pattern-detector handed
    strategy-manager. None before the day's first classifier row (09:45).
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT * FROM {SENTIMENTS} WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts >= %s AND captured_at <= %s ORDER BY snapshot_ts DESC LIMIT 1",
        (str(security_id), str(instrument_type), int(day_start), int(captured_by)),
    )
    sentiment = _rows(cursor)
    if not sentiment:
        return None
    sentiment = _plain(sentiment[0])
    cursor.execute(
        f"SELECT * FROM {FNO_DATA} WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts = %s",
        (str(security_id), str(instrument_type), int(sentiment["snapshot_ts"])),
    )
    fno = _rows(cursor)
    if not fno:
        raise RuntimeError(
            f"{SENTIMENTS} has a row at {sentiment['snapshot_ts']} but {FNO_DATA} has none - "
            f"the chain wrote a judgement with no measurement")
    return {**_plain(fno[0]), **sentiment}


def read_prev_tick_captured_at(conn, security_id, instrument_type, snapshot_ts):
    """
    captured_at of the newest poller tick BEFORE this one, any day - so a missed
    tick widens the next run's fresh window instead of losing its events. None
    when there is no earlier tick at all.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT captured_at FROM {FNO_5M} WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts < %s ORDER BY snapshot_ts DESC LIMIT 1",
        (str(security_id), str(instrument_type), int(snapshot_ts)),
    )
    rows = cursor.fetchall()
    return int(rows[0][0]) if rows and rows[0][0] is not None else None


def read_bar_oi(conn, security_id, instrument_type, day_start, captured_by):
    """{fut_bar_ts: fut_bar_oi_change} for today's ticks this run can read."""
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT fut_bar_ts, fut_bar_oi_change FROM {FNO_5M} WHERE security_id = %s "
        f"AND instrument_type = %s AND snapshot_ts >= %s AND captured_at <= %s",
        (str(security_id), str(instrument_type), int(day_start), int(captured_by)),
    )
    return {int(ts): change for ts, change in cursor.fetchall() if ts is not None}


def read_daily(conn, security_id, instrument_type, day_start):
    """
    The daily read in force today - the newest daily_market_sentiment row stamped
    BEFORE today (trade_date is the session a row describes) - with `stale` set
    when it was written before today, exactly as intraday-market-sentiment builds
    it. None when there is no such row.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(DAILY_COLUMNS)} FROM {DAILY} WHERE security_id = %s "
        f"AND instrument_type = %s AND trade_date < %s ORDER BY trade_date DESC LIMIT 1",
        (str(security_id), str(instrument_type), int(day_start)),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    row = _plain(dict(zip(DAILY_COLUMNS, rows[0])))
    row["stale"] = int(row["created_at"] or 0) < int(day_start)
    return row


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


# ---- send once -------------------------------------------------------------------

def claim_and_mark(conn, row, column, action, now):
    """
    Run action() at most once for this event, and record it in `column`.

    In ONE transaction: lock the event's row only if `column` is still NULL
    (FOR UPDATE SKIP LOCKED, so an overlapping run skips it instead of
    waiting), run the action - the Telegram send or the manager invoke - then
    set the mark and commit.
    - If the action raises, the transaction rolls back, the mark stays NULL, and
      the exception propagates: the run fails, and Lambda's retry sends again.
    - The only duplicate left is a send that succeeded followed by a commit that
      failed.
    Returns True when this call ran the action, False when it was already done
    (or is being done by another run).
    """
    if column not in SENT_MARKS:
        raise ValueError(f"unknown sent mark {column!r}")
    where = " AND ".join(f"{c} = %s" for c in KEY)
    key = [row[c] for c in KEY]
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT 1 FROM {TABLE} WHERE {where} AND {column} IS NULL FOR UPDATE SKIP LOCKED", key)
        if not cursor.fetchall():
            conn.rollback()
            return False
        action()
        cursor.execute(f"UPDATE {TABLE} SET {column} = %s WHERE {where}", [int(now)] + key)
        if cursor.rowcount != 1:
            raise RuntimeError(f"marking {column} affected {cursor.rowcount} rows, expected 1")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
