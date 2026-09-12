"""
The liquidity pools, and which of them sit on top of each other.

A pool is a price where resting stop orders collect. The playbook lists four
kinds in priority order, and the whole setup is about price reaching for one,
failing, and rotating away - so getting these levels wrong does not produce a
worse trade, it produces a trade against a level nobody's stops are at.

EVERY POOL CARRIES A SIDE. A `high` pool is one price sweeps upward through
and a `low` pool one it sweeps downward through. The side decides the
direction of the resulting trade - a swept high is faded short - and keeping
it on the pool rather than inferring it later is what stops a long being
generated from a swept high.
"""

import datetime
import logging

from config import (
    FIRST_HOUR_END,
    IST,
    ORB_END,
    SESSION_END,
    SESSION_START,
    STACKED_POOL_PCT,
)

logger = logging.getLogger()

HIGH = "high"
LOW = "low"


def ist_datetime(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), IST)


def ist_time(epoch):
    return ist_datetime(epoch).time()


def session_bars(bars, day):
    """
    The bars belonging to `day`'s session, oldest first.

    SESSION_END is exclusive: bars are stamped at the START of their bucket,
    so the last legitimate 5-minute bar of a session is stamped 15:25.
    """
    kept = [
        bar
        for bar in bars
        if ist_datetime(bar["ts"]).date() == day
        and SESSION_START <= ist_time(bar["ts"]) < SESSION_END
    ]
    return sorted(kept, key=lambda bar: bar["ts"])


def _window(bars, end_time, start_time=None):
    start_time = SESSION_START if start_time is None else start_time
    return [bar for bar in bars if start_time <= ist_time(bar["ts"]) < end_time]


def opening_range(bars):
    """
    The 09:15-09:30 high and low, aggregated from the three 5-minute bars.

    NOT Dhan's native 15-minute bar. Its buckets are aligned from the session
    open in a way that does not match the chart these levels were marked on,
    and the playbook rules it out by name.
    """
    window = _window(bars, ORB_END)
    if not window:
        return None
    return {
        "high": max(bar["high"] for bar in window),
        "low": min(bar["low"] for bar in window),
        "bars": len(window),
        "complete": len(window) >= 3,
    }


def first_hour(bars):
    """The 09:15-10:15 high and low."""
    window = _window(bars, FIRST_HOUR_END)
    if not window:
        return None
    return {
        "high": max(bar["high"] for bar in window),
        "low": min(bar["low"] for bar in window),
        "bars": len(window),
        "complete": ist_time(window[-1]["ts"]) >= _minus_one_bar(FIRST_HOUR_END),
    }


def _minus_one_bar(end_time):
    """The stamp of the last 5-minute bar inside a window ending at end_time."""
    marker = datetime.datetime.combine(datetime.date(2000, 1, 1), end_time)
    return (marker - datetime.timedelta(minutes=5)).time()


def session_extremes(bars):
    """
    The high and low made AFTER the opening range.

    After, because the opening range's own extremes are already pools 1 and 2;
    a session extreme that is simply the opening-range extreme is not a fourth
    pool, it is the same pool counted twice.
    """
    window = [bar for bar in bars if ist_time(bar["ts"]) >= ORB_END]
    if not window:
        return None
    return {
        "high": max(bar["high"] for bar in window),
        "low": min(bar["low"] for bar in window),
        "bars": len(window),
    }


def build_pools(bars, previous_day=None, snapshot=None):
    """
    Every pool the sweep can target, with its side.

    `previous_day` carries pd_high/pd_low off the daily read. When it is
    absent - which happens on the 09:35 run, before daily-market-sentiment has
    written the day's row - those two pools are simply not in the list, and the
    caller can see that from the returned names rather than getting silently
    fewer levels.
    """
    pools = []
    orb = opening_range(bars)
    if orb:
        _cross_check_orb(orb, snapshot)
        pools.append(_pool("15min high", orb["high"], HIGH, 1))
        pools.append(_pool("15min low", orb["low"], LOW, 1))

    if previous_day:
        if previous_day.get("pd_high") is not None:
            pools.append(_pool("PDH", float(previous_day["pd_high"]), HIGH, 2))
        if previous_day.get("pd_low") is not None:
            pools.append(_pool("PDL", float(previous_day["pd_low"]), LOW, 2))

    hour = first_hour(bars)
    if hour:
        pools.append(_pool("1Hr high", hour["high"], HIGH, 3))
        pools.append(_pool("1Hr low", hour["low"], LOW, 3))

    extremes = session_extremes(bars)
    if extremes:
        pools.append(_pool("session high", extremes["high"], HIGH, 4))
        pools.append(_pool("session low", extremes["low"], LOW, 4))

    return pools, orb


def _pool(name, price, side, priority):
    return {"name": name, "price": float(price), "side": side, "priority": priority}


def _cross_check_orb(orb, snapshot):
    """
    Compare the locally computed opening range against the snapshot's.

    Both are meant to be the same three 5-minute bars, so a disagreement means
    one of the two series is not what it claims - a partial bar included
    somewhere, or a different session. It logs rather than raises because the
    local computation is the one the playbook specifies and is what this
    function will use either way; the point is that a mismatch is visible
    instead of being averaged into a level.
    """
    if not snapshot:
        return
    for key, local in (("orb_high", orb["high"]), ("orb_low", orb["low"])):
        theirs = snapshot.get(key)
        if theirs is None:
            continue
        if abs(float(theirs) - local) > 0.01:
            logger.warning(
                "%s disagrees: snapshot %s, computed from %d 5-minute bars %s",
                key, theirs, orb["bars"], local,
            )


def stacked_with(pool, pools, price):
    """
    The other pools sitting on the same side within the stacking distance.

    A sweep that takes out two stacked pools at once and reclaims both is the
    highest-grade version of this setup, so this is what upgrades a candidate
    to grade A.

    The distance threshold is the least-evidenced number in the config - the
    playbook names stacked pools repeatedly but never says how near is near.
    It is set from the playbook's own Example 2, where the 15-minute low and
    the previous day low stack 14.50 points apart.
    """
    if not price:
        return []
    window = price * STACKED_POOL_PCT / 100.0
    return [
        other
        for other in pools
        if other is not pool
        and other["side"] == pool["side"]
        and abs(other["price"] - pool["price"]) <= window
    ]
