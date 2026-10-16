"""
The tick read: which bar is which, and what changed.

Pure functions over candle lists and numbers. No Dhan, no Postgres - the whole
module runs against a saved payload.

TWO BARS PER SERIES, AND THEY ARE NOT THE SAME BAR.

    snapshot bar   the NEWEST bar Dhan returned, including the one still
                   forming. It names the row (snapshot_ts) and supplies the live
                   price and the live open interest.
    closed bar     the newest bar that has FINISHED forming. Its volume and its
                   open interest are final, so they are what a 5-minute reading
                   compares. The forming bar's own volume and OI are seconds old
                   and near-empty - comparing them to a reference would read as
                   "volume collapsed".

The closed test is the one the rest of this plane uses:

    a bar is closed  iff  candle_ts + interval_seconds <= now

At the 15:31 run the newest bar is 15:25 and it IS closed (the market shut at
15:30), so the snapshot bar and the closed bar are the same bar. That falls out
of the test with no special case.
"""

import logging

from neon_access import ist_datetime

logger = logging.getLogger()


def newest_bar(candles, label):
    """The newest bar Dhan returned, INCLUDING the one still forming."""
    if not candles:
        raise RuntimeError(
            f"{label}: no bars returned - too early in the session, or the "
            f"exchange published nothing for this day"
        )
    return max(candles, key=lambda c: c["ts"])


def last_closed(candles, now_epoch, interval_seconds, label):
    """
    The newest FULLY CLOSED bar. Raises if there is none: a tick run inside
    the session always has at least the 09:15 bar closed, so an empty answer
    means the series or the clock is wrong, and a row written anyway would
    carry no reading at all.
    """
    closed = [c for c in candles if c["ts"] + interval_seconds <= now_epoch]
    if not closed:
        raise RuntimeError(
            f"{label}: no closed {interval_seconds // 60}-minute bar at "
            f"{ist_datetime(now_epoch):%H:%M} - nothing to read"
        )
    return max(closed, key=lambda c: c["ts"])


def bar_before(candles, bar):
    """The bar immediately before `bar` in the series, or None for the first."""
    earlier = [c for c in candles if c["ts"] < bar["ts"]]
    return max(earlier, key=lambda c: c["ts"]) if earlier else None


def bar_at(candles, snapshot_ts, label):
    """
    The bar stamped `snapshot_ts`, or the newest bar before it.

    The index sets snapshot_ts and every other series is aligned to it. A
    series that is short a bar at exactly that stamp falls back to its newest
    earlier one and says so - taking that series' own latest bar instead would
    compare two different minutes without a word.
    """
    exact = [c for c in candles if c["ts"] == snapshot_ts]
    if exact:
        return exact[0]
    earlier = [c for c in candles if c["ts"] < snapshot_ts]
    if not earlier:
        raise RuntimeError(
            f"{label} has no bar at or before {ist_datetime(snapshot_ts):%H:%M} "
            f"- the series does not cover this snapshot"
        )
    fallback = max(earlier, key=lambda c: c["ts"])
    logger.warning(
        "%s has no bar at %s - falling back to %s, %d minute(s) stale",
        label, ist_datetime(snapshot_ts).strftime("%H:%M"),
        ist_datetime(fallback["ts"]).strftime("%H:%M"),
        (snapshot_ts - fallback["ts"]) // 60,
    )
    return fallback


def pct_change(current, previous):
    """
    Percentage change, or None when there is nothing to compare against.

    None rather than 0.0 is the point. The first tick of a session has no
    baseline, and a contract roll invalidates one - both are "not known", and
    a 0.0 there reads as "did not move", which is a different claim.
    """
    if current is None or previous in (None, 0):
        return None
    return (float(current) - float(previous)) / abs(float(previous)) * 100


def change(current, previous):
    """Absolute change, or None when there is nothing to compare against."""
    if current is None or previous is None:
        return None
    return current - previous


def same_contract(baseline, key, value):
    """
    Whether the baseline row describes the same contract as this tick.

    Across a monthly futures roll the previous row's fut_price and fut_oi belong
    to a contract that no longer exists here; across an option expiry roll its
    OI totals do. A delta between the two is arithmetic on unrelated numbers
    that reads exactly like a real collapse in open interest. Measured
    2026-09-30 on the slow row: NIFTY-OCT2026-FUT against the expired
    September contract read +146% OI.

    `key` is the baseline column carrying the contract's identity; `value` is
    this tick's.
    """
    if not baseline:
        return False
    return str(baseline.get(key)) == str(value)
