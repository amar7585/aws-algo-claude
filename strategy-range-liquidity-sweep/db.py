"""
Neon access - the reads, and one write: the alert register.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

ONE WRITE, AND NOTHING READS IT BACK. Since 2026-11-14 (Amar: "re-derive and
record") write_register() upserts today's alert register into
algo.playbook_alerts - a RECORD of what the playbook decided, rebuilt whole on
every run. No run reads it, so nothing it holds can steer a later run or go
stale in a way that matters: the next run rewrites the day. Everything else
here is a SELECT.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders).
"""

import decimal
import json
import logging

from config import REGISTER_TABLE

logger = logging.getLogger()

_INSTRUMENT_COLUMNS = (
    "SELECT security_id, trading_symbol, exchange_segment, instrument_type "
    "FROM algo.instrument_master "
)

CANDLE_COLUMNS = ("candle_ts", "open", "high", "low", "close", "volume")

# Which table holds which interval. Mirrors intraday-data-loader's
# INTERVAL_TABLES, and deliberately a copy - a shared version would have to
# live in the neon-access layer, where every change means republishing it and
# repointing every function's pinned ARN.
INTERVAL_TABLES = {
    5: "algo.candle_5min",
    15: "algo.candle_15min",
    60: "algo.candle_1hr",
}

DAILY_TABLE = "algo.candle_daily"
# THE DAILY SENTIMENT ROW IS NOT READ HERE. It arrives in the manager's
# payload, already looked up by intraday-market-sentiment. Reading it again
# would be a second answer to the same question, and the two could differ -
# the payload's row is the one the routing decision was actually made
# against, so it is the one this playbook must gate on.

# Everything the sweep playbook's gate and its levels need off the daily row.
# pd_high/pd_low are the previous day's extremes - two of the liquidity pools -
# and upper/lower_volatility bracket the VIX-implied move for the day.


def closed_candles(conn, interval_minutes, instrument, as_of, limit):
    """
    The newest `limit` CLOSED bars at or before `as_of`, oldest first.

    THE CLOSURE RULE IS THE DOCUMENTED READER'S RULE, not a guess:
    intraday-data-loader stores the in-progress bucket on purpose and corrects
    it by primary key on a later pass, so a consumer tells a finished bar from
    a forming one with `candle_ts + interval_seconds <= now`. `as_of` stands in
    for now, and it is derived from the snapshot rather than the clock so that
    two runs over the same snapshot read exactly the same bars.

    Ordered oldest-first on return because every indicator in indicators.py
    walks forward, and a reversed series produces a plausible number rather
    than an error.
    """
    table = INTERVAL_TABLES.get(int(interval_minutes))
    if table is None:
        raise RuntimeError(
            f"no table for a {interval_minutes}-minute interval - "
            f"intraday-data-loader fills {sorted(INTERVAL_TABLES)}"
        )
    interval_seconds = int(interval_minutes) * 60
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(CANDLE_COLUMNS)} FROM {table} "
        f"WHERE security_id = %s AND instrument_type = %s "
        f"AND candle_ts + %s <= %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (
            str(instrument["security_id"]),
            str(instrument["instrument_type"]),
            interval_seconds,
            int(as_of),
            int(limit),
        ),
    )
    rows = cursor.fetchall()
    candles = [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": int(row[5] or 0),
        }
        for row in reversed(rows)
    ]
    logger.info(
        "%s: %d closed bars up to %d", table, len(candles), int(as_of)
    )
    return candles


