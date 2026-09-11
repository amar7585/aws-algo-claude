"""
The daily read.

detect_market_regime, calculate_sentiment, sentiment_bias and
calculate_confidence are ported unchanged from
trading-algo/helpers/{regime_helper,sentiment_helper}.py - thresholds included.
Three things deliberately depart from legacy; each is marked where it happens.
"""

import logging
import math

from neon_access import ist_datetime, now_epoch

from config import EXPECTED_MOVE_K, TRADING_DAYS_PER_YEAR
from indicators import decorate_daily

logger = logging.getLogger()


def detect_market_regime(today, recent=None, min_sep=0.002, atr_floor=0.004):
    """
    Ported unchanged, with one addition: it RAISES on a missing indicator.

    Legacy compared against a NaN sma200, and because every comparison with NaN
    is false, bull_stack and bear_stack both went false and the function
    returned TRANSITION - a plausible-looking regime built on an absent
    indicator, with nothing reporting it.
    """
    for field in ("sma20", "sma50", "sma100", "sma200", "rsi", "atr_14"):
        if today.get(field) is None:
            raise ValueError(
                f"cannot classify regime: {field} unavailable for "
                f"{ist_datetime(today['ts']):%Y-%m-%d}. Not enough stored "
                f"candles - refusing to emit a regime from a missing indicator."
            )
    sma20, sma50 = float(today["sma20"]), float(today["sma50"])
    sma100, sma200 = float(today["sma100"]), float(today["sma200"])
    close, rsi = float(today["close"]), float(today["rsi"])
    atr_14 = float(today["atr_14"])

    bull_stack = sma50 > sma100 > sma200
    bear_stack = sma50 < sma100 < sma200
    sep_50_100 = abs(sma50 - sma100) / close
    sep_100_200 = abs(sma100 - sma200) / close
    enough_separation = sep_50_100 > min_sep and sep_100_200 > min_sep
    compressed = (atr_14 / close) < atr_floor

    if recent is not None and len(recent) >= 3:
        anchor = recent[-3]
        sma50_rising = sma50 > float(anchor["sma50"])
        sma100_rising = sma100 > float(anchor["sma100"])
        sma50_falling = sma50 < float(anchor["sma50"])
        sma100_falling = sma100 < float(anchor["sma100"])
        rsi_rising = rsi > float(anchor["rsi"])
        rsi_falling = rsi < float(anchor["rsi"])
    else:
        sma50_rising = sma100_rising = True
        sma50_falling = sma100_falling = True
        rsi_rising = rsi_falling = True

    bullish_trend = (
        bull_stack and enough_separation and not compressed
        and close >= sma50 and close >= sma20
        and sma50_rising and sma100_rising and rsi >= 55 and rsi_rising
    )
    bearish_trend = (
        bear_stack and enough_separation and not compressed
        and close <= sma50 and close <= sma20
        and sma50_falling and sma100_falling and rsi <= 45 and rsi_falling
    )
    if bullish_trend or bearish_trend:
        return "TREND"

    weak_separation = sep_50_100 < min_sep or sep_100_200 < min_sep
    mixed_structure = not bull_stack and not bear_stack
    if 45 < rsi < 55 and (compressed or weak_separation or mixed_structure):
        return "RANGE"
    return "TRANSITION"


def calculate_sentiment(candles, regime):
    today, prev = candles[-1], candles[-2]
    score = 0
    if today["close"] > today["sma20"] > today["sma50"] > today["sma100"]:
        score += 3
    elif today["close"] < today["sma20"] < today["sma50"] < today["sma100"]:
        score -= 3
    if today["close"] > today["sma200"]:
        score += 1
    elif today["close"] < today["sma200"]:
        score -= 1
    if today["rsi"] >= 60:
        score += 1
    elif today["rsi"] <= 40:
        score -= 1
    high_participation = (
        prev["vol_avg_50"] is not None and prev["volume"] > prev["vol_avg_50"]
    )
    if prev["close"] > prev["open"]:
        score += 1
        if high_participation:
            score += 1
    elif prev["close"] < prev["open"]:
        score -= 1
        if high_participation:
            score -= 1
    if regime == "RANGE":
        score = int(score * 0.4)
    elif regime == "TRANSITION":
        score = int(score * 0.7)
    return score


