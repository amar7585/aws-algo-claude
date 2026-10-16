"""
Configuration for intraday-fno-poller.

Only this function's own tunables. Epoch/IST helpers, the SSL context and the
Neon connection live in the neon-access layer - see neon_access.

This function measures the F&O side of the market every five minutes and stores
it. It does not classify, score, invoke or read the daily read: nothing here
depends on, or is depended on by, the 15-minute chain.
"""

import datetime
import os

# --------------------------------------------------------------------------
# Endpoints
#
# THE CHAIN IS NOT UNDER THE EXPIRY LIST'S BASE PATH. expirylist lives at
# /v2/optionchain/expirylist, but the chain itself is the FLAT /v2/optionchain
# - /v2/optionchain/optionchain answers "404 page not found". Measured
# 2026-09-12. Held as two whole URLs, not a shared base plus suffixes.
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
# NOT UNIQUE. `13` is both NIFTY (IDX_I/INDEX) and ABB (NSE_EQ/EQUITY). Both
# values are always passed together.
# --------------------------------------------------------------------------
NIFTY_SECURITY_ID = os.environ.get("NIFTY_SECURITY_ID", "13")
NIFTY_INSTRUMENT_TYPE = os.environ.get("NIFTY_INSTRUMENT_TYPE", "INDEX")

# The current-month NIFTY future. NOTHING about the contract is hardcoded: the
# month comes from Dhan's expiry list at run time, so the resolver rolls
# itself. Same rule as intraday-data-loader and intraday-market-sentiment.
FUTURES_UNDERLYING_SCRIP = int(os.environ.get("FUTURES_UNDERLYING_SCRIP", "13"))
FUTURES_UNDERLYING_SEG = os.environ.get("FUTURES_UNDERLYING_SEG", "IDX_I")
FUTURES_INSTRUMENT_TYPE = os.environ.get("FUTURES_INSTRUMENT_TYPE", "FUTIDX")

# NIFTY-OCT2026-FUT. The `NIFTY-` prefix matters: NIFTYFPI-OCT2026-FUT and
# NIFTYNXT50-OCT2026-FUT are different contracts that a looser pattern eats.
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

# THIS FUNCTION IS ON ITS OWN CRON, three EventBridge rules, 72 runs a day,
# offset ONE MINUTE past each five-minute mark so it does not start on the
# same second as intraday-data-loader (09:45, 10:00 ... every quarter hour):
#
#   -open     cron(36,41,46,51,56 9 ? * MON-FRI *)        09:36 - 09:56
#   -session  cron(1/5 10-14 ? * MON-FRI *)               10:01 - 14:56
#   -close    cron(1,6,11,16,21,26,31 15 ? * MON-FRI *)   15:01 - 15:31
#
# FIRST_RUN and LAST_RUN are a second line of defence against a manual
# invocation, not the mechanism. The window is deliberately a little wider than
# the cron: 09:35 .. 15:35.
FIRST_RUN = datetime.time(9, 35)
LAST_RUN = datetime.time(15, 35)

# The interval whose bars the snapshot reads.
#
# SNAPSHOT_TS IS THE NEWEST BAR DHAN RETURNS, INCLUDING THE ONE STILL FORMING -
# the same grain intraday_fno_data uses, so a fast row and a slow row at a
# 15-minute mark carry the same snapshot_ts and join directly. The LAST CLOSED
# bar is stored separately (idx_bar_*, fut_bar_*): the forming bar's own volume
# and OI are near-empty and are never what a reader should compare.
CANDLE_INTERVAL_MINUTES = 5

# --------------------------------------------------------------------------
# Option chain windows - the same two widths as intraday-market-sentiment, so a
# fast row and a slow row are directly comparable:
#   * aggregates (PCR, OI totals, max pain) over ATM +-20 strikes;
#   * raw legs stored for 5 strikes only - ATM, +-1, +-2 - x CE and PE.
# THE STRIKE STEP IS NEVER HARDCODED; it is derived from the payload.
# --------------------------------------------------------------------------
AGGREGATE_STRIKES_PER_SIDE = int(os.environ.get("AGGREGATE_STRIKES_PER_SIDE", "20"))
RAW_STRIKES_PER_SIDE = int(os.environ.get("RAW_STRIKES_PER_SIDE", "2"))

# Rate limits are tighter than Dhan's documented 5/s: six unpaced calls earned
# DH-904 and stayed throttled. 4s spacing runs clean. Pacing is per function
# instance; Dhan's limit is per account, so overlap with another function's run
# can still draw DH-904 - the client retries with backoff.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))

# 24 bind parameters per fno_5m_leg row against Postgres's 65,535 cap is a
# ceiling of 2,730 rows per statement. A tick writes 10.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "500"))
