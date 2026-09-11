"""
Configuration for daily-market-sentiment.

Only this function's own tunables. Epoch/IST helpers, the SSL context and the
Neon connection live in the neon-access layer - see neon_access.
"""

import os

CHARTS_BASE = os.environ.get("DHAN_CHARTS_BASE", "https://api.dhan.co/v2/charts/")

TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")
TELEGRAM_PARAMETER_NAME = os.environ.get(
    "TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief"
)

# --------------------------------------------------------------------------
# Instruments
#
# (security_id, instrument_type) is instrument identity - security_id ALONE IS
# NOT UNIQUE. 19 security_ids in instrument_master carry more than one
# instrument_type; `13` is both NIFTY (IDX_I/INDEX) and ABB (NSE_EQ/EQUITY).
# A lookup on security_id alone silently resolved to ABB on 2026-09-11 and
# wrote 204 ABB candles plus an ABB sentiment row labelled NIFTY. Both values
# below are always passed together.
# --------------------------------------------------------------------------
NIFTY_SECURITY_ID = os.environ.get("NIFTY_SECURITY_ID", "13")
NIFTY_INSTRUMENT_TYPE = os.environ.get("NIFTY_INSTRUMENT_TYPE", "INDEX")
VIX_SECURITY_ID = os.environ.get("VIX_SECURITY_ID", "21")
VIX_INSTRUMENT_TYPE = os.environ.get("VIX_INSTRUMENT_TYPE", "INDEX")

import datetime  # noqa: E402  - session bounds are config, not clock logic

SESSION_START = datetime.time(9, 15)
SESSION_END = datetime.time(15, 30)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
# Cold start depth in calendar days. 300 measured to 204 daily candles on
# 2026-09-10 - four clear of the 200 sma200 needs. Cold start happens once;
# after it the table only grows. The sma200 guard covers the edge.
COLD_START_DAYS = int(os.environ.get("COLD_START_DAYS", "300"))

# expected_move = price x (VIX/100) / sqrt(252) x K, the textbook 1-sigma daily
# move. K picks the coverage. Measured on a held-out test set of 284 NIFTY
# sessions (params fitted on the preceding 660):
#     K 0.71 -> 50%   K 1.00 -> 73%   K 1.10 -> 82%   K 1.42 -> 93%
#
# This REPLACES legacy's `atr_14 x {TREND .35, TRANSITION .25, RANGE .18}`,
# which measured 5.7% coverage over 944 sessions - the day left that band 94%
# of the time. VIX-implied also beat ATR-14 and ADR-10 on correlation with the
# actual move (0.410 vs 0.365 and 0.409), and no blend of VIX with realised
# excursion or the overnight gap improved on it: the best blend scored TEST MAE
# 62.5 against plain VIX's 61.7. Do not add terms back without numbers that
# beat 61.7.
EXPECTED_MOVE_K = float(os.environ.get("EXPECTED_MOVE_K", "1.0"))
TRADING_DAYS_PER_YEAR = 252

# Rate limits are tighter than Dhan's documented 5/s: six unpaced calls earned
# DH-904 and stayed throttled. 4s spacing runs clean.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))

# 8 bind parameters per row against Postgres's 65,535 cap is a hard ceiling of
# 8,191 rows per statement; this sits well under it.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "5000"))