def daily_candles(conn, instrument, before_ts, limit):
    """
    The newest `limit` daily bars strictly before `before_ts`, oldest first.

    Strictly before, because `before_ts` is IST midnight of the session in
    progress and today's daily candle is not a finished bar. Dhan's daily
    endpoint lags a session anyway, so it will not normally be there - but
    "will not normally be" is not a filter.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(CANDLE_COLUMNS)} FROM {DAILY_TABLE} "
        f"WHERE security_id = %s AND instrument_type = %s AND candle_ts < %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (
            str(instrument["security_id"]),
            str(instrument["instrument_type"]),
            int(before_ts),
            int(limit),
        ),
    )
    rows = cursor.fetchall()
    return [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": int(row[5] or 0),
        }
        for row in reversed(rows)
    ]


SENTIMENTS_TABLE = "algo.intraday_sentiments"
FNO_DATA_TABLE = "algo.intraday_fno_data"


def _plain(row):
    return {k: float(v) if isinstance(v, decimal.Decimal) else v for k, v in row.items()}


def day_rows(conn, instrument, day_start, until):
    """
    Today's 15-min rows WRITTEN by `until`, oldest first - each the
    intraday_fno_data row merged with its intraday_sentiments row (sentiment
    wins on a shared key), the same shape pattern-recorder hands the manager.

    WHY THE WHOLE DAY. Since 2026-11-14 this function runs on every 5-min tick
    and re-derives the session candle by candle, so it must judge each earlier
    candle with the regime, day trend and VIX that were in force THEN -
    not with the newest row. Without that, a trade entered before the context
    changed would vanish from the re-derivation and its exit would never be
    sent. Bounded on created_at (when a row became readable), so a re-run sees
    exactly what an earlier run could have seen.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT * FROM {SENTIMENTS_TABLE} WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts >= %s AND created_at <= %s ORDER BY snapshot_ts",
        (str(instrument["security_id"]), str(instrument["instrument_type"]), int(day_start), int(until)),
    )
    cols = [d[0] for d in cursor.description]
    sentiments = [_plain(dict(zip(cols, r))) for r in cursor.fetchall()]
    if not sentiments:
        return []
    cursor.execute(
        f"SELECT * FROM {FNO_DATA_TABLE} WHERE security_id = %s AND instrument_type = %s "
        f"AND snapshot_ts >= %s AND snapshot_ts <= %s",
        (str(instrument["security_id"]), str(instrument["instrument_type"]), int(day_start),
         int(sentiments[-1]["snapshot_ts"])),
    )
    cols = [d[0] for d in cursor.description]
    fno = {int(r["snapshot_ts"]): r for r in (_plain(dict(zip(cols, x))) for x in cursor.fetchall())}
    merged = []
    for s in sentiments:
        f = fno.get(int(s["snapshot_ts"]))
        if f is None:
            raise RuntimeError(
                f"{SENTIMENTS_TABLE} has a row at {s['snapshot_ts']} but {FNO_DATA_TABLE} has none - "
                f"the chain wrote a judgement with no measurement")
        merged.append({**f, **s})
    logger.info("%s: %d rows today written by %d", SENTIMENTS_TABLE, len(merged), int(until))
    return merged


# ---------------------------------------------------------------------------
# The alert register (register.py) - the one write
# ---------------------------------------------------------------------------
REGISTER_KEY = ("security_id", "instrument_type", "playbook", "logic", "side", "level_name", "alert_ts")
REGISTER_FIELDS = (
    "logic", "level_name", "side", "level_price", "alert_ts", "touches", "touch_log", "status", "status_ts",
    "reason", "target_price", "direction", "entry_ts", "entry_price", "signal_close", "stop_price", "t1_price",
    "t2_price", "risk_reward", "grade", "outcome", "steps",
)
_JSON_FIELDS = frozenset(("touch_log", "steps"))
# Postgres caps a statement at 65,535 bind parameters. A session writes a few
# dozen rows of 28, so this is far off - but name it if it is ever reached.
_MAX_PARAMS = 65535


