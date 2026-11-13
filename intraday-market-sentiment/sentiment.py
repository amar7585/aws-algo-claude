"""
The session read: what the candles say.

Pure functions over candle lists and numbers. No Dhan, no Postgres - the whole
module runs against a saved payload. The buildup label it used to compute moved
to market-classifier, which derives it from the two futures deltas this
snapshot measures (fut_price_change_pct, fut_oi_change_pct).
"""

import logging

from neon_access import ist_datetime

from config import ORB_END, SESSION_START

logger = logging.getLogger()


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


def bar_volume_stats(candles, now_epoch, interval_seconds):
    """
    The volume of the last FULLY CLOSED bar, against the average of every
    closed bar so far today.

    CLOSED IS THE SAME TEST AS EVERYWHERE ELSE IN THIS PLANE:
    candle_ts + interval_seconds <= now. NOT "before snapshot_ts" - that rule
    is wrong on exactly the day's last snapshot. At the 15:35 run, snapshot_ts
    is 15:25 (see newest_bar) and that bar IS closed, because the market shut
    at 15:30 - "strictly before 15:25" would skip it and grab 15:20 instead,
    silently reporting a stale bar's volume as if it were the closing one. The
    closed-bar test used here includes 15:25 in that case, correctly, without
    a special-cased branch.

    On the first run of a session (10:00) "today so far" is 09:15-09:55,
    eight real closed bars - there is history to average against even though
    no snapshot row exists yet for that stretch.
    """
    closed = [c for c in candles if c["ts"] + interval_seconds <= now_epoch]
    if not closed:
        return {"last_bar_volume": None, "volume_vs_avg": None}
    last_closed = max(closed, key=lambda c: c["ts"])
    avg = sum(c["volume"] for c in closed) / len(closed)
    return {
        "last_bar_volume": last_closed["volume"],
        "volume_vs_avg": (last_closed["volume"] / avg) if avg else None,
    }


def recent_range(candles, now_epoch, interval_seconds, profile, bars):
    """
    How wide the last `bars` CLOSED candles were, against what is normal for
    their clock slots. The input to market-classifier's volatility test
    (agreed with Amar 2026-11-12; it replaces "the day's range since 09:15 vs
    the VIX-implied move x sqrt(time)", which the opening range alone tripped
    on 82% of 09:45 runs).

    candles    today's bars, the forming one included - it is dropped here by
               the same closed test as everywhere else in this plane
               (candle_ts + interval <= now), so the 10:00 run judges 09:30..09:55.
    profile    {slot_ts: normal_width_pct} for today, or None (see db.width_profile).

    Width is (high - low) / close x 100 per candle; recent_range_pct is the sum
    over the window and recent_range_vs_normal that sum over the sum of the
    same slots' normals - 1.0 is an ordinary half hour for that time of day.

    Returns the three columns plus `reason`. The width is stored whenever the
    window is complete; the ratio is None, with a reason, when the profile is
    missing or lacks one of the window's slots - the caller then fails the run.
    """
    closed = sorted(
        (c for c in candles if c["ts"] + interval_seconds <= now_epoch),
        key=lambda c: c["ts"],
    )
    window = closed[-bars:]
    out = {
        "recent_range_pct": None,
        "recent_range_vs_normal": None,
        "recent_range_from_ts": None,
        "reason": None,
    }
    if len(window) < bars:
        out["reason"] = f"only {len(window)} closed candle(s) today, need {bars}"
        return out
    width = sum(
        (float(c["high"]) - float(c["low"])) / float(c["close"]) * 100 for c in window
    )
    out["recent_range_pct"] = width
    out["recent_range_from_ts"] = int(window[0]["ts"])
    if not profile:
        out["reason"] = "no width profile for today"
        return out
    missing = [c["ts"] for c in window if c["ts"] not in profile]
    if missing:
        out["reason"] = (
            f"the width profile has no slot for "
            f"{', '.join(ist_datetime(t).strftime('%H:%M') for t in missing)}"
        )
        return out
    normal = sum(profile[c["ts"]] for c in window)
    out["recent_range_vs_normal"] = width / normal if normal else None
    if not normal:
        out["reason"] = "the window's normal width sums to zero"
    return out


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


