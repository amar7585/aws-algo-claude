"""
The one measurement the classifier cannot make for itself: the SMAs and RSI.

This module used to CLASSIFY here (classify_snapshot + row_columns). That moved
to the market-classifier function - this one only MEASURES now. What stays is
the indicator set that needs the ~200-bar cross-session history only
intraday-market-sentiment fetches: decorate() computes it, and the newest bar
(the one snapshot_ts names) carries the scalars the classifier scores from.

IT RAISES ON TOO LITTLE HISTORY rather than storing a row whose sma200 came
from a missing indicator - intraday_fno_data.sma200 is NOT NULL and the
classifier's alignment term reads all four SMAs off the row. This is the same
guard the classifier used to raise, kept on the measurement side because these
columns are stored here.

The expected-move and session-elapsed maths that used to sit alongside the
scoring moved WITH it, into market-classifier: they are inputs to the
volatility rule, not measurements.
"""

import logging

from market_classifier import decorate
from neon_access import ist_datetime

from config import CANDLE_INTERVAL_MINUTES, MIN_BARS_TO_CLASSIFY

logger = logging.getLogger()

# The indicator columns intraday_fno_data stores and the classifier reads back.
INDICATOR_COLUMNS = ("sma9", "sma50", "sma100", "sma200", "rsi")


def indicator_columns(history):
    """
    decorate the history and return the newest bar's indicator scalars.

    history  in-session bars across several sessions, oldest first, ending on
             the bar snapshot_ts names. decorate needs the whole span because
             sma200 needs 200 closed bars and one session supplies ~75.

    Raises when the history is too short, or an indicator is absent on the
    newest bar - refusing to write a partial indicator set.
    """
    if len(history) < MIN_BARS_TO_CLASSIFY:
        raise RuntimeError(
            f"{len(history)} in-session {CANDLE_INTERVAL_MINUTES}-minute bars is "
            f"below the {MIN_BARS_TO_CLASSIFY} sma200 needs - the fetch covered "
            f"{ist_datetime(history[0]['ts']):%Y-%m-%d} to "
            f"{ist_datetime(history[-1]['ts']):%Y-%m-%d}. Widen HISTORY_DAYS, or "
            f"check Dhan returned the whole window."
        )
    decorate(history)
    bar = history[-1]
    missing = [c for c in INDICATOR_COLUMNS if bar.get(c) is None]
    if missing:
        raise RuntimeError(
            f"the newest bar ({ist_datetime(bar['ts']):%H:%M}) is missing "
            f"{missing} over {len(history)} bars - refusing to store a partial "
            f"indicator set. sma200 needs 200 bars."
        )
    logger.info(
        "measured indicators on %d bars: sma9 %.2f sma50 %.2f sma200 %.2f rsi %.1f",
        len(history), bar["sma9"], bar["sma50"], bar["sma200"], bar["rsi"],
    )
    return {c: bar[c] for c in INDICATOR_COLUMNS}
