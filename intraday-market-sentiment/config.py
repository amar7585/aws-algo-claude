"""
Configuration for intraday-market-sentiment.

Only this function's own tunables. Epoch/IST helpers, the SSL context and the
Neon connection live in the neon-access layer - see neon_access.
"""

import datetime
import os

# --------------------------------------------------------------------------
# Endpoints
#
# THE CHAIN IS NOT UNDER THE EXPIRY LIST'S BASE PATH. expirylist lives at
# /v2/optionchain/expirylist, but the chain itself is the FLAT /v2/optionchain
# - /v2/optionchain/optionchain answers "404 page not found". Measured
# 2026-09-12. They are held as two whole URLs rather than a shared base plus
# two suffixes, because a shared base is exactly the assumption that produced
# the 404.
# --------------------------------------------------------------------------
CHARTS_BASE = os.environ.get("DHAN_CHARTS_BASE", "https://api.dhan.co/v2/charts/")
OPTIONCHAIN_URL = os.environ.get(
    "DHAN_OPTIONCHAIN_URL", "https://api.dhan.co/v2/optionchain"
)
EXPIRYLIST_URL = os.environ.get(
    "DHAN_EXPIRYLIST_URL", "https://api.dhan.co/v2/optionchain/expirylist"
)

TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")

# The batch quote endpoint used for market breadth. It returns last_price and
# ohlc.close (the PREVIOUS day's close) for up to 1000 instruments per request
# at 1 req/sec - see breadth.py. Held whole, like the two option-chain URLs.
MARKETFEED_OHLC_URL = os.environ.get(
    "DHAN_MARKETFEED_OHLC_URL", "https://api.dhan.co/v2/marketfeed/ohlc"
)

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
VIX_SECURITY_ID = os.environ.get("VIX_SECURITY_ID", "21")
VIX_INSTRUMENT_TYPE = os.environ.get("VIX_INSTRUMENT_TYPE", "INDEX")

# The current-month NIFTY future. NOTHING about the contract is hardcoded: the
# month comes from Dhan's expiry list at run time, so the resolver rolls
# itself. Same rule as intraday-data-loader.
FUTURES_UNDERLYING_SCRIP = int(os.environ.get("FUTURES_UNDERLYING_SCRIP", "13"))
FUTURES_UNDERLYING_SEG = os.environ.get("FUTURES_UNDERLYING_SEG", "IDX_I")
FUTURES_INSTRUMENT_TYPE = os.environ.get("FUTURES_INSTRUMENT_TYPE", "FUTIDX")

# NIFTY-SEP2026-FUT. The `NIFTY-` prefix matters: NIFTYFPI-SEP2026-FUT and
# NIFTYNXT50-SEP2026-FUT are different contracts that a looser pattern eats.
FUTURES_SYMBOL_TEMPLATE = os.environ.get(
    "FUTURES_SYMBOL_TEMPLATE", "NIFTY-{month}{year}-FUT"
)

# Explicit, because %b is locale-dependent and Lambda's locale is not ours to
# assume.
MONTH_ABBREVIATIONS = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)

# --------------------------------------------------------------------------
# Session and schedule
# --------------------------------------------------------------------------
SESSION_START = datetime.time(9, 15)
SESSION_END = datetime.time(15, 30)

# The opening range: bars stamped 09:15, 09:20 and 09:25, i.e. 09:15-09:30.
ORB_END = datetime.time(9, 30)

# THIS FUNCTION HAS NO SCHEDULE. intraday-data-loader is the only thing on a
# cron in the intraday plane; it invokes this function once its candles are
# committed, so the run times below are the loader's - 09:45, 10:00 ... 15:30,
# plus the 15:35 closing sweep. FIRST_RUN and LAST_RUN are a second line of
# defence against a manual invocation, not the mechanism.
#
# FIRST_RUN MUST TRACK THE LOADER'S FIRST CRON. The loader fires at 09:45
# (schedule intraday-data-loader-open) and invokes this function; a FIRST_RUN
# later than that would reject the invoke as "outside session" and the whole
# snapshot -> classifier -> ... chain would silently skip that tick.
FIRST_RUN = datetime.time(9, 45)
LAST_RUN = datetime.time(15, 35)