def sentiment_bias(score):
    if score >= 4:
        return "BULLISH"
    if score <= -4:
        return "BEARISH"
    return "NEUTRAL"


def calculate_confidence(score, regime, max_score=7):
    confidence = (abs(score) / max_score) * 100
    if regime == "RANGE":
        confidence *= 0.6
    elif regime == "TRANSITION":
        confidence *= 0.8
    return round(min(confidence, 100), 2)


_STRUCTURE = {"TREND": "trending", "RANGE": "sideways", "TRANSITION": "transitional"}


def combined_label(bias, structure):
    """Legacy keeps bias and structure apart; this is the agreed combined form."""
    bias = bias.lower()
    if structure == "transitional":
        return "transitional"
    if bias == "neutral":
        # A trending day always develops a bias, so neutral + trending is the
        # empty cell in the taxonomy and reads as transitional.
        return "neutral range" if structure == "sideways" else "transitional"
    return f"{bias} {structure}"


def vix_expected_move(price, vix_close, k=None):
    """
    The textbook 1-sigma daily move. Replaces legacy's ATR-based formula, which
    measured 5.7% coverage over 944 sessions - see the EXPECTED_MOVE_K note in
    config.py for the numbers behind the change.
    """
    if price is None or vix_close is None:
        return None
    k = EXPECTED_MOVE_K if k is None else k
    return round(
        float(price) * (float(vix_close) / 100) / math.sqrt(TRADING_DAYS_PER_YEAR) * k,
        2,
    )


def build_daily_sentiment(instrument, candles, vix_close, session_open,
                          min15_high, min15_low):
    """
    Computed on the newest COMPLETED daily candle.

    DEPARTURE 1 - legacy dropped the last row (`df.iloc[:-1]`) because it ran
    intraday and that row was today's forming candle. Dhan's daily endpoint
    lags a session, so at 10:00 the newest stored row is already closed; the
    drop would shift every level a day stale. It is deliberately absent.
    """
    if len(candles) < 2:
        raise ValueError("need at least two daily candles to build sentiment")

    decorate_daily(candles)
    last_closed = candles[-1]
    if last_closed["sma200"] is None:
        raise ValueError(
            f"sma200 unavailable with {len(candles)} stored daily candles (need "
            f"200). Refusing to write a row whose regime would come from a "
            f"missing indicator."
        )

    legacy_regime = detect_market_regime(last_closed, recent=candles[-5:])
    score = calculate_sentiment(candles, legacy_regime)
    bias = sentiment_bias(score)
    structure = _STRUCTURE[legacy_regime]

    # DEPARTURE 2 - legacy read `price` off today's forming daily candle, which
    # does not exist at 10:00. Today's open comes from intraday instead.
    price = round(float(session_open), 2) if session_open is not None else None
    # DEPARTURE 3 - VIX-implied rather than atr_14 x regime factor.
    expected_move = vix_expected_move(price, vix_close)
    upper = round(price + expected_move, 2) if expected_move is not None else None
    lower = round(price - expected_move, 2) if expected_move is not None else None

    return {
        "security_id": instrument["security_id"],
        "instrument_type": instrument["instrument_type"],
        "trade_date": last_closed["ts"],
        "bias": bias.lower(),
        "structure": structure,
        "regime": combined_label(bias, structure),
        "score": int(score),
        "confidence": calculate_confidence(score, legacy_regime),
        "pd_high": last_closed["high"],
        "pd_low": last_closed["low"],
        "pd_close": last_closed["close"],
        "rsi": last_closed["rsi"],
        "sma20": last_closed["sma20"],
        "sma50": last_closed["sma50"],
        "sma100": last_closed["sma100"],
        "sma200": last_closed["sma200"],
        "prev_volume": int(last_closed["volume"]),
        "avg_volume_50": int(last_closed["vol_avg_50"] or 0),
        "vix": vix_close,
        "price": price,
        "expected_move": expected_move,
        "upper_volatility": upper,
        "lower_volatility": lower,
        "min15_high": min15_high,
        "min15_low": min15_low,
        "created_at": now_epoch(),
    }
