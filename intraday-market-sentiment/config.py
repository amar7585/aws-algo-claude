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
# committed, so the run times below are the loader's - 10:00, 10:15 ... 15:30,
# plus the 15:35 closing sweep. FIRST_RUN and LAST_RUN are a second line of
# defence against a manual invocation, not the mechanism.
FIRST_RUN = datetime.time(10, 0)
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
# The strategy chain
#
# Once the row is written this function invokes strategy-manager with it,
# because the manager's input IS this snapshot and the completion of the
# write is the only honest trigger for it - see dispatch.py.
#
# UNSET MEANS OFF, and that is the point. With no name configured nothing is
# dispatched and a log line says so, which lets this ship with no behavioural
# change: the chain is switched on by setting this one variable after the
# manager exists and this function has proved itself on a live session.
#
# Setting it also needs one IAM change - this function's execution role must
# allow lambda:InvokeFunction on the manager's ARN, and NOT on a wildcard.
# --------------------------------------------------------------------------
STRATEGY_MANAGER_FUNCTION_NAME = os.environ.get("STRATEGY_MANAGER_FUNCTION_NAME", "")
STRATEGY_MANAGER_INVOCATION_TYPE = os.environ.get(
    "STRATEGY_MANAGER_INVOCATION_TYPE", "Event"
)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
# The move below which the buildup label treats a change as no change, in
# percent. 0.0 means pure sign: any tick either way counts, and the label can
# flip on noise in a quiet fifteen minutes.
#
# NOT SET FROM MEASUREMENT. A sensible floor needs the distribution of
# 15-minute price and OI moves across real sessions, which this function has
# to run for a while to produce. It is a tunable rather than a constant so
# that can be set later without touching sentiment.py.
BUILDUP_EPSILON_PCT = float(os.environ.get("BUILDUP_EPSILON_PCT", "0.0"))

# Rate limits are tighter than Dhan's documented 5/s: six unpaced calls earned
# DH-904 and stayed throttled. 4s spacing runs clean.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))

# 24 bind parameters per option_chain_snapshot row against Postgres's 65,535
# cap is a ceiling of 2,730 rows per statement. A snapshot writes 10.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "500"))
