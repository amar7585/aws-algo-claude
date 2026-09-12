"""
Configuration for strategy-orchestrator.

Only this function's own tunables. Epoch/IST helpers, the SSL context and the
Neon connection live in the neon-access layer - see neon_access.
"""

import datetime
import json
import os

# --------------------------------------------------------------------------
# The payload contract
#
# THIS NUMBER IS AN INTERFACE. Every strategy this function invokes is written
# against a specific context shape, so a change to that shape that is not
# announced is a silent mismatch: the strategy reads a key that is no longer
# there, gets None, and gates on it.
#
# Bump it for ANY change to the dispatched payload - a renamed key, a removed
# key, a changed unit. Strategies assert the version they were written for and
# raise on anything else, so a bump is loud on both sides.
# --------------------------------------------------------------------------
CONTEXT_VERSION = 1

# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
CHARTS_BASE = os.environ.get("DHAN_CHARTS_BASE", "https://api.dhan.co/v2/charts/")
TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")

# --------------------------------------------------------------------------
# Instruments
#
# (security_id, instrument_type) is instrument identity - security_id ALONE IS
# NOT UNIQUE. `13` is both NIFTY (IDX_I/INDEX) and ABB (NSE_EQ/EQUITY), and a
# lookup on security_id alone once wrote 204 ABB candles labelled NIFTY. Both
# values are always passed together.
# --------------------------------------------------------------------------
NIFTY_SECURITY_ID = os.environ.get("NIFTY_SECURITY_ID", "13")
NIFTY_INSTRUMENT_TYPE = os.environ.get("NIFTY_INSTRUMENT_TYPE", "INDEX")

# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
SESSION_START = datetime.time(9, 15)
SESSION_END = datetime.time(15, 30)

# The opening range: bars stamped 09:15, 09:20 and 09:25, i.e. 09:15-09:30.
# The same definition intraday-market-sentiment uses for orb_high/orb_low, and
# the same one the sweep playbook requires - three 5-minute bars aggregated,
# never Dhan's native 15-minute bucket, which is aligned differently.
ORB_END = datetime.time(9, 30)

# The interval of the bar `snapshot_ts` names. intraday-market-sentiment reads
# 5-minute bars and stamps its snapshot with the last CLOSED one, so the
# instant that bar closed - snapshot_ts + this - is the moment the whole run
# describes. Every candle read here is bounded by it rather than by the wall
# clock, so two runs over the same snapshot read exactly the same bars.
SNAPSHOT_INTERVAL_MINUTES = int(os.environ.get("SNAPSHOT_INTERVAL_MINUTES", "5"))
SNAPSHOT_INTERVAL_SECONDS = SNAPSHOT_INTERVAL_MINUTES * 60

# --------------------------------------------------------------------------
# Candle history
#
# WHERE THE BARS COME FROM. This function reads candle_15min, candle_5min and
# candle_daily - tables intraday-data-loader and daily-market-sentiment write.
# That is a deliberate departure from intraday-market-sentiment, which fetches
# everything itself so a stalled loader cannot feed it stale inputs.
#
# The reason that rule does not transfer: a snapshot row is never revisited,
# so a stale input there would be wrong forever. This function persists
# nothing. A stale SMA costs one routing decision that the next run corrects
# fifteen minutes later, and re-fetching ~1,500 bars from Dhan every run to
# avoid that would leave the loader filling tables its only consumer ignores.
# --------------------------------------------------------------------------
# sma200 on the 15-minute frame needs 200 closed bars. A session is 25
# fifteen-minute bars, so 200 bars is 8 sessions. The extra 60 is headroom so
# a run is never one bar short of classifying.
HISTORY_15MIN_BARS = int(os.environ.get("HISTORY_15MIN_BARS", "260"))

# 200 is the legacy strategy's own window - tail(200) then rolling(200), so
# sma200 is defined on exactly the last row. Fewer bars and the sma200 price
# level is absent rather than wrong.
HISTORY_5MIN_BARS = int(os.environ.get("HISTORY_5MIN_BARS", "200"))

# Wilder ATR14 over daily bars needs 15 closes for its first value. 40 gives
# the average time to settle.
HISTORY_DAILY_BARS = int(os.environ.get("HISTORY_DAILY_BARS", "40"))

# The minimum closed 15-minute bars required to classify. Below this the
# function RAISES rather than classifying on partial inputs.
#
# WHY RAISE. The legacy builder ran every SMA through a safe_value() mapping
# NaN to 0.0, so below 200 bars `close > sma100 > sma200` evaluates as
# `close > 0 > 0` - False - and the longer-SMA term silently contributes
# nothing. The score then caps at +-2, bias needs both surviving tests to
# agree, and that is a different strategy wearing the same name. Nothing
# raises and nothing looks wrong.
#
# daily-market-sentiment already defends against exactly this with `sma200
# NOT NULL`, documented there as a second line of defence because
# detect_market_regime() "silently returns TRANSITION when it compares against
# a missing sma200 rather than raising". This is that defence, earlier.
MIN_BARS_TO_CLASSIFY = int(os.environ.get("MIN_BARS_TO_CLASSIFY", "200"))