def write_register(conn, instrument, playbook, session_start, records, now, derived_through_ts):
    """
    Upsert today's whole register for this playbook in ONE statement, then commit.

    A re-derivation can only add alerts or move them forward - a touch that
    happened cannot un-happen on closed candles (measured on 41 sessions, 2,796
    ticks, 2026-11-14). So a stored row that this run did NOT derive means the
    derivation changed under the data, and that is raised and rolled back rather
    than silently overwritten or left behind.

    ORDER, NOT ONLY CONTENT (option A, Amar 2026-11-14). Ticks can arrive out of
    order - an async retry, a delayed invoke. Every row carries
    `derived_through_ts`, the newest candle of the run that wrote it:
      * an update applies only when this run is derived through the same candle
        or a later one, so an OLDER run can never overwrite a newer register;
      * the "lost row" check counts only stored rows derived through this run's
        candle or earlier - rows a NEWER run wrote are not this run's to
        reproduce, and are left as they are.
    Before this, an older tick arriving after a newer register was stored raised
    "N stored row(s) were not re-derived" (found by the 22:58 smoke).

    Returns {"written": n, "newer_kept": m}. Raises on any inconsistency.
    """
    sid, itype = str(instrument["security_id"]), str(instrument["instrument_type"])
    thru = int(derived_through_ts)
    scope = [sid, itype, playbook, int(session_start)]
    where = ("security_id = %s AND instrument_type = %s AND playbook = %s AND session_start = %s")
    t_where = "t." + where.replace(" AND ", " AND t.")
    cursor = conn.cursor()
    if not records:
        cursor.execute(
            f"SELECT count(*) FILTER (WHERE derived_through_ts <= %s), count(*) FILTER (WHERE derived_through_ts > %s) "
            f"FROM {REGISTER_TABLE} WHERE {where}", [thru, thru] + scope)
        lost, newer = (int(x) for x in cursor.fetchone())
        if lost:
            conn.rollback()
            raise RuntimeError(
                f"register: this run derived no alerts but {lost} are stored for the session from runs no newer than "
                f"this one - the derivation lost rows an earlier run made")
        conn.commit()
        return {"written": 0, "newer_kept": newer}

    cols = (("security_id", "instrument_type", "playbook", "session_start", "derived_through_ts")
            + REGISTER_FIELDS + ("first_seen_at", "updated_at"))
    if (len(cols) + 4) * len(records) + 10 > _MAX_PARAMS:
        raise RuntimeError(f"register: {len(records)} rows exceed Postgres' {_MAX_PARAMS} bind parameters")
    one_row = "(" + ", ".join("%s::jsonb" if c in _JSON_FIELDS else "%s" for c in cols) + ")"
    ident_params, row_params = [], []
    for r in records:
        ident_params += [r["logic"], r["side"], r["level_name"], int(r["alert_ts"])]
        row_params += (scope + [thru] + [json.dumps(r[f]) if f in _JSON_FIELDS else r[f] for f in REGISTER_FIELDS]
                       + [int(now), int(now)])
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in REGISTER_KEY and c != "first_seen_at")
    match = ("i.logic = t.logic AND i.side = t.side AND i.level_name = t.level_name AND i.alert_ts = t.alert_ts")
    sql = (
        f"WITH incoming (logic, side, level_name, alert_ts) AS (VALUES "
        f"{', '.join(['(%s, %s, %s, %s::bigint)'] * len(records))}), "
        f"written AS ("
        f"INSERT INTO {REGISTER_TABLE} AS pa ({', '.join(cols)}) VALUES {', '.join([one_row] * len(records))} "
        f"ON CONFLICT ({', '.join(REGISTER_KEY)}) DO UPDATE SET {updates} "
        f"WHERE pa.derived_through_ts <= EXCLUDED.derived_through_ts "
        f"RETURNING 1) "
        f"SELECT (SELECT count(*) FROM written) AS written, "
        f"(SELECT count(*) FROM {REGISTER_TABLE} t WHERE {t_where} AND t.derived_through_ts <= %s "
        f"AND NOT EXISTS (SELECT 1 FROM incoming i WHERE {match})) AS lost, "
        f"(SELECT count(*) FROM {REGISTER_TABLE} t WHERE {t_where} AND t.derived_through_ts > %s) AS newer"
    )
    cursor.execute(sql, ident_params + row_params + scope + [thru] + scope + [thru])
    written, lost, newer = (int(x) for x in cursor.fetchone())
    if lost or (newer == 0 and written != len(records)):
        conn.rollback()
        raise RuntimeError(
            f"register: wrote {written} of {len(records)} rows and {lost} stored row(s) derived through this run's "
            f"candle or earlier were not re-derived - rolled back; the derivation is not reproducing an earlier run")
    conn.commit()
    if newer:
        logger.info("%s: an older run (derived through %d) - %d row(s) from a newer run kept, %d written",
                    REGISTER_TABLE, thru, newer, written)
    else:
        logger.info("%s: %d register rows upserted for session %d, derived through %d",
                    REGISTER_TABLE, written, int(session_start), thru)
    return {"written": written, "newer_kept": newer}
