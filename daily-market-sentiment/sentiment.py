"""
The daily read.

THE RULES ARE NOT HERE ANY MORE. They live in the market-classifier layer and
are the same rules intraday-market-sentiment runs on 5-minute candles. This
module only assembles the inputs for the daily frame and shapes the row.

WHAT WAS REMOVED, AND WHY. This file used to carry a port of
trading-algo's detect_market_regime / calculate_sentiment / sentiment_bias /
calculate_confidence, while the intraday path carried a port of a different
legacy builder. The two disagreed about almost everything that matters:

    regime      TREND / RANGE / TRANSITION   vs   TREND / RANGE
    score       +-7, damped by regime        vs   +-3, undamped
    bias        threshold +-4                vs   threshold +-2
    confidence  |score|/7, damped            vs   |score|*25 + bonuses

so `regime` on a daily row and `regime` on an intraday row were different
measurements wearing one name, and a score of -3 meant "mildly bearish" in one
and "as bearish as it gets" in the other. Both ports were discarded rather than
reconciled - reconciling meant imposing one frame's rules on the other - and
the replacement was defined fresh against the agreed taxonomy.

WHAT THE DAILY FRAME STILL OWNS. Three things the classifier cannot know:

  * the structure window. "The current day from 09:15" is the intraday rule;
    the daily frame reads its structure off recent DAILY bars, which is what
    identifies a gap, a broken trend, and the broader bias.
  * the expected move, which is VIX-implied here and drives the volatility
    columns the row stores.
  * that the row describes YESTERDAY. Dhan's daily endpoint lags a session, so
    the newest stored daily candle at 09:35 is the previous session's.

DEPARTURE FROM LEGACY, KEPT. Legacy dropped the last row (`df.iloc[:-1]`)
because it ran intraday against a forming candle. Dhan's daily endpoint lags,
so at 09:35 the newest stored row is already closed and the drop would shift
every level a day stale. It is deliberately absent.
"""

import logging
import math

from market_classifier import classify, decorate, read_structure
from market_classifier import thresholds as classifier_thresholds
from neon_access import now_epoch

from config import EXPECTED_MOVE_K, TRADING_DAYS_PER_YEAR

logger = logging.getLogger()

FRAME = "daily"


def vix_expected_move(price, vix_close, k=None):
    """
    The textbook one-sigma daily move.

    Replaces legacy's ATR-based formula, which measured 5.7% coverage over 944
    sessions - see the EXPECTED_MOVE_K note in config.py. intraday-market-
    sentiment computes the same figure with the same K, so "volatile-expansion"
    means one thing across both frames rather than two.
    """
    if price is None or vix_close is None:
        return None
    k = EXPECTED_MOVE_K if k is None else k
    return round(
        float(price) * (float(vix_close) / 100) / math.sqrt(TRADING_DAYS_PER_YEAR) * k,
        2,
    )


def gap_pct(session_open, previous_close):
    """
    Today's open against yesterday's close, in percent.

    This is the gap up / gap down. It is carried on the row and handed to the
    classifier unscored - it describes the open, not the session, and scoring
    it would let a gap that filled in the first ten minutes keep voting on the
    daily bias all day.
    """
    if session_open is None or not previous_close:
        return None
    return round(
        (float(session_open) - float(previous_close)) / float(previous_close) * 100, 4
    )


def build_daily_sentiment(instrument, candles, vix_candles, session_open,
                          min15_high, min15_low):
    """
    Computed on the newest COMPLETED daily candle.

    vix_candles is the INDIA VIX daily series, not a single close: the
    expansion test needs the previous session's close as its baseline, the
    same way the intraday frame uses the previous snapshot's VIX.
    """
    if len(candles) < 2:
        raise ValueError("need at least two daily candles to build sentiment")

    # volume_bars=50 because the row's column is avg_volume_50 and has been
    # since the table was created. The layer defaults to 20, which is the
    # right window for the 5-minute frame and the wrong one for this column.
    decorate(candles, volume_bars=50)
    last = candles[-1]
    if last["sma200"] is None:
        raise ValueError(
            f"sma200 unavailable with {len(candles)} stored daily candles (need "
            f"200). Refusing to write a row whose regime would come from a "
            f"missing indicator."
        )

    vix_close = vix_candles[-1]["close"] if vix_candles else None
    vix_baseline = vix_candles[-2]["close"] if len(vix_candles or ()) >= 2 else None

    # Legacy read `price` off today's forming daily candle, which does not
    # exist at 09:35. Today's open comes from the intraday call instead.
    price = round(float(session_open), 2) if session_open is not None else None
    expected_move = vix_expected_move(price, vix_close)
    upper = round(price + expected_move, 2) if expected_move is not None else None
    lower = round(price - expected_move, 2) if expected_move is not None else None

    lookback = classifier_thresholds.get("STRUCTURE_LOOKBACK_DAILY", FRAME)
    # The swing read is hoisted out of classify() now - the caller owns its
    # own structure window. daily reads it off the recent daily bars; the
    # intraday classifier reads it off today's session. Same read, both frames.
    structure = read_structure(candles[-lookback:], FRAME)
    result = classify(
        frame=FRAME,
        bar=last,
        structure=structure,
        vix=vix_close,
        vix_baseline=vix_baseline,
        # Yesterday's own range against yesterday's expected move. The session
        # is complete, so session_elapsed is 1.0 and the sqrt scaling the
        # intraday frame needs is a no-op here.
        day_range=float(last["high"]) - float(last["low"]),
        expected_move=vix_expected_move(last["close"], vix_close),
        vwap=None,
        gap_pct=gap_pct(session_open, last["close"]),
        session_elapsed=1.0,
        bars_considered=len(candles),
        structure_bars=len(candles[-lookback:]),
    )

    return {
        "security_id": instrument["security_id"],
        "instrument_type": instrument["instrument_type"],
        "trade_date": last["ts"],

        # the classification - same rules, same scale as the intraday row
        "bias": result["bias"],
        "structure": result["structure"],
        "regime": result["regime"],
        "volatility": result["volatility"],
        "score": int(result["score"]),
        "max_score": int(result["max_score"]),
        "confidence": result["confidence"],
        "swing_direction": result["swing_direction"],
        "swing_high": result["swing_high"],
        "swing_low": result["swing_low"],
        "structure_determined": result["structure_determined"],
        "range_used": result["range_used"],
        "volatility_expanding": result["volatility_expanding"],
        "gap_pct": result["gap_pct"],

        # previous-session price context
        "pd_high": last["high"],
        "pd_low": last["low"],
        "pd_close": last["close"],

        # the indicators the score was computed from
        "rsi": last["rsi"],
        "sma9": last["sma9"],
        "sma50": last["sma50"],
        "sma100": last["sma100"],
        "sma200": last["sma200"],
        "prev_volume": int(last["volume"]),
        "avg_volume_50": int(last["vol_avg"] or 0),
        "vix": vix_close,

        # today-grain
        "price": price,
        "expected_move": expected_move,
        "upper_volatility": upper,
        "lower_volatility": lower,
        "min15_high": min15_high,
        "min15_low": min15_low,

        "created_at": now_epoch(),
    }
