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


def read_future_bars(conn, fut_security_id, snapshot_ts):
    """
    The future's newest FUT_BARS_LOOKBACK 5-min bars at or before snapshot_ts,
    returned oldest-first. Bounding on snapshot_ts rather than the clock is what
    makes a re-run reach the same answer instead of merely repeating.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT candle_ts, open, high, low, close, volume FROM {CANDLE_5MIN} "
        f"WHERE security_id = %s AND instrument_type = %s AND candle_ts <= %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (str(fut_security_id), FUTURES_INSTRUMENT_TYPE, int(snapshot_ts),
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
