"""
Neon access.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders) and has no execute_values equivalent, so the
multi-row upserts below build their own VALUES tuples.
"""

import logging

from config import UPSERT_BATCH_SIZE

logger = logging.getLogger()


def resolve_instrument(conn, security_id, instrument_type):
    """
    Resolve on the FULL identity, never security_id alone.

    security_id is NOT unique. 19 security_ids in instrument_master carry more
    than one instrument_type, and `13` is both NIFTY (IDX_I/INDEX) and ABB
    (NSE_EQ/EQUITY). An earlier version of this function queried on
    security_id alone and took fetchone(); on 2026-09-11 it silently resolved
    to ABB and wrote 204 ABB candles plus an ABB sentiment row labelled NIFTY.

    fetchall() plus an exactly-one check means a future collision raises
    instead of picking whichever row Postgres happens to return first.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT security_id, trading_symbol, exchange_segment, instrument_type "
        "FROM algo.instrument_master "
        "WHERE security_id = %s AND instrument_type = %s",
        (str(security_id), str(instrument_type)),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{security_id}/{instrument_type} matched {len(rows)} rows in "
            f"algo.instrument_master, expected exactly 1"
            + ("" if rows else " - run instrument-master-loader first")
        )
    row = rows[0]
    instrument = {
        "security_id": row[0],
        "trading_symbol": row[1],
        "exchange_segment": row[2],
        "instrument_type": row[3],
    }
    logger.info(
        "resolved %s/%s -> %s on %s",
        security_id, instrument_type, instrument["trading_symbol"],
        instrument["exchange_segment"],
    )
    return instrument


def latest_candle_ts(conn, instrument):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MAX(candle_ts) FROM algo.candle_daily "
        "WHERE security_id = %s AND instrument_type = %s",
        (instrument["security_id"], instrument["instrument_type"]),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] is not None else None


def load_daily_candles(conn, instrument):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT candle_ts, open, high, low, close, volume FROM algo.candle_daily "
        "WHERE security_id = %s AND instrument_type = %s ORDER BY candle_ts",
        (instrument["security_id"], instrument["instrument_type"]),
    )
    return [
        {
            "ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": int(r[5]),
        }
        for r in cursor.fetchall()
    ]


CANDLE_UPSERT = """
INSERT INTO algo.candle_daily
    (security_id, instrument_type, candle_ts, open, high, low, close, volume)
