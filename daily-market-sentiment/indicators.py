"""
Indicator maths - pure-Python ports of trading-algo/helpers/*, formula for
formula rather than rewritten.

Each returns a list aligned to its input, carrying None where pandas produced
NaN. That None matters: it is what the sma200 guard keys off, and turning it
into a 0.0 would let a regime be computed from a missing indicator.

No pandas. SMA/RSI/ATR over a few hundred rows is unremarkable stdlib, and
keeping it that way is what lets this run as a zip Lambda on a pure-Python
layer.
"""


def rolling_mean(values, period, precision=2):
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = round(window / period, precision)
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = round(window / period, precision)
    return out


def wilder_rsi(closes, period=14, precision=2):
    """Wilder RSI seeded on the simple mean of the first `period` changes."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains[i] = max(delta, 0.0)
        losses[i] = max(-delta, 0.0)
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def value(gain, loss):
        if loss == 0 and gain == 0:
            return 50.0
        if loss == 0:
            return 100.0
        return 100 - (100 / (1 + gain / loss))

    out[period] = round(value(avg_gain, avg_loss), precision)
    for i in range(period + 1, len(closes)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        out[i] = round(value(avg_gain, avg_loss), precision)
    return out


def wilder_atr(candles, period=14, precision=2):
    """Wilder ATR with the standard seed: mean of the first `period` TRs."""
    out = [None] * len(candles)
    if len(candles) < period:
        return out
    true_ranges = []
    for i, candle in enumerate(candles):
        if i == 0:
            true_ranges.append(candle["high"] - candle["low"])
            continue
        prev_close = candles[i - 1]["close"]
        true_ranges.append(
            max(
                candle["high"] - candle["low"],
                abs(candle["high"] - prev_close),
                abs(candle["low"] - prev_close),
            )
        )
    seed = sum(true_ranges[:period]) / period
    out[period - 1] = round(seed, precision)
    prev_atr = seed
    for i in range(period, len(true_ranges)):
        prev_atr = ((prev_atr * (period - 1)) + true_ranges[i]) / period
        out[i] = round(prev_atr, precision)
    return out


def decorate_daily(candles):
    """Attach the indicator set the classification reads. Mutates in place."""
    closes = [c["close"] for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    sma20 = rolling_mean(closes, 20)
    sma50 = rolling_mean(closes, 50)
    sma100 = rolling_mean(closes, 100)
    sma200 = rolling_mean(closes, 200)
    vol50 = rolling_mean(volumes, 50, precision=0)
    rsi = wilder_rsi(closes)
    atr = wilder_atr(candles)
    for i, candle in enumerate(candles):
        candle["sma20"] = sma20[i]
        candle["sma50"] = sma50[i]
        candle["sma100"] = sma100[i]
        candle["sma200"] = sma200[i]
        candle["vol_avg_50"] = int(vol50[i]) if vol50[i] is not None else None
        candle["rsi"] = rsi[i]
        candle["atr_14"] = atr[i]
    return candles
