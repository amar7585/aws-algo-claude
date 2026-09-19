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

import decimal
import logging

from config import UPSERT_BATCH_SIZE

logger = logging.getLogger()

SNAPSHOT_TABLE = "algo.intraday_fno_data"
CHAIN_TABLE = "algo.option_chain_snapshot"

SNAPSHOT_KEY = ("security_id", "instrument_type", "snapshot_ts")
SNAPSHOT_COLUMNS = SNAPSHOT_KEY + (
    "captured_at", "prev_snapshot_ts",
    "spot", "chain_spot", "spot_change_pct", "day_high", "day_low", "vwap",
    "orb_high", "orb_low", "last_bar_volume", "volume_vs_avg",
    "fut_security_id", "fut_symbol", "fut_price", "fut_oi", "basis", "basis_pct",
    "fut_price_change_pct", "fut_oi_change_pct",
    # buildup MOVED to algo.intraday_sentiments (the classifier derives it).
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
    # Indicators (MEASURED): the SMAs and RSI the classifier scores from,
    # computed here because only this function fetches the ~200-bar history
    # sma200 needs. The classification they feed - regime, structure, bias and
    # the swing/volatility reads - is written to algo.intraday_sentiments by the
    # market-classifier function, not here.
    "sma9", "sma50", "sma100", "sma200", "rsi",
    # Market breadth (MEASURED): advance/decline for the NIFTY 50 (nifty_*) and
    # the NIFTY 500 (mkt_*), counted from one /marketfeed/ohlc fetch. Null as a
    # block when breadth is disabled; see breadth.py. Kept in step with
    # breadth.BREADTH_COLUMNS and the schema.
    "nifty_advances", "nifty_declines", "nifty_unchanged", "nifty_adv_dec_ratio",
    "mkt_advances", "mkt_declines", "mkt_unchanged", "mkt_adv_dec_ratio",
    "mkt_sampled",
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


def read_constituents(conn, index_names):
    """
    The members of each named index, for the breadth read.

    Returns {index_name: [(exchange_segment, security_id), ...]}, with an empty
    list for an index carrying no rows. security_id is returned as text (it is
    text in instrument identity, and /marketfeed/ohlc keys quotes by security id
    as a string), and exchange_segment is the raw Dhan code the request groups
    by - so the caller needs no join.
    """
    if not index_names:
        return {}
    cursor = conn.cursor()
    placeholders = ", ".join(["%s"] * len(index_names))
    cursor.execute(
        f"SELECT index_name, exchange_segment, security_id "
        f"FROM algo.index_constituents WHERE index_name IN ({placeholders})",
        [str(n) for n in index_names],
    )
    rosters = {str(name): [] for name in index_names}
    for name, segment, security_id in cursor.fetchall():
        rosters.setdefault(str(name), []).append((segment, str(security_id)))
    for name, members in rosters.items():
        logger.info("index_constituents: %s has %d member(s)", name, len(members))
    return rosters


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


DAILY_SENTIMENT_TABLE = "algo.daily_market_sentiment"

# Everything a playbook needs off the daily row. pd_high/pd_low are the
# previous day's extremes, upper/lower_volatility bracket the VIX-implied move,
# and the classification columns are on the SAME scale as this row's own -
# max_score differs between the frames, which is why it is carried.
DAILY_SENTIMENT_COLUMNS = (
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


def daily_sentiment(conn, instrument, session_midnight):
    """
    The daily read in force for the session starting at `session_midnight`.

    THE LOOKUP IS NOT trade_date = TODAY, AND THAT IS THE WHOLE POINT. A daily
    row's trade_date is the session it DESCRIBES, which is the newest completed
    daily candle - yesterday - because Dhan's daily endpoint lags a session.
    The row daily-market-sentiment writes this morning at 09:35 is stamped
    YESTERDAY. Verified against the live table: the only stored row carries
    trade_date 2026-09-10 and was written during the 2026-09-11 session.

    So the row in force today is the newest one stamped BEFORE today, not one
    stamped today - which never exists.

    FRESHNESS IS REPORTED, NOT ASSUMED. Taking "the newest row before today"
    alone would silently return last Tuesday's read if this morning's 09:35 run
    failed, and hand a playbook a stale pd_high as today's level - a wrong
    level rather than a missing one, which is worse. created_at says when the
    row was actually written, so the caller can tell today's read from a stale
    one; `stale` carries that answer rather than leaving it to be inferred.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(DAILY_SENTIMENT_COLUMNS)} FROM {DAILY_SENTIMENT_TABLE} "
        f"WHERE security_id = %s AND instrument_type = %s AND trade_date < %s "
        f"ORDER BY trade_date DESC LIMIT 1",
        (
            str(instrument["security_id"]),
            str(instrument["instrument_type"]),
            int(session_midnight),
        ),
    )
    rows = cursor.fetchall()
    if not rows:
        logger.warning(
            "no %s row before %d - daily-market-sentiment has never run for "
            "this instrument, or has never succeeded",
            DAILY_SENTIMENT_TABLE, int(session_midnight),
        )
        return None

    row = dict(zip(DAILY_SENTIMENT_COLUMNS, rows[0]))
    row = {k: float(v) if isinstance(v, decimal.Decimal) else v
           for k, v in row.items()}
    row["stale"] = int(row["created_at"] or 0) < int(session_midnight)
    if row["stale"]:
        logger.warning(
            "daily read for trade_date %s was written at %s, before today - "
            "this morning's 09:35 run did not land",
            row["trade_date"], row["created_at"],
        )
    return row


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