# The interval whose bars the snapshot reads.
#
# SNAPSHOT_TS IS THE NEWEST BAR DHAN RETURNS, INCLUDING THE ONE STILL FORMING.
# At a 10:00 run the bucket stamped 10:00 has just opened, so the snapshot is
# stamped 10:00 and carries the live price rather than the 09:55 close. Two
# things follow and both are intended:
#
#   * snapshot_ts lands on the same grid as candle_5min.candle_ts - the row
#     stamped 10:00 here describes the bar stamped 10:00 there, which the
#     loader completes at the next run. The two tables join directly.
#   * at the 15:30 run there is no 15:30 bucket, because the market has
#     closed, so the newest bar returned is 15:25 and the last snapshot of the
#     day is stamped 15:25. That falls out of the rule rather than being a
#     special case.
#
# The cost is that the bar is seconds old: its own high, low and volume are
# near-empty. Nothing scored reads them - the classification reads `close`,
# which is the live price, and the SMAs and RSI built from closes. Session
# aggregates (day high/low, VWAP, the opening range) span every bar of the day
# and are unaffected.
CANDLE_INTERVAL_MINUTES = 5

# --------------------------------------------------------------------------
# Classification
#
# The rules live in the market-classifier layer and are shared with
# daily-market-sentiment - see layers/market-classifier/README.md. Only the
# amount of history to feed them is this function's business.
#
# THIS FUNCTION STILL FETCHES ITS OWN CANDLES. It does not read candle_5min,
# even though intraday-data-loader has just written it and invoked this
# function. A snapshot row is never revisited, so a stale input here is wrong
# forever; the loader having run is not proof that its write covered the bar
# this snapshot describes.
# --------------------------------------------------------------------------
# sma200 on the 5-minute frame needs 200 closed bars. A session is 75 five
# minute bars, so 200 bars is under three sessions - but weekends and holidays
# make calendar days a poor proxy for sessions, so the window is generous and
# the bar count is what is actually checked.
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "10"))

# Below this the run RAISES rather than classifying on partial inputs. The
# layer raises too; this is the earlier, clearer failure, naming the fetch
# rather than the indicator.
MIN_BARS_TO_CLASSIFY = int(os.environ.get("MIN_BARS_TO_CLASSIFY", "200"))

# The session is 09:15-15:30, which is 375 minutes. The range-expansion test
# scales the expected move by sqrt(elapsed / SESSION_MINUTES) so that it means
# the same thing at 10:00 as at 15:15 - see the layer README.
SESSION_MINUTES = 375

# The VIX-implied expected move: price x (vix/100) / sqrt(252) x K. The same
# formula and the same K daily-market-sentiment uses, so the two frames'
# volatility reads are comparable.
EXPECTED_MOVE_K = float(os.environ.get("EXPECTED_MOVE_K", "1.0"))
TRADING_DAYS_PER_YEAR = 252

# --------------------------------------------------------------------------
# Option chain windows
#
# Two different widths on purpose, both set by Amar:
#   * aggregates (PCR, OI totals, max pain) over ATM +-20 strikes;
#   * raw legs stored for 5 strikes only - ATM, +-1, +-2 - x CE and PE,
#     which is the 10 rows a snapshot the schema documents.
# The live chain carries 232 strikes (18150-29700, step 50, measured
# 2026-09-12), so +-20 is comfortably inside it.
#
# THE STRIKE STEP IS NEVER HARDCODED. It is derived from the sorted strike
# list in the payload, the same way the futures month is derived from the
# expiry list rather than written down.
# --------------------------------------------------------------------------
AGGREGATE_STRIKES_PER_SIDE = int(os.environ.get("AGGREGATE_STRIKES_PER_SIDE", "20"))
RAW_STRIKES_PER_SIDE = int(os.environ.get("RAW_STRIKES_PER_SIDE", "2"))

# --------------------------------------------------------------------------
# The classifier chain
#
# Once the measurement row is written this function invokes market-classifier
# with it, because the classifier's input IS this snapshot and the completion
# of the write is the only honest trigger for it - see dispatch.py. The
# classifier scores the row, writes algo.intraday_sentiments and invokes
# pattern-detector, which gates strategy-manager.
#
# UNSET MEANS OFF, and that is the point. With no name configured nothing is
# dispatched and a log line says so, which lets this ship with no behavioural
# change: the chain is switched on by setting this one variable after the
# classifier exists and this function has proved itself on a live session.
#
# Setting it also needs one IAM change - this function's execution role must
# allow lambda:InvokeFunction on the classifier's ARN, and NOT on a wildcard.
# --------------------------------------------------------------------------
CLASSIFIER_FUNCTION_NAME = os.environ.get("CLASSIFIER_FUNCTION_NAME", "")
CLASSIFIER_INVOCATION_TYPE = os.environ.get("CLASSIFIER_INVOCATION_TYPE", "Event")

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
# buildup and its BUILDUP_EPSILON_PCT tunable moved to market-classifier, which
# now derives the label from the futures deltas this function measures.

