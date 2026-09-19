"""
The classification. ONE rule set, run on both frames.

daily-market-sentiment calls it with daily candles; intraday-market-sentiment
calls it with 5-minute candles. That is the whole point of this layer: the two
functions previously carried two different classifications - different score
scales, different bias thresholds, different regime enums - and calling both
of them "the sentiment" made the daily row and the intraday row incomparable.

IT IS NOT A PORT. The legacy detect_market_regime / calculate_sentiment pair
and the legacy intraday builder were both discarded rather than reconciled,
because reconciling them meant picking one frame's rules and imposing them on
the other. The rules below were defined fresh against the agreed taxonomy.

THE TAXONOMY, AND WHY IT IS THREE SEPARATE ANSWERS:

    bias        bullish | bearish | range-bound    - which way, from a score
    structure   trending | sideways | transitional - what price is DOING,
                                                     from the swing read
    regime      trending | sideways |              - what KIND of day it is,
                volatile-expansion                   which gates the playbooks

structure and regime share two of their three values and are still not the
same question. structure is the swing read alone. regime is that read AFTER
volatility has had a veto: on a day whose range is already expanding, the
structure underneath is the thing being disrupted, so the regime says so and
the structure label keeps reporting what it actually saw.

NOTHING HERE FORECASTS. bias and regime name what price has already done.
daily-market-sentiment measured its predecessor's score as not predictive of
forward return; treat this the same way. Its job is to decide which playbooks
are eligible.

EVERY THRESHOLD IS PROVISIONAL - see thresholds.py. The shape is agreed; the
numbers wait on live sessions.
"""

import logging
import math

from . import structure as structure_lib
from . import thresholds
from .indicators import SMA_PERIODS

logger = logging.getLogger(__name__)

BULLISH = "bullish"
BEARISH = "bearish"
RANGE_BOUND = "range-bound"

TRENDING = "trending"
SIDEWAYS = "sideways"
VOLATILE_EXPANSION = "volatile-expansion"

LOW = "low"
NORMAL = "normal"
HIGH = "high"

# Present on the newest bar or the run fails. Absent any one of them the score
# is not the score the rules describe - a missing SMA compared as 0.0 makes
# `close > sma100 > sma200` read as `close > 0 > 0`, which is False, so the
# term contributes nothing and the score silently caps. Legacy did that. This
# raises instead.
REQUIRED = tuple(f"sma{p}" for p in SMA_PERIODS) + ("rsi",)

_CONFIDENCE_FACTORS = {
    TRENDING: "CONFIDENCE_FACTOR_TRENDING",
    SIDEWAYS: "CONFIDENCE_FACTOR_SIDEWAYS",
    VOLATILE_EXPANSION: "CONFIDENCE_FACTOR_VOLATILE",
}


def _alignment_score(bar):
    """
    The SMA term: +-2 for a full stack, +-1 for the fast pair only, else 0.

    Full stack is close > sma9 > sma50 > sma100 > sma200 - price above every
    average and every average in order. The partial credit is the fast pair
    alone, which is where a turn shows up first.
    """
    close = float(bar["close"])
    sma9, sma50 = float(bar["sma9"]), float(bar["sma50"])
    sma100, sma200 = float(bar["sma100"]), float(bar["sma200"])
    if close > sma9 > sma50 > sma100 > sma200:
        return 2
    if close < sma9 < sma50 < sma100 < sma200:
        return -2
    if close > sma9 > sma50:
        return 1
    if close < sma9 < sma50:
        return -1
    return 0


