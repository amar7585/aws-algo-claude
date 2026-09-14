"""
Configuration for auth-dhan-broker.

Only this function's own tunables. Epoch/IST helpers, the SSL context and the
Neon connection live in the neon-access layer - see neon_access.
"""

import os

GENERATE_URL = os.environ.get(
    "DHAN_GENERATE_TOKEN_URL",
    "https://auth.dhan.co/app/generateAccessToken",
)

TOKEN_PARAMETER_NAME = os.environ.get("TOKEN_PARAMETER_NAME", "/algo/dhan/token")

# The same parameter daily-market-sentiment and error-notifier read. One chat,
# one bot: a holiday notice belongs beside the daily brief it replaces.
TELEGRAM_PARAMETER_NAME = os.environ.get(
    "TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief"
)

# How far ahead the holiday notice looks for the next session. Generous: the
# longest run of weekday closures here is two (Diwali 2025, 21-22 October).
NEXT_SESSION_HORIZON_DAYS = 10

HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))

# RFC 6238 defaults, which is what Dhan's authenticator enrolment issues.
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6

HOLIDAY_TABLE = "algo.trading_holiday"

# The schedules this function switches on and off. NAMES, not ARNs: the
# Scheduler API addresses a schedule by name within its group. Configured
# rather than hardcoded so renaming a schedule is an environment change.
#
# THIS FUNCTION'S OWN SCHEDULE MUST NEVER APPEAR HERE. It is the heartbeat that
# makes the decision, so a run that switched it off could never switch it back
# on — the system would stay dark until someone noticed by hand.
#
# THE DEFAULT IS SCHEDULE NAMES, NOT FUNCTION NAMES. It read
# "daily-market-sentiment,intraday-data-loader" until 2026-09-14 — function
# names, which match no schedule and would have raised on GetSchedule. Never
# exercised, because the environment variable is always set, but a default that
# cannot work is not a default. THE LOADER HAS TWO RULES and both belong here:
# leaving out `-close` would arm the 15:00–15:35 runs on a holiday.
MANAGED_SCHEDULES = [
    name.strip()
    for name in os.environ.get(
        "MANAGED_SCHEDULE_NAMES",
        "daily-market-sentiment-daily,"
        "intraday-data-loader-session,"
        "intraday-data-loader-close",
    ).split(",")
    if name.strip()
]
SCHEDULE_GROUP = os.environ.get("SCHEDULE_GROUP_NAME", "default")
