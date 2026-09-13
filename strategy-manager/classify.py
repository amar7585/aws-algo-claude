"""
The intraday classification: bias, regime, score, confidence.

A port of trading-algo/helpers/sentiment_builder.py::build_intraday_sentiment,
rule for rule.

IT IS NOT detect_market_regime(). That helper is the DAILY path - it is what
daily-market-sentiment/sentiment.py ports, it reads atr_14 and SMA slopes over
recent daily bars, and it returns a third value, TRANSITION. The intraday path
never calls it and never produces TRANSITION: the intraday regime comes out of
a two-branch test below and is TREND or RANGE by construction. There is no
missing enum value here and nothing to fold.

THIS CLASSIFIES, IT DOES NOT FORECAST. `bias` and `regime` name what price and
volume have already done. daily-market-sentiment measured its own score as not
predictive of forward return; treat this the same way. Its job is to decide
which playbooks are eligible, not to predict anything.

THE ONE DELIBERATE DEPARTURE. Legacy pushes every indicator through a
safe_value() that maps NaN to 0.0. With fewer than 200 bars that makes
`close > sma100 > sma200` read as `close > 0 > 0` - False - so the
longer-SMA test contributes nothing, the score silently caps at +-2, and bias
then requires both surviving tests to agree. Nothing raises. This module
raises instead, which is the same defence daily-market-sentiment already
carries as `sma200 NOT NULL`.
"""

import logging

from config import MIN_BARS_TO_CLASSIFY, TREND_SEPARATION

logger = logging.getLogger()

BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"

TREND = "TREND"
RANGE = "RANGE"

# The inputs every test below reads. Absent any one of them the score is not
# the score the rules describe, so the run fails rather than degrades.
REQUIRED = ("sma20", "sma50", "sma100", "sma200", "rsi")


def classify(candles, interval_minutes):
    """
    Classify the newest bar of `candles`, which must all be CLOSED bars.

    Returns the block the payload carries to every strategy. Raises when the
    history is too short to support it.
    """
    if len(candles) < MIN_BARS_TO_CLASSIFY:
        raise RuntimeError(
            f"{len(candles)} closed {interval_minutes}-minute bars is below the "
            f"{MIN_BARS_TO_CLASSIFY} needed to classify - sma200 would be absent "
            f"and the longer-SMA test would silently contribute nothing to the "
            f"score. Check intraday-data-loader has been filling "
            f"algo.candle_{interval_minutes}min"
        )

    last = candles[-1]
    missing = [field for field in REQUIRED if last.get(field) is None]
    if missing:
        raise RuntimeError(
            f"the newest {interval_minutes}-minute bar "
            f"({last['ts']}) is missing {', '.join(missing)} over "
            f"{len(candles)} bars - refusing to classify on a partial "
            f"indicator set"
        )

    close = float(last["close"])
    sma20 = float(last["sma20"])
    sma50 = float(last["sma50"])
    sma100 = float(last["sma100"])
    sma200 = float(last["sma200"])
    rsi = float(last["rsi"])

    # ---- score: three tests, each worth one point either way --------------
    sma_stack_bull = close > sma20 > sma50
    sma_stack_bear = close < sma20 < sma50
    above_longer_smas = close > sma100 > sma200
    below_longer_smas = close < sma100 < sma200
    rsi_bullish = rsi >= 55
    rsi_bearish = rsi <= 45

    score = 0
    if sma_stack_bull:
        score += 1
    elif sma_stack_bear:
        score -= 1
    if above_longer_smas:
        score += 1
    elif below_longer_smas:
        score -= 1
    if rsi_bullish:
        score += 1
    elif rsi_bearish:
        score -= 1

    if score >= 2:
        bias = BULLISH
    elif score <= -2:
        bias = BEARISH
    else:
        bias = NEUTRAL

    # ---- volume strength --------------------------------------------------
    # vol_avg can legitimately be None (fewer bars than the window) or 0 - an
    # index with no reported volume. Legacy divides and guards with a
    # truthiness test; the 0.0 result feeds the confidence term below, where
    # it simply contributes nothing.
    vol_avg = last.get("vol_avg")
    volume = float(last.get("volume") or 0)
    volume_strength = (volume / float(vol_avg)) if vol_avg else 0.0

    # ---- regime -----------------------------------------------------------
    # Two branches. TREND requires all three of: the fast pair separated by
    # more than TREND_SEPARATION, price on the same side of sma20 as the bias,
    # and a bias at all. Everything else is RANGE - including a market that is
    # directional but compressed, which is the case this playbook set cares
    # most about getting right.
    sma_separation = abs(sma20 - sma50) / close if close else 0.0
    directional_price = (bias == BULLISH and close >= sma20) or (
        bias == BEARISH and close <= sma20
    )
    if sma_separation > TREND_SEPARATION and directional_price and bias != NEUTRAL:
        regime = TREND
    else:
        regime = RANGE

    # ---- confidence -------------------------------------------------------
    # Legacy's weights exactly: the score carries most of it, TREND and an
    # agreeing RSI add fixed bonuses, and volume adds up to 40 more. Capped at
    # 100. Not calibrated against this system's data - the numbers come from
    # the legacy file and are documented here so they can be re-measured
    # rather than rediscovered.
    confidence = abs(score) * 25
    if regime == TREND:
        confidence += 20
    if (bias == BULLISH and rsi_bullish) or (bias == BEARISH and rsi_bearish):
        confidence += 15
    confidence += min(volume_strength * 20, 40)
    confidence = min(100.0, confidence)

    logger.info(
        "classified %d-min: bias %s regime %s score %+d confidence %.1f "
        "(rsi %.1f, sma20/50 separation %.4f%%, volume %.2fx average)",
        interval_minutes, bias, regime, score, confidence,
        rsi, sma_separation * 100, volume_strength,
    )

    return {
        "interval_minutes": interval_minutes,
        "candle_ts": last["ts"],
        "close": close,
        "bias": bias,
        "regime": regime,
        "score": score,
        "confidence": round(confidence, 2),
        "rsi": rsi,
        "sma20": sma20,
        "sma50": sma50,
        "sma100": sma100,
        "sma200": sma200,
        "sma_separation_pct": round(sma_separation * 100, 4),
        "volume": int(volume),
        "vol_avg": int(vol_avg) if vol_avg else None,
        "volume_strength": round(volume_strength, 4),
        "bars_considered": len(candles),
    }
