"""
pattern-recorder settings. The pattern rules' own tunables live in structure.py
and patterns.py (all provisional, all env-overridable); this file is only about
reading and writing.
"""

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
