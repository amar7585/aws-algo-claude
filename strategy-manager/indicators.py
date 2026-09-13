"""
Indicator maths - pure-Python ports of trading-algo/helpers/*, formula for
formula rather than rewritten.

Each returns a list aligned to its input, carrying None where pandas produced
NaN. That None is load-bearing: it is what the sma200 guard in classify.py
keys off, and turning it into 0.0 would let a regime be computed from a
missing indicator. The legacy builder did exactly that - see classify.py.

rolling_mean, wilder_rsi and wilder_atr are byte-for-byte the versions in
daily-market-sentiment/indicators.py, and deliberately a copy rather than a
shared module: sharing would mean putting them in the neon-access layer, where
every change means republishing the layer AND repointing every function's
pinned ARN. The same trade params.py already makes.

No pandas. SMA/RSI/ATR over a few hundred rows is unremarkable stdlib, and
keeping it that way is what lets this run as a zip Lambda.
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
    """
    Wilder ATR with the standard seed: mean of the first `period` TRs.

    TRUE range - it includes the gap through abs(high - prev_close) and
    abs(low - prev_close). That matters downstream: the sweep playbook's
    "range already used" gate was calibrated against a 10-day mean of
    high-low, which excludes gaps and is therefore a SMALLER number than this
    for the same market. The strategy carries the re-scaled threshold and the
    reason; see strategy-range-liquidity-sweep/config.py.
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

    A DEPARTURE FROM THE LEGACY HELPER, ON PURPOSE. trading-algo's
    helpers/intraday_indicators.py::session_vwap takes a plain cumsum over the
    whole frame it is handed, and build_intraday_sentiment hands it a 90-day
    frame - so the value it calls "session VWAP" is a 90-day cumulative VWAP
    that never resets at the open. The same repo's liquidity_swap_strategy
    computes it correctly, resetting per trade_date, which is the read the
    name implies and the one the sweep playbook's T1 target needs.

    This function computes it over exactly the bars passed in; the caller
    passes one session. It therefore agrees with
    intraday_market_sentiment.vwap, which is also bounded to the session.

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


def average_range(candles, precision=2):
    """Mean high-low of the bars given. The sweep stop buffer uses this."""
    if not candles:
        return None
    return round(sum(c["high"] - c["low"] for c in candles) / len(candles), precision)


def decorate(candles, rsi_period=14, atr_period=14, volume_bars=20):
    """
    Attach the indicator set the classification reads. Mutates in place.

    SMA periods are fixed at 20/50/100/200 because the classification's own
    tests are written against those four - `close > sma20 > sma50` and
    `close > sma100 > sma200`. They are structure, not tunables.
    """
    closes = [c["close"] for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    sma20 = rolling_mean(closes, 20)
    sma50 = rolling_mean(closes, 50)
    sma100 = rolling_mean(closes, 100)
    sma200 = rolling_mean(closes, 200)
    vol_avg = rolling_mean(volumes, volume_bars, precision=0)
    rsi = wilder_rsi(closes, period=rsi_period)
    atr = wilder_atr(candles, period=atr_period)
    for i, candle in enumerate(candles):
        candle["sma20"] = sma20[i]
        candle["sma50"] = sma50[i]
        candle["sma100"] = sma100[i]
        candle["sma200"] = sma200[i]
        candle["vol_avg"] = int(vol_avg[i]) if vol_avg[i] is not None else None
        candle["rsi"] = rsi[i]
        candle["atr"] = atr[i]
    return candles
