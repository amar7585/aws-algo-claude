"""
Assembling the classifier's inputs for the 5-minute frame.

The RULES are not here. They live in the market-classifier layer and are
shared, unchanged, with daily-market-sentiment - one rule set over both frames
is the whole reason that layer exists. This module only decides what to hand
it, which is a per-frame question and therefore a per-function one.

Three inputs are not obvious and each is wrong in a different way if guessed:

  * WHICH BARS CARRY THE INDICATORS, AND WHICH CARRY THE STRUCTURE. sma200 on
    the 5-minute frame needs 200 closed bars and a session supplies 75, so the
    indicator history spans several sessions. Market structure is explicitly
    "the current day from 09:15", so the swing read gets today's bars only.
    Handing the swing read the multi-session history would find swings from
    Tuesday and call them today's structure.

  * HOW MUCH OF THE SESSION HAS ELAPSED. The range-expansion test compares the
    day's range so far against the expected move, and the expected move is a
    whole-session figure. Elapsed comes from the RUN CLOCK, not from
    snapshot_ts: at a 10:00 run the bar stamped 10:00 has zero seconds of data
    in it, so 45 minutes have passed, not 50.

  * THE VIX BASELINE. The previous snapshot's VIX, so the expansion test
    measures the last fifteen minutes rather than the whole day. On the first
    run of a day there is no earlier row and the baseline is the previous
    session's last snapshot, which is the same convention every other
    *_change_pct on the row already follows.
"""

import logging
import math

from market_classifier import classify, decorate, session_vwap
from neon_access import ist_datetime

from config import (
    CANDLE_INTERVAL_MINUTES,
    EXPECTED_MOVE_K,
    MIN_BARS_TO_CLASSIFY,
    SESSION_MINUTES,
    SESSION_START,
    TRADING_DAYS_PER_YEAR,
)

logger = logging.getLogger()

FRAME = "5min"


def expected_move(price, vix):
    """
    The VIX-implied one-sigma move for a whole session.

    The same formula, and the same K, daily-market-sentiment uses. That is the
    point: the two frames' volatility reads have to be built from the same
    number or "volatile-expansion" means two different things depending on
    which row you read it from.
    """
    if price is None or vix is None:
        return None
    return float(price) * (float(vix) / 100) / math.sqrt(TRADING_DAYS_PER_YEAR) * (
        EXPECTED_MOVE_K
    )


def session_elapsed(now):
    """
    The fraction of the trading session that has actually happened, 0 < f <= 1.

    From the wall clock of the run, not from snapshot_ts. At a 10:00 run the
    newest bar is stamped 10:00 and carries no elapsed time at all, so taking
    the bar stamp plus an interval would credit the session with five minutes
    that have not happened - and the expansion test divides by the square root
    of this, so an overstated f makes a quiet market look calmer still.

    Clamped at both ends. The 15:35 closing sweep is past the close and would
    otherwise read above 1.0.
    """
    start = now.replace(
        hour=SESSION_START.hour, minute=SESSION_START.minute, second=0, microsecond=0
    )
    elapsed = (now - start).total_seconds() / 60
    return min(max(elapsed / SESSION_MINUTES, 1e-6), 1.0)


def classify_snapshot(history, session_bars, stats, now, vix, vix_baseline):
    """
    Classify the snapshot. Raises rather than classifying on partial inputs.

    history       in-session 5-minute bars across several sessions, oldest
                  first, ending at the bar snapshot_ts names.
    session_bars  today's bars only, for the swing read.
    stats         session_stats() for today, bounded by snapshot_ts.
    """
    if len(history) < MIN_BARS_TO_CLASSIFY:
        raise RuntimeError(
            f"{len(history)} in-session {CANDLE_INTERVAL_MINUTES}-minute bars is "
            f"below the {MIN_BARS_TO_CLASSIFY} the classification needs - sma200 "
            f"would be absent and its term would silently contribute nothing to "
            f"the score. Widen HISTORY_DAYS, or check Dhan returned the whole "
            f"window: the fetch covered "
            f"{ist_datetime(history[0]['ts']):%Y-%m-%d} to "
            f"{ist_datetime(history[-1]['ts']):%Y-%m-%d}"
        )

    decorate(history)
    spot = stats["close"]
    day_range = (
        stats["high"] - stats["low"]
        if stats["high"] is not None and stats["low"] is not None
        else None
    )
    elapsed = session_elapsed(now)

    result = classify(
        history,
        frame=FRAME,
        structure_candles=session_bars,
        vix=vix,
        vix_baseline=vix_baseline,
        day_range=day_range,
        expected_move=expected_move(spot, vix),
        vwap=session_vwap(session_bars),
        session_elapsed=elapsed,
    )
    logger.info(
        "classified on %d bars (%d of them today), session %.1f%% elapsed",
        len(history), len(session_bars), elapsed * 100,
    )
    return result


# The classifier returns more than the row stores - the per-term breakdown and
# the structure diagnostics stay in the log. These are the columns.
ROW_FIELDS = (
    "bias",
    "structure",
    "regime",
    "volatility",
    "score",
    "max_score",
    "confidence",
    "sma9",
    "sma50",
    "sma100",
    "sma200",
    "rsi",
    "swing_direction",
    "swing_high",
    "swing_low",
    "structure_determined",
    "range_used",
    "session_elapsed",
    "volatility_expanding",
)


def row_columns(result):
    """The classification as the columns intraday_market_sentiment stores."""
    return {name: result[name] for name in ROW_FIELDS}
