"""
Indicator maths - pure-Python, no pandas.

rolling_mean, wilder_rsi, wilder_atr and session_vwap arrive here unchanged
from daily-market-sentiment/indicators.py and strategy-manager/indicators.py,
which carried byte-identical copies of the first three.

WHY THEY MOVED INTO A LAYER. The copies were a deliberate choice, and their
reasoning is recorded in strategy-manager/indicators.py: sharing meant the
neon-access layer, where every change costs a republish and an ARN repoint on
every function. That trade held while the two copies fed two DIFFERENT
classifications. It stops holding the moment daily and intraday must produce
the SAME classification - two copies of the inputs to one agreed rule set is
precisely the drift the single rule set exists to prevent.

Each function returns a list aligned to its input, carrying None where pandas
would have produced NaN. That None is load-bearing: it is what the sma200
guard keys off, and turning it into 0.0 would let a regime be computed from a
missing indicator. The legacy builder did exactly that, and the score silently
capped at +-2 with nothing raising.
"""

# The alignment test in classify.py is written as
#     close > sma9 > sma50 > sma100 > sma200
# so these four periods are structure, not tunables, and are deliberately not
# in thresholds.py. Changing one changes what the test means.
SMA_PERIODS = (9, 50, 100, 200)


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
    """
    Wilder ATR with the standard seed: mean of the first `period` TRs.

    TRUE range - it includes the gap through abs(high - prev_close) and
    abs(low - prev_close), so it is a LARGER number than a mean of high-low
    over the same bars. Anything calibrated against a high-low mean has to be
    re-scaled before it is compared with this; strategy-range-liquidity-sweep
    carries that re-scaling and the reason for it.
    """
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


def session_vwap(candles, precision=2):
    """
    Volume-weighted average of the typical price (h+l+c)/3, over the bars given.

    Computed over exactly the bars passed in - the caller passes one session.
    Legacy's helper took a cumsum over whatever frame it was handed and was
    given a 90-day one, so the value it called "session VWAP" never reset at
    the open.

    Returns None when total volume is zero. INDIA VIX carries volume 0 on
    every bar, so this is reachable rather than theoretical.
    """
    volume = sum(c["volume"] for c in candles)
    if not volume:
        return None
    weighted = sum(
        (c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in candles
    )
    return round(weighted / volume, precision)


def decorate(candles, rsi_period=14, atr_period=14, volume_bars=20):
    """
    Attach the indicator set the classification reads. Mutates in place.

    The caller passes the FULL history - enough bars for sma200 - not just the
    bars structure is read over. On the 5-minute frame that is 200 closed bars,
    roughly three sessions; on the daily frame, 200 sessions.
    """
    closes = [c["close"] for c in candles]
    volumes = [float(c.get("volume") or 0) for c in candles]
    means = {p: rolling_mean(closes, p) for p in SMA_PERIODS}
    vol_avg = rolling_mean(volumes, volume_bars, precision=0)
    rsi = wilder_rsi(closes, period=rsi_period)
    atr = wilder_atr(candles, period=atr_period)
    for i, candle in enumerate(candles):
        for period in SMA_PERIODS:
            candle[f"sma{period}"] = means[period][i]
        candle["vol_avg"] = int(vol_avg[i]) if vol_avg[i] is not None else None
        candle["rsi"] = rsi[i]
        candle["atr"] = atr[i]
    return candles
