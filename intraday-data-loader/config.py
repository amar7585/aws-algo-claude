"""
Configuration for intraday-data-loader.

Only this function's own tunables. Epoch/IST helpers, the SSL context and the
Neon connection live in the neon-access layer - see neon_access.
"""

import datetime
import os

CHARTS_BASE = os.environ.get("DHAN_CHARTS_BASE", "https://api.dhan.co/v2/charts/")
OPTIONCHAIN_BASE = os.environ.get(
    "DHAN_OPTIONCHAIN_BASE", "https://api.dhan.co/v2/optionchain/"
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

# The current-month NIFTY future. NOTHING about the contract is hardcoded: the
# month comes from Dhan's expiry list at run time (see dhan.expiry_list), so
# the resolver rolls itself. See resolve_current_future() in db.py.
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

# Which interval lands in which table. All three carry the same shape as
# candle_daily: (security_id, instrument_type, candle_ts, o/h/l/c, volume).
INTERVAL_TABLES = {
    5: "algo.candle_5min",
    15: "algo.candle_15min",
    60: "algo.candle_1hr",
}

# The closing sweep. The schedule runs to 15:30, but at 15:30 the 15:25 5-min
# bar, the 15:15 15-min bar and the 15:15 hourly bar have only just closed -
# with no later run they would sit PARTIAL in the database forever, quietly
# corrupting any end-of-day read. One extra run at 15:35 fetches all three
# intervals and finalises them.
CLOSING_SWEEP = datetime.time(15, 35)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
# Cold-start depth. Dhan caps an intraday window at 90 days per call, so 90 is
# both the depth and the ceiling - one call, no chunking. Going deeper means
# chunking forward in <=90-day windows, which is deliberately not built.
COLD_START_DAYS = int(os.environ.get("COLD_START_DAYS", "90"))
MAX_WINDOW_DAYS = 90

# Rate limits are tighter than Dhan's documented 5/s: six unpaced calls earned
# DH-904 and stayed throttled. 4s spacing runs clean.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))

# 8 bind parameters per row against Postgres's 65,535 cap is a hard ceiling of
# 8,191 rows per statement; this sits well under it.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "5000"))
