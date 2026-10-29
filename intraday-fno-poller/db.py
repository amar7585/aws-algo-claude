"""
Neon access.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

Writes algo.fno_5m_snapshot and algo.fno_5m_leg, in ONE transaction, and
deletes the legs it is about to replace. Reads algo.instrument_master and its
own previous row.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders) and has no execute_values equivalent, so the
multi-row insert below builds its own VALUES tuples.

THE COLUMN LISTS ARE THE SOURCE OF TRUTH. Both statements are generated from
one ordered tuple per table rather than written out three times - once in the
INSERT list, once in the placeholders, once in the DO UPDATE SET. Those three
drifting apart is how a row silently acquires a value under the wrong column
name, and the generated form makes that impossible rather than merely
unlikely. Keep the tuples in step with schema.sql.
"""

import logging

from config import UPSERT_BATCH_SIZE

logger = logging.getLogger()

SNAPSHOT_TABLE = "algo.fno_5m_snapshot"
LEG_TABLE = "algo.fno_5m_leg"


def _expiry_columns(prefix):
    """The aggregate block for one expiry - the same names as intraday_fno_data."""
    return tuple(f"{prefix}_{name}" for name in (
        "expiry_ts", "atm_strike", "ce_ltp", "pe_ltp", "straddle", "straddle_pct",
        "pcr_oi", "pcr_volume", "ce_oi_total", "pe_oi_total",
        "ce_oi_change", "pe_oi_change", "ce_oi_change_pct", "pe_oi_change_pct",
        "max_oi_call", "max_oi_put", "max_pain", "ce_iv", "pe_iv", "iv_skew",
    ))


SNAPSHOT_KEY = ("security_id", "instrument_type", "snapshot_ts")
SNAPSHOT_COLUMNS = SNAPSHOT_KEY + (
    "captured_at", "prev_snapshot_ts",
    # ---- underlying index
    "spot", "chain_spot", "spot_change_pct", "idx_bar_ts", "idx_bar_volume",
    # ---- current-month future. fut_price / fut_oi are LIVE (the forming bar);
    # fut_bar_* are the last CLOSED bar, and fut_bar_oi_change is that bar's OI
    # minus the bar before it, taken from the same Dhan response.
    "fut_security_id", "fut_symbol", "fut_price", "fut_oi", "basis", "basis_pct",
    "fut_price_change_pct", "fut_oi_change", "fut_oi_change_pct",
    "fut_bar_ts", "fut_bar_volume", "fut_bar_oi", "fut_bar_oi_change",
) + _expiry_columns("near") + _expiry_columns("mth") + ("created_at",)

LEG_KEY = (
    "security_id", "instrument_type", "snapshot_ts", "expiry_ts",
    "strike", "option_type",
)
LEG_COLUMNS = LEG_KEY + (
    "option_security_id", "last_price", "oi", "previous_oi", "volume",
    "previous_volume", "previous_close_price", "average_price",
    "implied_volatility", "delta", "theta", "gamma", "vega",
    "top_bid_price", "top_bid_quantity", "top_ask_price", "top_ask_quantity",
    "created_at",
)

# The columns the previous-tick baseline is read back through. The contract
# identities (fut_security_id and the two expiry timestamps) are here so the
# caller can tell whether a price or an OI total from that row describes the
# same contract as this tick.
BASELINE_COLUMNS = (
    "snapshot_ts", "spot", "fut_security_id", "fut_price", "fut_oi",
    "near_expiry_ts", "near_ce_oi_total", "near_pe_oi_total",
    "mth_expiry_ts", "mth_ce_oi_total", "mth_pe_oi_total",
)


def _upsert_sql(table, columns, key):
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in key)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES {{values}} "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}"
    )


SNAPSHOT_UPSERT = _upsert_sql(SNAPSHOT_TABLE, SNAPSHOT_COLUMNS, SNAPSHOT_KEY)
LEG_UPSERT = _upsert_sql(LEG_TABLE, LEG_COLUMNS, LEG_KEY)

# Every leg of one snapshot, whatever its strike - the leg key's first three
# columns are the snapshot key.
LEG_DELETE = (
    f"DELETE FROM {LEG_TABLE} WHERE "
    + " AND ".join(f"{c} = %s" for c in SNAPSHOT_KEY)
)


def _instrument(row):
    return {
        "security_id": row[0],
        "trading_symbol": row[1],
        "exchange_segment": row[2],
        "instrument_type": row[3],
    }


_INSTRUMENT_COLUMNS = (
    "SELECT security_id, trading_symbol, exchange_segment, instrument_type "
    "FROM algo.instrument_master "
)


def resolve_instrument(conn, security_id, instrument_type):
    """
    Resolve on the FULL identity, never security_id alone.

    security_id is NOT unique. 19 security_ids in instrument_master carry more
    than one instrument_type, and `13` is both NIFTY (IDX_I/INDEX) and ABB
    (NSE_EQ/EQUITY).
    """
    cursor = conn.cursor()
    cursor.execute(
        _INSTRUMENT_COLUMNS + "WHERE security_id = %s AND instrument_type = %s",
        (str(security_id), str(instrument_type)),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{security_id}/{instrument_type} matched {len(rows)} rows in "
            f"algo.instrument_master, expected exactly 1"
            + ("" if rows else " - run instrument-master-loader first")
        )
    instrument = _instrument(rows[0])
    logger.info(
        "resolved %s/%s -> %s on %s",
        security_id, instrument_type, instrument["trading_symbol"],
        instrument["exchange_segment"],
    )
    return instrument


