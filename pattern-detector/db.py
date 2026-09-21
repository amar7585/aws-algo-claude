"""
Neon read for pattern-detector: the future's recent 5-min bars.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo - the same rule every function here follows. READS ONLY: the
detector writes nothing yet, the way strategy-range-liquidity-sweep does - its
output is its log, and a table comes once the rule is proven.

candle_5min is keyed (security_id, instrument_type, candle_ts). The future is
queried on the FULL identity (hard rule #4), the instrument_type from
config.FUTURES_INSTRUMENT_TYPE - the same value the loader stored its bars
under, so this cannot silently read the wrong contract.
"""

import logging

from config import FUT_BARS_LOOKBACK, FUTURES_INSTRUMENT_TYPE

logger = logging.getLogger()

CANDLE_5MIN = "algo.candle_5min"
FNO_DATA = "algo.intraday_fno_data"


def read_prev_pcr(conn, security_id, instrument_type, prev_snapshot_ts):
    """
    near_pcr_oi on the PREVIOUS snapshot's fno row, or None.

    The continuation confirmation wants PCR DIRECTION (rising / falling), and the
    fno row in the payload carries only the current PCR. One cheap SELECT on the
    prior snapshot gives the comparison. None when there is no prior snapshot
    (the first run of the day) or the row/value is absent - the caller then omits
    the PCR leg rather than inventing a direction.
    """
    if not prev_snapshot_ts:
        return None
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT near_pcr_oi FROM {FNO_DATA} "
        f"WHERE security_id = %s AND instrument_type = %s AND snapshot_ts = %s",
        (str(security_id), str(instrument_type), int(prev_snapshot_ts)),
    )
    row = cursor.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def read_bars(conn, security_id, instrument_type, snapshot_ts):
    """
    The newest FUT_BARS_LOOKBACK 5-min bars for one instrument at or before
    snapshot_ts, oldest-first. The full identity (security_id, instrument_type)
    is passed - hard rule #4 - so this never reads the wrong contract. Bounding
    on snapshot_ts rather than the clock is what makes a re-run reach the same
    answer instead of merely repeating.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT candle_ts, open, high, low, close, volume FROM {CANDLE_5MIN} "
        f"WHERE security_id = %s AND instrument_type = %s AND candle_ts <= %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (str(security_id), str(instrument_type), int(snapshot_ts),
         FUT_BARS_LOOKBACK),
    )
    rows = cursor.fetchall()
    bars = [
        {
            "ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": float(r[5] or 0),
        }
        for r in rows
    ]
    bars.reverse()  # oldest-first for the z-score and window scan
    return bars


def read_future_bars(conn, fut_security_id, snapshot_ts):
    """The future's recent 5-min bars - the reversal candidate's price+volume."""
    return read_bars(conn, fut_security_id, FUTURES_INSTRUMENT_TYPE, snapshot_ts)
