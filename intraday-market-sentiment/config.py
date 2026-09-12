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

# Snapshots are taken FIVE MINUTES AFTER a 15-minute boundary - 09:35, 09:50,
# 10:05 ... 15:35 - so that the 5-minute bar the snapshot describes has just
# closed. FIRST_RUN and LAST_RUN bound the window; three EventBridge rules
# produce exactly these 25 times, and the guard in the handler is the second
# line of defence rather than the mechanism.
FIRST_RUN = datetime.time(9, 35)
LAST_RUN = datetime.time(15, 35)

# The interval whose bars the snapshot reads. 5 is not arbitrary: at :35, :50,
# :05 and :20 a 5-minute bucket has closed exactly on the run time, so the
# newest CLOSED bar is never more than a few seconds stale. A 15-minute fetch
# would be up to 5 minutes behind at every run.
CANDLE_INTERVAL_MINUTES = 5
CANDLE_INTERVAL_SECONDS = CANDLE_INTERVAL_MINUTES * 60

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
