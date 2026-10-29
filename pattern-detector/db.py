"""
Neon read for pattern-detector: the future's recent CLOSED 5-min bars.

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

from config import BAR_SECONDS, FUT_BARS_LOOKBACK, FUTURES_INSTRUMENT_TYPE

logger = logging.getLogger()

CANDLE_5MIN = "algo.candle_5min"
FNO_DATA = "algo.intraday_fno_data"


def read_prev_snapshot(conn, security_id, instrument_type, prev_snapshot_ts):
    """
    near_pcr_oi and captured_at on the PREVIOUS snapshot's fno row, or None.

    near_pcr_oi: the continuation confirmation wants PCR DIRECTION (rising /
    falling), and the fno row in the payload carries only the current PCR.
    captured_at: the previous run's clock, so a bar can be called fresh when it
    closed SINCE that run (detect.bar_closed_within). One cheap SELECT on the
    prior snapshot gives both. None when there is no prior snapshot or the row is
    absent - the caller then omits the PCR leg rather than inventing a direction,
    and falls back to one interval for freshness.
    """
    if not prev_snapshot_ts:
        return None
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT near_pcr_oi, captured_at FROM {FNO_DATA} "
        f"WHERE security_id = %s AND instrument_type = %s AND snapshot_ts = %s",
        (str(security_id), str(instrument_type), int(prev_snapshot_ts)),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "near_pcr_oi": float(row[0]) if row[0] is not None else None,
        "captured_at": int(row[1]) if row[1] is not None else None,
    }


def read_bars(conn, security_id, instrument_type, snapshot_ts, closed_by):
    """
    The newest FUT_BARS_LOOKBACK CLOSED 5-min bars for one instrument at or
    before snapshot_ts, oldest-first. The full identity (security_id,
    instrument_type) is passed - hard rule #4 - so this never reads the wrong
    contract.

    CLOSED ONLY. A bar is closed iff candle_ts + BAR_SECONDS <= closed_by, the
    same rule the loader and the poller use. closed_by is the snapshot's stored
    captured_at, so the bar at snapshot_ts - the one still forming, seconds old
    and near-empty, at every run but the last of the day - is left out, while
    the 15:25 bar on the 15:30 run (already closed) is kept. Reading the forming
    bar made its seconds of volume score as a "collapse" (2026-10-23 11:00 fired a
    TURN at z -3.0 on it) and made live disagree with any replay on final bars.
    Bounding on stored values rather than the clock is what makes a re-run reach
    the same answer instead of merely repeating.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT candle_ts, open, high, low, close, volume FROM {CANDLE_5MIN} "
        f"WHERE security_id = %s AND instrument_type = %s AND candle_ts <= %s "
        f"AND candle_ts + %s <= %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (str(security_id), str(instrument_type), int(snapshot_ts),
         BAR_SECONDS, int(closed_by), FUT_BARS_LOOKBACK),
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


def read_future_bars(conn, fut_security_id, snapshot_ts, closed_by):
    """The future's recent closed 5-min bars - the reversal candidate's price+volume."""
    return read_bars(conn, fut_security_id, FUTURES_INSTRUMENT_TYPE, snapshot_ts, closed_by)
