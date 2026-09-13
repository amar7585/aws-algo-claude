"""
Every tunable the classification reads, in one place.

NONE OF THESE NUMBERS ARE MEASURED. They are starting values, chosen to be
defensible rather than correct, and they are meant to be re-set once there are
live sessions in algo.intraday_market_sentiment and algo.daily_market_sentiment
to fit them against. That was the agreed sequence: build the shape, collect
sessions, then tune.

So every one is read from the environment. Re-tuning is a configuration change
on two Lambdas, not a layer republish and an ARN repoint on both.

WHY THE SAME NUMBERS ON BOTH FRAMES, FOR NOW. The classifier runs on daily
candles for daily-market-sentiment and on 5-minute candles for
intraday-market-sentiment, and a given SMA separation plainly does not mean
the same thing on a 5-minute bar as on a daily one. The honest position is
that nothing here has been measured on either frame yet, so splitting them now
would be inventing two sets of unmeasured numbers instead of one. Every value
below takes an optional per-frame override - CLASSIFIER_RSI_BULLISH_DAILY
beats CLASSIFIER_RSI_BULLISH - so the split can be made per value, with
evidence, without touching this file.
"""

import os

# The SMA periods are NOT here, and that is deliberate. The alignment test
# below is written as close > sma9 > sma50 > sma100 > sma200; changing a
# period changes what the test means, so they are structure rather than
# tunables. They live in indicators.SMA_PERIODS.

_DEFAULTS = {
    # ---- RSI: the momentum term, worth +-1 -------------------------------
    "RSI_PERIOD": 14,
    "RSI_BULLISH": 60.0,
    "RSI_BEARISH": 40.0,

    # ---- bias: |score| at or above this leaves range-bound ----------------
    # The score runs +-4 without VWAP and +-5 with it (see classify.py), so 2
    # asks for two agreeing terms out of four or five.
    "BIAS_THRESHOLD": 2,

    # ---- structure: the swing read ----------------------------------------
    # A bar is a swing high when its high is the highest of the SWING_WINDOW
    # bars either side of it - a (2k+1) fractal, k=2. Smaller finds noise;
    # larger finds nothing before midday on the 5-minute frame.
    "SWING_WINDOW": 2,
    # Daily structure looks at this many recent daily bars.
    #
    # 40, NOT THE 10 ORIGINALLY PROPOSED, AND THE REASON IS MEASURED. A k=2
    # fractal needs two confirmed swing highs AND two confirmed swing lows
    # before it can compare anything. Run over the 204 real NIFTY daily bars
    # in algo.candle_daily on 2026-09-13, the last 10 bars yield 0 swing highs
    # and 1 swing low - undetermined. So does 15, 20, 25 and 30. Structure is
    # first determined at 40 (2 highs, 4 lows -> trending down, which is what
    # that stretch of the index actually did). A lookback that can never
    # determine structure is not a conservative default; it is a term silently
    # contributing zero to every score, which is the exact failure this layer
    # raises about elsewhere.
    "STRUCTURE_LOOKBACK_DAILY": 40,
    # A swing must clear the one before it by this fraction of price to count
    # as higher or lower. Without it, a 0.01% difference reads as a trend.
    "SWING_SIGNIFICANCE": 0.0005,

    # ---- volatility --------------------------------------------------------
    # INDIA VIX bands. Absolute levels, not percentiles - a percentile needs a
    # history this system has not collected yet.
    "VIX_LOW": 12.0,
    "VIX_HIGH": 18.0,
    # Expansion, test one: VIX moving this much against its baseline. The
    # baseline is the previous snapshot intraday, the previous close daily.
    "VIX_EXPANSION_PCT": 5.0,
    # Expansion, test two: the day's range already exceeds this multiple of
    # the VIX-implied expected move for the session.
    #
    # 1.5, AND 1.0 WAS MEASURED WRONG. Over the 203 sessions in
    # algo.candle_daily that have both a previous NIFTY close and a previous
    # INDIA VIX close (measured 2026-09-13), full-session range divided by the
    # VIX-implied expected move distributes as:
    #
    #     min 0.39 | p25 0.73 | median 0.95 | p75 1.22 | p90 1.59 | max 4.00
    #
    # so a 1.0 multiple calls 44.8% of ordinary sessions an expansion. 1.5
    # keeps 11.8% - roughly the p90 - which is what "this day is not behaving
    # normally" should mean.
    #
    # THE COMPARISON IS TIME-SCALED, so this multiple means the same thing at
    # every hour. `expected_move` is a whole-session figure while the day's
    # range at 10:00 covers 45 minutes; compared raw the ratio climbs
    # mechanically through the day and the regime drifts into
    # volatile-expansion every afternoon. classify._volatility divides by
    # expected_move * sqrt(fraction of session elapsed) instead. Measured on
    # the real 2026-09-11 session, that flattens the ratio from a monotonic
    # 0.27 -> 1.24 climb to a 0.77 - 1.34 band with no trend in the hour.
    #
    # The scaling is very sensitive when almost no session has elapsed - the
    # 09:15 bar alone reads 2.30x. The first run of the day is 10:00, by which
    # point f is 0.13 and the reading is 0.77, so the sensitive region is
    # never sampled in production.
    "RANGE_EXPANSION_MULTIPLE": 1.5,

    # ---- confidence --------------------------------------------------------
    # |score|/max_score scaled to 100, then multiplied by the regime's factor.
    # A read taken on an expanding day is worth less because the structure it
    # rests on is the thing being disrupted.
    "CONFIDENCE_FACTOR_TRENDING": 1.0,
    "CONFIDENCE_FACTOR_SIDEWAYS": 0.7,
    "CONFIDENCE_FACTOR_VOLATILE": 0.5,
}


def _coerce(default, raw):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return type(default)(raw)


def get(name, frame=None):
    """
    The value of `name`, with an optional per-frame override.

    Lookup order, first hit wins:
        CLASSIFIER_<NAME>_<FRAME>   e.g. CLASSIFIER_RSI_BULLISH_DAILY
        CLASSIFIER_<NAME>           e.g. CLASSIFIER_RSI_BULLISH
        the default above

    An unknown name raises rather than returning None - a typo in a threshold
    name would otherwise disable a test silently, which is the same class of
    failure as an SMA defaulting to zero.
    """
    if name not in _DEFAULTS:
        raise KeyError(
            f"{name!r} is not a classifier threshold "
            f"({', '.join(sorted(_DEFAULTS))})"
        )
    default = _DEFAULTS[name]
    if frame:
        scoped = os.environ.get(f"CLASSIFIER_{name}_{frame.upper()}")
        if scoped is not None:
            return _coerce(default, scoped)
    raw = os.environ.get(f"CLASSIFIER_{name}")
    if raw is not None:
        return _coerce(default, raw)
    return default


def snapshot(frame=None):
    """Every threshold as it resolves for `frame`. Logged once per run."""
    return {name: get(name, frame) for name in sorted(_DEFAULTS)}