def _volatility(vix, vix_baseline, day_range, expected_move, frame,
                session_elapsed=1.0):
    """
    The volatility read: a level, and whether it is expanding.

    Two independent expansion tests, either of which is enough:

      * VIX has moved VIX_EXPANSION_PCT against its baseline - the previous
        snapshot intraday, the previous close daily. Direction is not filtered:
        a VIX collapsing that fast is also not a day whose structure holds.
      * The day's range has already covered RANGE_EXPANSION_MULTIPLE of the
        expected move FOR THE PART OF THE SESSION THAT HAS HAPPENED.

    THE SECOND TEST IS TIME-SCALED, AND IT HAS TO BE. `expected_move` is a
    whole-session figure, while `day_range` at 10:00 covers 45 minutes. Compared
    raw, the ratio climbs mechanically through the day: the test is far easier
    to trip at 15:15 than at 10:00, so the regime drifts into
    volatile-expansion as the afternoon wears on regardless of what the market
    is doing. No threshold fixes that - it only moves when the drift crosses
    the line.

    Volatility scales with the square root of time, so the expected move for a
    fraction f of the session is expected_move * sqrt(f). That is what the
    range is measured against, which makes 1.5x mean the same thing at 10:00
    as at 15:15.

    Both inputs are optional and a missing one simply cannot fire its test.
    `expanding` being False therefore means "no test fired", which is not the
    same as "measured and calm" - `tests_run` records which ones actually ran
    so a reader can tell those apart.
    """
    level, tests = None, {}
    if vix is not None:
        vix = float(vix)
        low = thresholds.get("VIX_LOW", frame)
        high = thresholds.get("VIX_HIGH", frame)
        level = LOW if vix < low else HIGH if vix > high else NORMAL

    vix_move_pct = None
    if vix is not None and vix_baseline:
        vix_move_pct = round((vix - float(vix_baseline)) / float(vix_baseline) * 100, 2)
        tests["vix_move"] = abs(vix_move_pct) >= thresholds.get(
            "VIX_EXPANSION_PCT", frame
        )

    range_used = None
    scaled_move = None
    if day_range is not None and expected_move:
        # Clamped, not trusted. A fraction of 0 would divide by zero and a
        # fraction above 1 would understate the day; both mean the caller has
        # computed the elapsed session wrongly, and neither should be silently
        # absorbed into a regime.
        fraction = min(max(float(session_elapsed), 1e-6), 1.0)
        scaled_move = float(expected_move) * math.sqrt(fraction)
        range_used = round(float(day_range) / scaled_move, 4)
        tests["range_used"] = range_used >= thresholds.get(
            "RANGE_EXPANSION_MULTIPLE", frame
        )

    return {
        "volatility": level,
        "vix": round(float(vix), 2) if vix is not None else None,
        "vix_move_pct": vix_move_pct,
        "range_used": range_used,
        "session_elapsed": round(float(session_elapsed), 4),
        "expected_move_scaled": round(scaled_move, 2) if scaled_move else None,
        "expanding": any(tests.values()),
        "tests_run": sorted(tests),
    }