VALUES {values}
ON CONFLICT (security_id, instrument_type, candle_ts) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume
"""


def upsert_daily_candles(conn, instrument, candles):
    if not candles:
        return 0
    cursor = conn.cursor()
    written = 0
    for start in range(0, len(candles), UPSERT_BATCH_SIZE):
        batch = candles[start : start + UPSERT_BATCH_SIZE]
        params = []
        for candle in batch:
            params.extend([
                instrument["security_id"], instrument["instrument_type"],
                candle["ts"], candle["open"], candle["high"], candle["low"],
                candle["close"], int(candle["volume"]),
            ])
        placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(batch))
        cursor.execute(CANDLE_UPSERT.format(values=placeholders), params)
        written += len(batch)
    conn.commit()
    return written


SENTIMENT_FIELDS = (
    "security_id", "instrument_type", "trade_date",
    # the classification - market-classifier layer, daily frame. The SAME
    # rules the intraday classifier runs on 5-minute candles. (bias, score,
    # max_score and confidence were removed 2026-11-19 - Amar.)
    "structure", "regime", "volatility", "swing_direction", "swing_high", "swing_low",
    "structure_determined", "range_used", "volatility_expanding", "gap_pct",
    "pd_high", "pd_low", "pd_close",
    "rsi", "sma9", "sma50", "sma100", "sma200", "prev_volume",
    "avg_volume_50", "vix", "price", "expected_move", "upper_volatility",
    "lower_volatility", "min15_high", "min15_low", "created_at",
)

SENTIMENT_UPSERT = """
INSERT INTO algo.daily_market_sentiment ({columns})
VALUES ({placeholders})
ON CONFLICT (security_id, instrument_type, trade_date) DO UPDATE SET
{updates}
""".format(
    columns=", ".join(SENTIMENT_FIELDS),
    placeholders=", ".join(["%s"] * len(SENTIMENT_FIELDS)),
    updates=",\n".join(
        f"    {f} = EXCLUDED.{f}"
        for f in SENTIMENT_FIELDS
        if f not in ("security_id", "instrument_type", "trade_date")
    ),
)


def upsert_sentiment(conn, row):
    cursor = conn.cursor()
    cursor.execute(SENTIMENT_UPSERT, tuple(row[f] for f in SENTIMENT_FIELDS))
    conn.commit()


PROFILE_FIELDS = (
    "security_id", "instrument_type", "slot_ts", "trade_date",
    "normal_width_pct", "samples", "sessions", "first_session", "last_session",
    "created_at",
)
PROFILE_KEY = ("security_id", "instrument_type", "slot_ts")

PROFILE_UPSERT = """
INSERT INTO algo.intraday_width_profile ({columns})
VALUES {{values}}
ON CONFLICT ({key}) DO UPDATE SET
{updates}
""".format(
    columns=", ".join(PROFILE_FIELDS),
    key=", ".join(PROFILE_KEY),
    updates=",\n".join(
        f"    {f} = EXCLUDED.{f}" for f in PROFILE_FIELDS if f not in PROFILE_KEY
    ),
)


def upsert_width_profile(conn, instrument, trade_date, rows, used_midnights, created_at):
    """
    One session's 75 slot rows in ONE statement (10 params a row, 750 in all).

    An upsert, so a re-run on the same morning rewrites the identical rows -
    the inputs are completed past sessions, which do not change.
    """
    if not rows:
        raise RuntimeError("width profile has no rows - refusing an empty write")
    params = []
    for row in rows:
        params.extend([
            instrument["security_id"], instrument["instrument_type"],
            int(row["slot_ts"]), int(trade_date),
            row["normal_width_pct"], int(row["samples"]),
            len(used_midnights), int(used_midnights[0]), int(used_midnights[-1]),
            int(created_at),
        ])
    placeholders = ", ".join(
        ["(" + ", ".join(["%s"] * len(PROFILE_FIELDS)) + ")"] * len(rows)
    )
    cursor = conn.cursor()
    cursor.execute(PROFILE_UPSERT.format(values=placeholders), params)
    conn.commit()
    return len(rows)


# ---- the morning brief (2026-11-22): READ-ONLY ---------------------------------
PREV_SNAPSHOT_FIELDS = (
    "snapshot_ts", "captured_at", "spot", "near_expiry_ts", "near_atm_strike",
    "near_max_oi_call", "near_max_oi_put", "near_max_pain", "near_pcr_oi",
    "near_ce_oi_total", "near_pe_oi_total",
)


def read_prev_session_snapshot(conn, instrument, day_start):
    """
    The previous session's LAST intraday-fno-poller snapshot (its 15:25 tick):
    yesterday's walls, max pain, PCR and OI totals, for the brief's
    "yesterday's close" line and today's OI change. Read only. None when the
    table holds nothing before today (sessions before the poller existed).
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(PREV_SNAPSHOT_FIELDS)} FROM algo.fno_5m_snapshot "
        "WHERE security_id = %s AND instrument_type = %s AND snapshot_ts < %s "
        "ORDER BY snapshot_ts DESC LIMIT 1",
        (instrument["security_id"], instrument["instrument_type"], int(day_start)),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {f: (float(v) if v is not None and f not in ("snapshot_ts", "captured_at", "near_expiry_ts") else v)
            for f, v in zip(PREV_SNAPSHOT_FIELDS, row)}


# ---- the evening brief (2026-11-23): READ-ONLY ---------------------------------
# Each returns today's rows (or a count) and never writes; the handler guards each
# one separately, so a table that cannot be read costs only its own section.
_INTS = ("ts", "snapshot_ts", "near_expiry_ts", "trigger_ts", "telegram_sent_at", "manager_sent_at",
         "entry_ts", "lot_size", "t1_booked_ts")


