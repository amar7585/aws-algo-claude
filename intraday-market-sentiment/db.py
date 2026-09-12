"""
Neon access.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders) and has no execute_values equivalent, so the
multi-row insert below builds its own VALUES tuples.

THE COLUMN LISTS ARE THE SOURCE OF TRUTH. Both statements are generated from
one ordered tuple per table rather than written out three times - once in the
INSERT list, once in the placeholders, once in the DO UPDATE SET. Those three
drifting apart is how a row silently acquires a value under the wrong column
name, and the generated form makes that impossible rather than merely
unlikely.
"""

import logging

from config import UPSERT_BATCH_SIZE

logger = logging.getLogger()

SNAPSHOT_TABLE = "algo.intraday_market_sentiment"
CHAIN_TABLE = "algo.option_chain_snapshot"

SNAPSHOT_KEY = ("security_id", "instrument_type", "snapshot_ts")
SNAPSHOT_COLUMNS = SNAPSHOT_KEY + (
    "captured_at", "prev_snapshot_ts",
    "spot", "chain_spot", "spot_change_pct", "day_high", "day_low", "vwap",
    "orb_high", "orb_low",
    "fut_security_id", "fut_symbol", "fut_price", "fut_oi", "basis", "basis_pct",
    "fut_price_change_pct", "fut_oi_change_pct", "buildup",
    "vix", "vix_open", "vix_day_high", "vix_day_low", "vix_change_pct",
    "near_expiry_ts", "near_atm_strike", "near_ce_ltp", "near_pe_ltp",
    "near_straddle", "near_straddle_pct", "near_pcr_oi", "near_pcr_volume",
    "near_ce_oi_total", "near_pe_oi_total", "near_ce_oi_change_pct",
    "near_pe_oi_change_pct", "near_max_oi_call", "near_max_oi_put",
    "near_max_pain", "near_ce_iv", "near_pe_iv", "near_iv_skew",
    "mth_expiry_ts", "mth_atm_strike", "mth_ce_ltp", "mth_pe_ltp",
    "mth_straddle", "mth_straddle_pct", "mth_pcr_oi", "mth_pcr_volume",
    "mth_ce_oi_total", "mth_pe_oi_total", "mth_ce_oi_change_pct",
    "mth_pe_oi_change_pct", "mth_max_oi_call", "mth_max_oi_put",
    "mth_max_pain", "mth_ce_iv", "mth_pe_iv", "mth_iv_skew",
    "created_at",
)

CHAIN_KEY = (
    "security_id", "instrument_type", "snapshot_ts", "expiry_ts",
    "strike", "option_type",
)
CHAIN_COLUMNS = CHAIN_KEY + (
    "option_security_id", "last_price", "oi", "previous_oi", "volume",
    "previous_volume", "previous_close_price", "average_price",
    "implied_volatility", "delta", "theta", "gamma", "vega",
    "top_bid_price", "top_bid_quantity", "top_ask_price", "top_ask_quantity",
    "created_at",
)

# The columns the previous-snapshot baseline is read back through. Every
# *_change_pct on a row is computed against these, and the two expiry
# timestamps are here so the caller can tell whether an OI total from that row
# describes the same contract as this one.
BASELINE_COLUMNS = (
    "snapshot_ts", "spot", "fut_price", "fut_oi", "vix",
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
CHAIN_UPSERT = _upsert_sql(CHAIN_TABLE, CHAIN_COLUMNS, CHAIN_KEY)


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
    (NSE_EQ/EQUITY). A lookup on security_id alone silently resolved to ABB on
    2026-09-11 in daily-market-sentiment and wrote 204 ABB candles labelled
    NIFTY.
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


def previous_snapshot(conn, instrument, snapshot_ts):
    """
    The newest snapshot strictly before this one, or None.

    NOT restricted to today. On the first run of a session that makes the
    baseline the PREVIOUS session's 15:30 row, so the 09:35 deltas span the
    overnight gap - which is the intended behaviour, not a fallback. A missed
    run widens the window the same way. Either way the caller stores
    prev_snapshot_ts, so the window every delta covers stays visible instead
    of being assumed to be fifteen minutes.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(BASELINE_COLUMNS)} FROM {SNAPSHOT_TABLE} "
        "WHERE security_id = %s AND instrument_type = %s AND snapshot_ts < %s "
        "ORDER BY snapshot_ts DESC LIMIT 1",
        (instrument["security_id"], instrument["instrument_type"], int(snapshot_ts)),
    )
    row = cursor.fetchone()
    if not row:
        logger.info("no earlier snapshot - every change column will be null")
        return None
    baseline = dict(zip(BASELINE_COLUMNS, row))
    logger.info("baseline is snapshot_ts %s", baseline["snapshot_ts"])
    return baseline


def write_snapshot(conn, row, legs):
    """
    The snapshot row and its raw legs, in ONE transaction.

    Both or neither. A committed row whose legs failed would look complete to
    every reader - the aggregates are on the row itself, so nothing downstream
    would notice the legs were missing.

    The upsert is what makes a re-run idempotent: snapshot_ts is the last
    closed bar, not the run clock, so an invocation repeated at 09:36
    overwrites 09:30 rather than adding a near-duplicate.
    """
    cursor = conn.cursor()
    try:
        missing = [c for c in SNAPSHOT_COLUMNS if c not in row]
        if missing:
            raise RuntimeError(
                f"snapshot row is missing {missing} - refusing to write a "
                f"partial row"
            )
        cursor.execute(
            SNAPSHOT_UPSERT.format(
                values="(" + ", ".join(["%s"] * len(SNAPSHOT_COLUMNS)) + ")"
            ),
            [row[c] for c in SNAPSHOT_COLUMNS],
        )

        written = 0
        for start in range(0, len(legs), UPSERT_BATCH_SIZE):
            batch = legs[start : start + UPSERT_BATCH_SIZE]
            params = []
            for leg in batch:
                params.extend(leg[c] for c in CHAIN_COLUMNS)
            placeholders = ", ".join(
                ["(" + ", ".join(["%s"] * len(CHAIN_COLUMNS)) + ")"] * len(batch)
            )
            cursor.execute(CHAIN_UPSERT.format(values=placeholders), params)
            written += len(batch)
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