def classify(frame, *, bar, structure, vix=None, vix_baseline=None,
             day_range=None, expected_move=None, vwap=None, gap_pct=None,
             session_elapsed=1.0, bars_considered=None, structure_bars=None):
    """
    Classify one already-measured bar. SCORES FROM SCALARS, TOUCHES NO ARRAY.

    bar               the scored bar's own values: close, sma9/50/100/200, rsi
                      (and ts, for the log). On the 5-minute frame these are the
                      columns intraday-market-sentiment measured and stored, so
                      the classifier scores straight from the row; on the daily
                      frame they are candles[-1] after decorate(). The caller
                      owns the history, decorate() and the sma200 depth - this
                      function only reads the newest bar's numbers.
    structure         the swing read, ALREADY COMPUTED by the caller via
                      structure_lib.read() over its own window (today's session
                      intraday, the recent daily bars daily). This used to be
                      computed in here; hoisting it out is what lets the
                      classifier score from stored scalars without re-fetching
                      bars, while daily runs the identical read. One rule set is
                      preserved because both callers use this same classify().
    frame             "5min" or "daily". Names the per-frame threshold overrides
                      and appears in the result; it does not change any rule.
    vwap              session VWAP, intraday only. See the note on the term.
    gap_pct           today's open against the previous close, carried through
                      to the result unscored.
    session_elapsed   fraction of the trading session already elapsed,
                      0 < f <= 1. The range-expansion test scales the expected
                      move by sqrt(f) so the test means the same thing at 09:45
                      as at 15:15. Daily passes 1.0.
    bars_considered   diagnostic counts for the log and result - the history
    structure_bars    depth and the structure window - since this function no
                      longer holds the arrays to measure them itself.

    Raises when the bar is missing any indicator the rules read.
    """
    missing = [field for field in REQUIRED if bar.get(field) is None]
    if missing:
        raise RuntimeError(
            f"{frame}: the scored bar ({bar.get('ts')}) is missing "
            f"{', '.join(missing)} - refusing to classify on a partial "
            f"indicator set. sma200 needs 200 bars of history; check the "
            f"measurement that produced this bar reached back far enough."
        )

    close = float(bar["close"])
    rsi = float(bar["rsi"])
    struct = structure

    # ---- the score --------------------------------------------------------
    # Four terms, +-4, and a fifth worth +-1 on the intraday frame only.
    alignment = _alignment_score(bar)

    rsi_bullish = rsi >= thresholds.get("RSI_BULLISH", frame)
    rsi_bearish = rsi <= thresholds.get("RSI_BEARISH", frame)
    momentum = 1 if rsi_bullish else -1 if rsi_bearish else 0

    # The swing read's DIRECTION, not its label. A transitional or sideways
    # structure has no direction and contributes nothing.
    swing = {
        structure_lib.UP: 1,
        structure_lib.DOWN: -1,
        structure_lib.NONE: 0,
    }[struct["direction"]]

    # VWAP IS ASYMMETRIC BETWEEN THE FRAMES, DELIBERATELY. There is no session
    # VWAP on a daily candle, so this term exists intraday and not daily, and
    # max_score moves with it rather than the daily frame being scored out of
    # a total it can never reach. If that asymmetry is unwanted the term comes
    # out in one place and max_score follows.
    vwap_term = 0
    if vwap is not None:
        vwap_term = 1 if close > float(vwap) else -1 if close < float(vwap) else 0

    score = alignment + momentum + swing + vwap_term
    max_score = 4 + (1 if vwap is not None else 0)

    bias_threshold = thresholds.get("BIAS_THRESHOLD", frame)
    if score >= bias_threshold:
        bias = BULLISH
    elif score <= -bias_threshold:
        bias = BEARISH
    else:
        bias = RANGE_BOUND

    # ---- regime -----------------------------------------------------------
    # Volatility has the first veto, then the swing read. A transitional
    # structure lands in sideways: it is not a trend, and it is the regime
    # with no directional playbook attached, which is the right default for a
    # day that has not chosen.
    vol = _volatility(vix, vix_baseline, day_range, expected_move, frame,
                      session_elapsed)
    if vol["expanding"]:
        regime = VOLATILE_EXPANSION
    elif struct["structure"] == structure_lib.TRENDING:
        regime = TRENDING
    else:
        regime = SIDEWAYS

    confidence = (abs(score) / max_score) * 100
    confidence *= thresholds.get(_CONFIDENCE_FACTORS[regime], frame)
    confidence = round(min(confidence, 100.0), 2)

    result = {
        "frame": frame,
        "candle_ts": bar["ts"],
        "close": close,
        "bias": bias,
        "structure": struct["structure"],
        "regime": regime,
        "volatility": vol["volatility"],
        "score": score,
        "max_score": max_score,
        "confidence": confidence,
        # the terms, so a row explains its own score
        "score_alignment": alignment,
        "score_momentum": momentum,
        "score_swing": swing,
        "score_vwap": vwap_term if vwap is not None else None,
        # the inputs
        "rsi": rsi,
        "vwap": round(float(vwap), 2) if vwap is not None else None,
        "gap_pct": round(float(gap_pct), 4) if gap_pct is not None else None,
        "swing_direction": struct["direction"],
        "swing_high": struct["swing_high"],
        "swing_low": struct["swing_low"],
        "structure_determined": struct["determined"],
        "structure_reason": struct["reason"],
        "bars_considered": bars_considered,
        "structure_bars": structure_bars,
        "vix": vol["vix"],
        "vix_move_pct": vol["vix_move_pct"],
        "range_used": vol["range_used"],
        "session_elapsed": vol["session_elapsed"],
        "expected_move_scaled": vol["expected_move_scaled"],
        "volatility_expanding": vol["expanding"],
        "volatility_tests_run": vol["tests_run"],
    }
    for period in SMA_PERIODS:
        result[f"sma{period}"] = float(bar[f"sma{period}"])

    logger.info(
        "%s: bias %s structure %s regime %s score %+d/%d confidence %.1f "
        "(alignment %+d momentum %+d swing %+d vwap %s, rsi %.1f, "
        "volatility %s expanding=%s via %s, structure over %d bars%s)",
        frame, bias, struct["structure"], regime, score, max_score, confidence,
        alignment, momentum, swing,
        f"{vwap_term:+d}" if vwap is not None else "n/a",
        rsi, vol["volatility"], vol["expanding"],
        ",".join(vol["tests_run"]) or "no test",
        structure_bars if structure_bars is not None else 0,
        "" if struct["determined"] else " - undetermined: " + str(struct["reason"]),
    )
    return result
