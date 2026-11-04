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

# "Invalid TOTP" is retried INSIDE the run, five minutes apart. Dhan answers it
# to a correctly computed code (our clock and Dhan's agree to ~20 ms, measured
# 2026-11-04) and the refusals come in spells of a few minutes: 15 of 37 trading
# days to 2026-11-04 needed a retry, and on 10-08 and 11-04 all three automatic
# attempts failed while a run by hand later that morning worked.
#
# Two retries is the most that fits: Lambda stops any run at 15 minutes, and
# two waits of 300 s plus three attempts of up to HTTP_TIMEOUT_SECONDS each is
# ~12 minutes. The function's own async retries are set to 0 so this loop is the
# only one — Lambda's would re-run the whole thing 1-2 minutes after a failure.
# Any other refusal raises at once: a wrong PIN does not get better by waiting.
TOTP_RETRIES = int(os.environ.get("TOTP_RETRIES", "2"))
TOTP_RETRY_DELAY_SECONDS = int(os.environ.get("TOTP_RETRY_DELAY_SECONDS", "300"))

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
# cannot work is not a default. THE LOADER HAS THREE RULES and all belong here:
# one left out keeps firing on a holiday. `-open` (09:45) was added by the
# 2026-09-18 retime and missed here until 2026-10-02, when it fired on Gandhi
# Jayanti against an expired token.
#
# ADDING A SCHEDULE TAKES TWO EDITS - THREE IF IT HAS ITS OWN SCHEDULER ROLE:
# this list (in practice the environment variable), the ToggleSessionSchedules
# resource list in the role's holiday-gate-access policy (it names schedule
# ARNs), and - for a schedule whose target role is new - that role's ARN in the
# PassScheduleTargetRoles statement, because UpdateSchedule re-passes the
# schedule's own role. A schedule listed here but missing either grant fails the
# run with AccessDenied. New names go LAST so that, should that happen, the error
# lands after the others are already switched. The intraday-fno-poller trio
# (2026-10-16) needed all three edits: it has its own Scheduler role.
MANAGED_SCHEDULES = [
    name.strip()
    for name in os.environ.get(
        "MANAGED_SCHEDULE_NAMES",
        "daily-market-sentiment-daily,"
        "intraday-data-loader-session,"
        "intraday-data-loader-close,"
        "intraday-data-loader-open,"
        "intraday-fno-poller-session,"
        "intraday-fno-poller-close,"
        "intraday-fno-poller-open",
    ).split(",")
    if name.strip()
]
SCHEDULE_GROUP = os.environ.get("SCHEDULE_GROUP_NAME", "default")
