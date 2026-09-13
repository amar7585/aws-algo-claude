"""
The read itself: what the candles say, and the buildup label.

Pure functions over candle lists and numbers. No Dhan, no Postgres - the whole
module runs against a saved payload.

THIS FUNCTION CLASSIFIES, IT DOES NOT FORECAST. `buildup` names what price and
open interest just did together; it says nothing about what happens next, and
daily-market-sentiment's README already records that its own score is not
predictive of forward return. Treat this the same way.
"""

import logging

from neon_access import ist_datetime

from config import BUILDUP_EPSILON_PCT, ORB_END, SESSION_START

logger = logging.getLogger()

LONG_BUILDUP = "LONG_BUILDUP"
SHORT_BUILDUP = "SHORT_BUILDUP"
LONG_UNWINDING = "LONG_UNWINDING"
SHORT_COVERING = "SHORT_COVERING"
FLAT = "FLAT"


def newest_bar(candles, label):
    """
    The newest bar Dhan returned, INCLUDING the one still forming.

    THIS DELIBERATELY REPLACED A last_closed() RULE. The earlier design took
    the newest bar that had finished forming, so that no column on the row was
    ever partial. The schedule now aligns the whole intraday plane on the
    quarter hour - the loader fires at 10:00, commits, and invokes this
    function - and the agreed grain is the bar that is open at that moment:

        10:00 run -> the bucket stamped 10:00, seconds old, carrying the live
                     price. The loader completes that same bar at the 10:15
                     run, so snapshot_ts joins straight to candle_5min.
        15:30 run -> there is no 15:30 bucket, the market has closed, so the
                     newest bar returned is 15:25. The last snapshot of the
                     day is stamped 15:25 without a special case.

    WHAT IS ACTUALLY PARTIAL, AND WHAT IS NOT. The forming bar's own high, low
    and volume are near-empty at the instant of the run, and nothing scored
    reads them: the classification reads `close`, which is the live price, and
    the SMAs and RSI built from a series of closes. Session aggregates - day
    high/low, VWAP, the opening range - span every bar of the day, so a
    near-empty last bar moves them by nothing. The columns that would be
    misleading are simply not taken from this bar.
    """
    if not candles:
        raise RuntimeError(
            f"{label}: no bars returned - too early in the session, or the "
            f"exchange published nothing for this day"
        )
    return max(candles, key=lambda c: c["ts"])


def session_stats(candles, upto_ts):
    """
    Day high/low, VWAP and the opening range, over bars up to `upto_ts`.

    Bounded by upto_ts rather than taking the whole list so that every figure
    on the row describes the same instant as `spot` does. Including a partial
    bar's high here while spot came from the last closed one would put a
    day_high on the row that no closed bar supports.

    VWAP is volume-weighted on the typical price (h+l+c)/3. INDIA VIX carries
    volume 0 on every bar, so vwap is None there rather than a division by
    zero - which is why the caller only asks for it on the index.
    """
    bars = [c for c in candles if c["ts"] <= upto_ts]
    if not bars:
        raise RuntimeError(f"no bars at or before {upto_ts} to summarise")

    opening = [
        c for c in bars if SESSION_START <= ist_datetime(c["ts"]).time() < ORB_END
    ]
    volume = sum(c["volume"] for c in bars)
    weighted = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in bars)

    return {
        "open": bars[0]["open"],
        "high": max(c["high"] for c in bars),
        "low": min(c["low"] for c in bars),
        "close": max(bars, key=lambda c: c["ts"])["close"],
        "vwap": (weighted / volume) if volume else None,
        "orb_high": max((c["high"] for c in opening), default=None),
        "orb_low": min((c["low"] for c in opening), default=None),
        "bars": len(bars),
    }


def pct_change(current, previous):
    """
    Percentage change, or None when there is nothing to compare against.

    None rather than 0.0 is the point. The first snapshot of the system's life
    has no baseline, and an expiry roll invalidates one - both are "not known",
    and a 0.0 there reads as "did not move", which is a different claim.
    """
    if current is None or previous in (None, 0):
        return None
    return (float(current) - float(previous)) / abs(float(previous)) * 100


def buildup(price_change_pct, oi_change_pct, epsilon=None):
    """
    The four-way read of price against open interest.

        price up,   OI up    long buildup      new longs, conviction
        price down, OI up    short buildup     new shorts, conviction
        price up,   OI down  short covering    shorts closing, not new buying
        price down, OI down  long unwinding    longs closing, not new selling

    The distinction that matters is the second column: a rally on rising OI is
    money coming in, a rally on falling OI is money leaving. They look
    identical on a price chart.

    `epsilon` is the move below which a change counts as no change, in
    percent, and defaults to BUILDUP_EPSILON_PCT - 0.0, meaning pure sign.
    Left at 0 the label flips on the smallest tick; it is a tunable rather
    than a constant so a threshold can be set from measurement later without
    touching this logic.
    """
    if price_change_pct is None or oi_change_pct is None:
        return None
    epsilon = BUILDUP_EPSILON_PCT if epsilon is None else epsilon
    if abs(price_change_pct) <= epsilon or abs(oi_change_pct) <= epsilon:
        return FLAT
    if price_change_pct > 0:
        return LONG_BUILDUP if oi_change_pct > 0 else SHORT_COVERING
    return SHORT_BUILDUP if oi_change_pct > 0 else LONG_UNWINDING