# --------------------------------------------------------------------------
# Freshness
#
# This function is handed snapshot_ts - the last CLOSED 5-minute bar at the
# moment intraday-market-sentiment ran - so candle_5min should already carry
# that bar.
#
# ONE BAR OF TOLERANCE, AND THE REASON IS A RACE. intraday-data-loader fires
# every 5 minutes (:00, :05 ...) and intraday-market-sentiment at :35, :50,
# :05, :20 - so the loader's write of the bar the snapshot describes and this
# function's read of it fall in the same minute. Requiring exact equality
# would raise on the ordinary case where Neon is read before the loader's
# write lands.
#
# Older than one bar is not a race, it is a stalled loader, and this function
# raises. The bar actually scanned travels in the payload either way, so a
# one-bar lag is visible to the strategy rather than assumed away.
CANDLE_LAG_TOLERANCE_BARS = int(os.environ.get("CANDLE_LAG_TOLERANCE_BARS", "1"))

# --------------------------------------------------------------------------
# Classification
#
# Ported from trading-algo/helpers/sentiment_builder.py::build_intraday_
# sentiment - NOT from detect_market_regime(), which is the DAILY path and
# returns a third value (TRANSITION) the intraday path never produces. The
# intraday regime is TREND or RANGE from a two-branch test; it is not an enum
# that lost a value. See classify.py.
# --------------------------------------------------------------------------
# The frame the classification describes. Legacy built intraday sentiment for
# MIN_15 and HOUR_1; the strategies only ever read MIN_15.
CLASSIFY_INTERVAL_MINUTES = int(os.environ.get("CLASSIFY_INTERVAL_MINUTES", "15"))

# |sma20 - sma50| / close above this, with price agreeing with bias and bias
# not neutral, is TREND. Legacy value, not measured against this system's data.
TREND_SEPARATION = float(os.environ.get("TREND_SEPARATION", "0.001"))

RSI_PERIOD = int(os.environ.get("RSI_PERIOD", "14"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
VOLUME_AVERAGE_BARS = int(os.environ.get("VOLUME_AVERAGE_BARS", "20"))

# --------------------------------------------------------------------------
# The registry
#
# regime -> the strategy Lambdas valid for it. A playbook fired in the wrong
# regime is the main way this loses money, so the gate is a lookup here rather
# than a judgement inside a strategy.
#
# EVERY STRATEGY GATES AGAIN ON ARRIVAL. This map is the coarse filter - it
# decides which functions are worth invoking at all. The fine gate (VIX, range
# already used, opening-range width, time of day) belongs to the strategy,
# because only the strategy knows what its own playbook requires.
#
# Held as JSON in the environment so adding a strategy is a configuration
# change rather than a redeployment of this function.
# --------------------------------------------------------------------------
_DEFAULT_REGISTRY = {
    # range-liquidity-sweep is a range-day playbook. The identical candle
    # pattern on a trending day is a breakout retest, and fading it is how the
    # setup loses - so TREND is deliberately empty rather than absent.
    "RANGE": ["strategy-range-liquidity-sweep"],
    "TREND": [],
}

STRATEGY_REGISTRY = json.loads(
    os.environ.get("STRATEGY_REGISTRY", json.dumps(_DEFAULT_REGISTRY))
)

# Asynchronous, and the choice is load-bearing. This function returning is not
# a claim that any strategy succeeded - each strategy has its own log group and
# error-notifier reports its failures directly. A synchronous invoke would put
# every strategy's runtime inside this function's timeout and let one slow
# playbook delay the rest.
INVOCATION_TYPE = os.environ.get("INVOCATION_TYPE", "Event")

# Lambda caps an asynchronous invocation payload at 256 KB. The dispatched
# context is dominated by HISTORY_5MIN_BARS bars of OHLCV.
#
# MEASURED, not estimated: the real 2026-09-11 NIFTY session - 75 five-minute
# bars plus the snapshot, the daily row and the classification - serialises to
# 8,212 bytes, 3.1% of the cap. At the configured 200 bars that extrapolates to
# roughly 22 KB, under a tenth of the limit. Checked before every invoke all
# the same, because exceeding it raises a boto3 error that names bytes and not
# the reason.
MAX_PAYLOAD_BYTES = int(os.environ.get("MAX_PAYLOAD_BYTES", str(256 * 1024)))

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))
