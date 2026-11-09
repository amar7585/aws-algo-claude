"""
pattern-recorder settings. The pattern rules' own tunables live in structure.py
and patterns.py (all provisional, all env-overridable); this file is about the
trigger, the reads and writes, Dhan, and the alerts.
"""

import datetime
import os

# The future's instrument_type in candle_5min - the same constant the loader
# stored its bars under, so the (security_id, instrument_type) identity matches
# (hard rule #4).
FUTURES_INSTRUMENT_TYPE = os.environ.get("FUTURES_INSTRUMENT_TYPE", "FUTIDX")

# Calendar days of history read on every run. It must cover the previous
# session (PDH/PDL, the volume baseline across the open) and the previous
# inputs.RVOL_SESSIONS (20) sessions for time-of-day volume. 40 calendar days is
# about 27 sessions, enough with a week of holidays to spare.
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "40"))

# Rows per INSERT. Postgres caps bind parameters at 65,535 per statement; at 35
# columns a row that is 1,872 rows. A session holds ~25 events (max 46 measured
# over 76 sessions), so one statement is the norm.
UPSERT_BATCH_SIZE = int(os.environ.get("UPSERT_BATCH_SIZE", "500"))

# ---- today's candles, from Dhan (v2.1) -------------------------------------
# intraday-data-loader is the only candle_5min writer and runs every 15 min, so
# a 5-min run reads TODAY's index and future candles from Dhan itself, in
# memory, never saved (Amar, 2026-11-07). Earlier sessions still come from
# candle_5min.
TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")
CHARTS_BASE = os.environ.get("DHAN_CHARTS_BASE", "https://api.dhan.co/v2/charts/")
CANDLE_INTERVAL_MINUTES = 5
# Dhan's limit is per account and tighter than documented: six unpaced calls
# earned DH-904 (intraday-data-loader/config.py). Same 4 s pacing and backoff as
# every other caller.
API_PACING_SECONDS = float(os.environ.get("API_PACING_SECONDS", "4.0"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "60"))
SESSION_START = datetime.time(9, 15)
SESSION_END = datetime.time(15, 30)   # exclusive: the last bar starts at 15:25

# ---- alerts (plan v3, approved 2026-11-09) ------------------------------------
# UNSET MEANS SHADOW: every run decides which events are alerts and logs them as
# "WOULD SEND", but nothing goes to Telegram or strategy-manager and no sent mark
# is written. Set it to "on" to send.
ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "").strip().lower() in ("1", "on", "true", "yes")
# Unset = Telegram only, no manager dispatch, even with alerts on.
STRATEGY_MANAGER_FUNCTION_NAME = os.environ.get("STRATEGY_MANAGER_FUNCTION_NAME", "")
TELEGRAM_PARAMETER_NAME = os.environ.get("TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief")
TELEGRAM_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_TIMEOUT_SECONDS", "8"))