def resolve_by_symbol(conn, trading_symbol, instrument_type):
    """
    Resolve a contract by its trading symbol - used for the current-month
    future, whose security_id changes every month and is therefore never
    hardcoded.
    """
    cursor = conn.cursor()
    cursor.execute(
        _INSTRUMENT_COLUMNS + "WHERE trading_symbol = %s AND instrument_type = %s",
        (str(trading_symbol), str(instrument_type)),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{trading_symbol}/{instrument_type} matched {len(rows)} rows in "
            f"algo.instrument_master, expected exactly 1"
            + (
                ""
                if rows
                else " - the instrument master predates this contract; run "
                "instrument-master-loader"
            )
        )
    instrument = _instrument(rows[0])
    logger.info(
        "resolved %s -> security_id %s on %s",
        trading_symbol, instrument["security_id"], instrument["exchange_segment"],
    )
    return instrument


def previous_tick(conn, instrument, snapshot_ts, session_midnight):
    """
    The newest tick strictly before this one ON THE SAME IST DAY, or None.

    UNLIKE the slow row, this baseline does NOT reach back across the overnight
    gap. At a 15-minute cadence the first snapshot's deltas spanning the night
    is a documented, intended read; at five minutes a `*_change_pct` that
    silently covers sixteen hours is a trap, because every other row's covers
    five minutes. So the first tick of a session has null deltas, and
    prev_snapshot_ts says which window every other delta covers.

    A missed tick widens the window rather than nulling it - the caller stores
    prev_snapshot_ts so the width stays visible.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(BASELINE_COLUMNS)} FROM {SNAPSHOT_TABLE} "
        "WHERE security_id = %s AND instrument_type = %s "
        "AND snapshot_ts < %s AND snapshot_ts >= %s "
        "ORDER BY snapshot_ts DESC LIMIT 1",
        (
            instrument["security_id"], instrument["instrument_type"],
            int(snapshot_ts), int(session_midnight),
        ),
    )
    row = cursor.fetchone()
    if not row:
        logger.info("no earlier tick today - every change column will be null")
        return None
    baseline = dict(zip(BASELINE_COLUMNS, row))
    logger.info("baseline is snapshot_ts %s", baseline["snapshot_ts"])
    return baseline


def write_snapshot(conn, row, legs):
    """
    The tick row and its raw legs, in ONE transaction.

    Both or neither. A committed row whose legs failed would look complete to
    every reader - the aggregates are on the row itself, so nothing downstream
    would notice the legs were missing.

    The upsert is what makes a re-run idempotent: snapshot_ts is the newest
    bar, not the run clock, so an invocation repeated a minute later
    overwrites its own row rather than adding a near-duplicate.

    THE LEGS ARE REPLACED WHOLE, NOT UPSERTED OVER. The leg key includes the
    strike, so an upsert alone overwrites only the strikes both runs cover:
    when ATM moves between two runs that share a snapshot_ts, the first run's
    outer legs stay behind under a row the second run wrote. Measured
    2026-10-29: the 15:26 and 15:31 ticks both stamped 15:25, and five of seven
    sessions kept 2-4 legs from 15:26 beside the 15:31 tick's ten. Deleting
    the snapshot's legs first, inside this transaction, leaves exactly the legs
    of the run that wrote the row - and a failure rolls the delete back too.
    """
    cursor = conn.cursor()
    try:
        missing = [c for c in SNAPSHOT_COLUMNS if c not in row]
        if missing:
            raise RuntimeError(
                f"tick row is missing {missing} - refusing to write a partial row"
            )
        cursor.execute(
            SNAPSHOT_UPSERT.format(
                values="(" + ", ".join(["%s"] * len(SNAPSHOT_COLUMNS)) + ")"
            ),
            [row[c] for c in SNAPSHOT_COLUMNS],
        )
        cursor.execute(LEG_DELETE, [row[c] for c in SNAPSHOT_KEY])

        written = 0
        for start in range(0, len(legs), UPSERT_BATCH_SIZE):
            batch = legs[start : start + UPSERT_BATCH_SIZE]
            params = []
            for leg in batch:
                absent = [c for c in LEG_COLUMNS if c not in leg]
                if absent:
                    raise RuntimeError(f"a leg is missing {absent} - refusing to write")
                if any(leg[c] != row[c] for c in SNAPSHOT_KEY):
                    # The delete above cleared the ROW's snapshot only.
                    raise RuntimeError("a leg's snapshot key differs from its row's")
                params.extend(leg[c] for c in LEG_COLUMNS)
            placeholders = ", ".join(
                ["(" + ", ".join(["%s"] * len(LEG_COLUMNS)) + ")"] * len(batch)
            )
            cursor.execute(LEG_UPSERT.format(values=placeholders), params)
            written += len(batch)
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