def _rows(conn, sql, params, fields):
    cursor = conn.cursor()
    cursor.execute(sql, params)
    out = []
    for row in cursor.fetchall():
        rec = {}
        for f, v in zip(fields, row):
            if v is None or isinstance(v, str):
                rec[f] = v
            elif f in _INTS:
                rec[f] = int(v)
            else:
                rec[f] = float(v)
        out.append(rec)
    return out


def read_day_bars(conn, instrument, day_start):
    """Today's stored 5-min candles of the instrument (the loader's), oldest first."""
    return _rows(conn,
                 "SELECT candle_ts, open, high, low, close FROM algo.candle_5min "
                 "WHERE security_id = %s AND instrument_type = %s AND candle_ts >= %s AND candle_ts < %s "
                 "ORDER BY candle_ts",
                 (instrument["security_id"], instrument["instrument_type"], int(day_start), int(day_start) + 86400),
                 ("ts", "open", "high", "low", "close"))


def read_day_sentiments(conn, instrument, day_start):
    """Today's 15-min rows: when they were stamped and the day_trend each carried."""
    return _rows(conn,
                 "SELECT snapshot_ts, day_trend FROM algo.intraday_sentiments "
                 "WHERE security_id = %s AND instrument_type = %s AND snapshot_ts >= %s AND snapshot_ts < %s "
                 "ORDER BY snapshot_ts",
                 (instrument["security_id"], instrument["instrument_type"], int(day_start), int(day_start) + 86400),
                 ("snapshot_ts", "day_trend"))


def count_day_fno_rows(conn, instrument, day_start):
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM algo.intraday_fno_data "
                   "WHERE security_id = %s AND instrument_type = %s AND snapshot_ts >= %s AND snapshot_ts < %s",
                   (instrument["security_id"], instrument["instrument_type"], int(day_start), int(day_start) + 86400))
    return int(cursor.fetchone()[0])


SNAPSHOT_FIELDS = ("snapshot_ts", "near_expiry_ts", "near_atm_strike", "near_max_oi_call", "near_max_oi_put",
                   "near_max_pain", "near_pcr_oi")


def read_day_snapshots(conn, instrument, day_start):
    """Today's intraday-fno-poller 5-min snapshots (the walls, max pain, PCR), oldest first."""
    return _rows(conn,
                 f"SELECT {', '.join(SNAPSHOT_FIELDS)} FROM algo.fno_5m_snapshot "
                 "WHERE security_id = %s AND instrument_type = %s AND snapshot_ts >= %s AND snapshot_ts < %s "
                 "ORDER BY snapshot_ts",
                 (instrument["security_id"], instrument["instrument_type"], int(day_start), int(day_start) + 86400),
                 SNAPSHOT_FIELDS)


def read_day_alerts(conn, instrument, day_start):
    """Today's pattern-recorder events that went to Telegram (and whether the manager got them)."""
    return _rows(conn,
                 "SELECT trigger_ts, family, direction, level, telegram_sent_at, manager_sent_at FROM algo.pattern_events "
                 "WHERE security_id = %s AND instrument_type = %s AND trigger_ts >= %s AND trigger_ts < %s "
                 "AND telegram_sent_at IS NOT NULL ORDER BY trigger_ts",
                 (instrument["security_id"], instrument["instrument_type"], int(day_start), int(day_start) + 86400),
                 ("trigger_ts", "family", "direction", "level", "telegram_sent_at", "manager_sent_at"))


def read_day_positions(conn, day_start):
    """Today's paper positions (order-manager's algo.position_master, P2a: the sweep)."""
    return _rows(conn,
                 "SELECT playbook, entry_ts, position_type, status, lot_size, realized_profit, exit_reason, t1_booked_ts "
                 "FROM algo.position_master WHERE session_start = %s ORDER BY entry_ts",
                 (int(day_start),),
                 ("playbook", "entry_ts", "position_type", "status", "lot_size", "realized_profit", "exit_reason",
                  "t1_booked_ts"))