# --------------------------------------------------------------------------
# Market breadth (advance/decline)
#
# Two universes off ONE /marketfeed/ohlc fetch: the NIFTY 50 is a subset of the
# NIFTY 500, so both are counted from the same payload. The rosters are read
# from algo.index_constituents (Dhan has no membership endpoint); see breadth.py
# and the schema.
#
# ALWAYS ON - there is no enable flag. When the NIFTY 50 roster is absent from
# the database (an unseeded or wiped table) breadth falls back to STATIC_NIFTY50
# below, so it still produces the index breadth rather than nothing. The NIFTY
# 500 has no static fallback - 500 ids are not sensibly carried in code - so
# with no DB roster its mkt_* columns are left null and a warning is logged. A
# broken FETCH (an HTTP error, or a roster that returns no usable quote at all)
# still RAISES: "roster not seeded" degrades, "the fetch broke" fails loudly.
# Amar's call, 2026-09-19.
#
# BREADTH_INDICES maps a column prefix to an index_name. It is a constant, not a
# tunable: the columns nifty_* and mkt_* ARE the schema, so the mapping cannot
# move without a migration. NIFTY 500 last so mkt_sampled reads its roster.
BREADTH_BATCH_SIZE = int(os.environ.get("BREADTH_BATCH_SIZE", "1000"))
BREADTH_INDICES = (("nifty", "NIFTY50"), ("mkt", "NIFTY500"))
# The universe whose roster size mkt_sampled records - the market breadth one.
BREADTH_SAMPLED_INDEX = "NIFTY500"

# Built-in NIFTY 50 fallback, used ONLY when algo.index_constituents carries no
# NIFTY50 roster - the DB roster is authoritative whenever present. (security_id,
# symbol) pairs, all on NSE_EQ. This is the one place index membership is written
# down rather than derived, because Dhan exposes none; maintain it at the
# semi-annual (March/September) rebalance. security_ids taken from
# instrument_master, measured 2026-09-19.
STATIC_NIFTY50 = (
    ("25", "ADANIENT"), ("15083", "ADANIPORTS"), ("157", "APOLLOHOSP"),
    ("236", "ASIANPAINT"), ("5900", "AXISBANK"), ("16669", "BAJAJ-AUTO"),
    ("16675", "BAJAJFINSV"), ("317", "BAJFINANCE"), ("383", "BEL"),
    ("10604", "BHARTIARTL"), ("694", "CIPLA"), ("20374", "COALINDIA"),
    ("881", "DRREDDY"), ("910", "EICHERMOT"), ("5097", "ETERNAL"),
    ("1232", "GRASIM"), ("7229", "HCLTECH"), ("1333", "HDFCBANK"),
    ("467", "HDFCLIFE"), ("1363", "HINDALCO"), ("1394", "HINDUNILVR"),
    ("4963", "ICICIBANK"), ("11195", "INDIGO"), ("1594", "INFY"),
    ("1660", "ITC"), ("18143", "JIOFIN"), ("11723", "JSWSTEEL"),
    ("1922", "KOTAKBANK"), ("11483", "LT"), ("2031", "M&M"),
    ("10999", "MARUTI"), ("22377", "MAXHEALTH"), ("17963", "NESTLEIND"),
    ("11630", "NTPC"), ("2475", "ONGC"), ("14977", "POWERGRID"),
    ("2885", "RELIANCE"), ("21808", "SBILIFE"), ("3045", "SBIN"),
    ("4306", "SHRIRAMFIN"), ("3351", "SUNPHARMA"), ("3432", "TATACONSUM"),
    ("3499", "TATASTEEL"), ("11536", "TCS"), ("13538", "TECHM"),
    ("3506", "TITAN"), ("3456", "TMPV"), ("1964", "TRENT"),
    ("11532", "ULTRACEMCO"), ("3787", "WIPRO"),
)

# Rate limits are tighter than Dhan's documented 5/s: six unpaced calls earned
# DH-904 and stayed throttled. 4s spacing runs clean.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))

# 24 bind parameters per option_chain_snapshot row against Postgres's 65,535
# cap is a ceiling of 2,730 rows per statement. A snapshot writes 10.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "500"))
