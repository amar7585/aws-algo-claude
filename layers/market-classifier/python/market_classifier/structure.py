"""
Market structure: is price making higher highs, lower lows, or neither.

The agreed definition is a swing read over ONE window of bars - today's
session from 09:15 on the intraday frame, the recent daily bars on the daily
frame - not a pattern match and not an indicator.

A bar is a swing high when its high is the highest of the SWING_WINDOW bars
either side of it, and a swing low symmetrically. That is a (2k+1) fractal
with k = SWING_WINDOW. Two consequences follow and both are intended:

  * THE LAST k BARS CAN NEVER BE A SWING. A pivot is only a pivot once the
    bars after it exist. So structure always describes price up to k bars ago,
    never the live bar, and it cannot flip on a wick that has not been
    confirmed. That is the correct behaviour for a structure read, and it is
    why the live bar's own high and low are deliberately not special-cased in.

  * EARLY IN A SESSION THERE IS NOTHING TO SAY. The first intraday run of the
    day has nine 5-minute bars, which yields at most a couple of pivots and
    often none. undetermined() is what comes back, and the caller reports
    "sideways" rather than inventing a trend out of four bars.

DIRECTION AND LABEL ARE TWO DIFFERENT ANSWERS. The swing read is directional -
up, down, or neither - and feeds the bias score. The stored `structure` label
is trending / sideways / transitional and carries no direction. Conflating
them is how a sideways market with one higher high reads as bullish.
"""

from . import thresholds

UP = "up"
DOWN = "down"
NONE = "none"

TRENDING = "trending"
SIDEWAYS = "sideways"
TRANSITIONAL = "transitional"


def swing_points(candles, window):
    """
    The confirmed swing highs and lows of `candles`, oldest first.

    Each is {"ts", "price", "index"}. A bar qualifying as both - possible on a
    flat stretch where every high is equal - is recorded as both, and the
    significance test downstream then discards the pair as unchanged.
    """
    highs, lows = [], []
    if window < 1 or len(candles) < (2 * window + 1):
        return highs, lows
    for i in range(window, len(candles) - window):
        span = candles[i - window : i + window + 1]
        bar = candles[i]
        if bar["high"] >= max(c["high"] for c in span):
            highs.append({"ts": bar["ts"], "price": bar["high"], "index": i})
        if bar["low"] <= min(c["low"] for c in span):
            lows.append({"ts": bar["ts"], "price": bar["low"], "index": i})
    return highs, lows


def _moved(later, earlier, reference, significance):
    """
    +1 / -1 / 0 for the move from `earlier` to `later`, as a fraction of price.

    The significance floor is the point: without it a swing high one paisa
    above the last one reads as a higher high, and a flat market classifies as
    trending on rounding noise.
    """
    if later is None or earlier is None or not reference:
        return 0
    delta = (later["price"] - earlier["price"]) / reference
    if delta > significance:
        return 1
    if delta < -significance:
        return -1
    return 0


def undetermined(reason, highs=(), lows=()):
    """
    The "not enough to say" result.

    IT REPORTS THE COUNTS IT ACTUALLY FOUND. An earlier version hardcoded them
    to 0 while the reason string carried the real numbers, so a caller logging
    the counts and a caller logging the reason disagreed with each other about
    the same read - one saying no swings were found, the other saying one of
    each was. The counts are the machine-readable half of the reason and have
    to match it.
    """
    return {
        "structure": SIDEWAYS,
        "direction": NONE,
        "higher_high": None,
        "higher_low": None,
        "lower_high": None,
        "lower_low": None,
        "swing_high": round(float(highs[-1]["price"]), 2) if highs else None,
        "swing_low": round(float(lows[-1]["price"]), 2) if lows else None,
        "swing_high_count": len(highs),
        "swing_low_count": len(lows),
        "determined": False,
        "reason": reason,
    }


def read(candles, frame=None):
    """
    The structure read over `candles`, which are the structure window only -
    today's session, or the recent daily bars - oldest first.

    THE FOUR OUTCOMES:
      higher high AND higher low   -> trending, up
      lower high  AND lower low    -> trending, down
      they disagree (one side making a new extreme while the other does not,
          or an outside sequence)  -> transitional
      neither side moved           -> sideways

    "transitional" is the honest answer for an expanding or contracting range,
    where price is making a new extreme on one side without the other side
    following. Calling it trending would put a directional playbook on a day
    that has not chosen a direction; calling it sideways would hide that one
    side is running.
    """
    window = thresholds.get("SWING_WINDOW", frame)
    significance = thresholds.get("SWING_SIGNIFICANCE", frame)

    if len(candles) < (2 * window + 1):
        return undetermined(
            f"{len(candles)} bars is fewer than the {2 * window + 1} a "
            f"{window}-bar fractal needs"
        )

    highs, lows = swing_points(candles, window)
    if len(highs) < 2 or len(lows) < 2:
        return undetermined(
            f"{len(highs)} swing high(s) and {len(lows)} swing low(s) "
            f"confirmed over {len(candles)} bars - two of each are needed to "
            f"compare",
            highs,
            lows,
        )

    reference = float(candles[-1]["close"])
    high_move = _moved(highs[-1], highs[-2], reference, significance)
    low_move = _moved(lows[-1], lows[-2], reference, significance)

    higher_high, lower_high = high_move > 0, high_move < 0
    higher_low, lower_low = low_move > 0, low_move < 0

    if higher_high and higher_low:
        structure, direction = TRENDING, UP
    elif lower_high and lower_low:
        structure, direction = TRENDING, DOWN
    elif high_move == 0 and low_move == 0:
        structure, direction = SIDEWAYS, NONE
    else:
        structure, direction = TRANSITIONAL, NONE

    return {
        "structure": structure,
        "direction": direction,
        "higher_high": higher_high,
        "higher_low": higher_low,
        "lower_high": lower_high,
        "lower_low": lower_low,
        "swing_high": round(float(highs[-1]["price"]), 2),
        "swing_low": round(float(lows[-1]["price"]), 2),
        "swing_high_count": len(highs),
        "swing_low_count": len(lows),
        "determined": True,
        "reason": None,
    }
