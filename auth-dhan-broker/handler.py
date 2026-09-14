"""
Dhan Broker Auth — AWS Lambda function

Mints a DhanHQ v2 access token and stores it in SSM Parameter Store, so the
session functions never have to authenticate themselves.

    weekday? ──no──▶ skip
       │yes
    holiday? ──yes──▶ DISABLE daily + intraday schedules ──▶ skip
       │no
    TOTP ──▶ POST /app/generateAccessToken ──▶ write /algo/dhan/token
                                          ├──▶ non-200 or no token ──▶ raise
                                          └──▶ ENABLE daily + intraday schedules

Trigger: EventBridge Scheduler, cron(0 8 ? * MON-FRI *) Asia/Kolkata — once
each weekday morning, before the 09:15 open. A token lives 24 hours, so the
08:00 token covers the whole session with hours to spare.

THIS FUNCTION DECIDES WHETHER THE DAY HAPPENS. It is the only thing that runs
before the session on every weekday, holiday included — a holiday IS a weekday,
so the MON-FRI cron still fires and the decision point is symmetric: disable on
a holiday, enable on a trading day. The session functions are therefore never
invoked on a holiday rather than invoked and skipping.

That concentration is deliberate and costs little, because this function is
already a hard dependency of the whole day: every consumer calls
read_token_record(), which RAISES on a missing or expired token. A morning
where auth fails is already a dead day, alarmed by error-notifier. Attaching
the schedule decision to it adds no new way to lose a session.

WHY A HOLIDAY IS AN ABSENT ROW. algo.trading_holiday stores closures only, so
"not in the table" means a normal session. A calendar nobody reseeded keeps the
system trading — a few no-op invocations — rather than making it go quiet,
which nothing here can detect: no alarm in this system can see a function that
was never invoked. See trading-calendar/README.md.

Weekends are skipped because nothing trades then; Monday simply mints a fresh
token like any other weekday. There is no renew path, and dhan.py records why.

Module layout:
    config.py     parameter names, URLs, the managed schedule names
    params.py     SSM — writes the token record, reads the Telegram config
    dhan.py       TOTP, generateAccessToken, the JWT expiry claim
    db.py         the trading-holiday reads
    notify.py     the holiday notice
    schedules.py  switching the session schedules on and off
    handler.py    the order those happen in, which is the load-bearing part

Packaging notes:
  - stdlib, boto3 (pre-installed in the runtime), and the neon-db-driver +
    neon-access layers. THIS FUNCTION USED TO NEED NO LAYERS AT ALL, and the
    holiday read is what changed that: it now also needs NEON_CONNECTION_STRING
    and so ssm:GetParameter + kms:Decrypt, which had been removed here on the
    grounds that this function only writes. Both layer ARNs are pinned and must
    be repointed together when either is republished.

Conventions:
  - expires_at is epoch seconds, read from the JWT's own `exp` claim — see
    dhan.py for what the API's own expiryTime string does instead.
  - Nothing here logs or returns the token. Only its prefix and expiry are
    logged; consumers read the token from the parameter.
"""

import json
import logging
import os
import time

from neon_access import connect, read_neon_connection_string, today_ist

from config import TOKEN_PARAMETER_NAME
from db import holiday_for, next_trading_day
from dhan import generate_token, token_expiry_epoch
from notify import format_holiday_notice, send_telegram
from params import write_stored_token
from schedules import set_managed_schedules

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """EventBridge Scheduler entry point.

    Raises on failure so the invocation is recorded as an error and the
    schedule's retry policy applies. Returns metadata only — never the token;
    consumers read that from the parameter.
    """
    started = time.time()
    today = today_ist()

    # Only reachable by hand: the cron is MON-FRI. The session schedules are
    # MON-FRI too, so a weekend run has nothing to switch and mints no token.
    if today.weekday() >= 5:
        logger.info("%s is a %s - not a trading day", today, today.strftime("%A"))
        return {
            "status": "skipped_non_trading_day",
            "date": today.isoformat(),
            "reason": "weekend",
        }

    conn = connect(read_neon_connection_string())
    try:
        holiday = holiday_for(conn, today)
        # Read on the same connection rather than reopening one for the
        # notice: Neon autosuspends, and this is the cold wake-up of the day.
        next_session = next_trading_day(conn, today) if holiday else None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if holiday:
        # Schedules go off FIRST, and no token is minted: nothing will run
        # today that could use one, and it would be long expired before the
        # next session anyway. The notice comes after, because the gate is the
        # job — a Telegram outage must not leave the schedules armed.
        schedules = set_managed_schedules("DISABLED")
        logger.info("%s is a holiday (%s) - session schedules disabled", today, holiday)
        send_telegram(format_holiday_notice(today, holiday, next_session, schedules))
        return {
            "status": "skipped_non_trading_day",
            "date": today.isoformat(),
            "reason": "holiday",
            "holiday": holiday,
            "next_session": next_session.isoformat() if next_session else None,
            "schedules": schedules,
        }

    # Read here rather than at the top so a holiday still disables the
    # schedules on a day when one of these is missing.
    client_id = os.environ["DHAN_CLIENT_ID"]
    pin = os.environ["DHAN_PIN"]
    totp_secret = os.environ["DHAN_TOTP_SECRET"]

    access_token = generate_token(client_id, pin, totp_secret)
    expires_at = token_expiry_epoch(access_token)
    record = write_stored_token(access_token, expires_at)

    # Armed only once the token is stored. The other order would switch the
    # session schedules on against a token that was never refreshed, turning a
    # recoverable auth failure into a day of failing invocations.
    schedules = set_managed_schedules("ENABLED")

    result = {
        "status": "success",
        "source": "totp",
        "date": today.isoformat(),
        "schedules": schedules,
        "expires_at": expires_at,
        "expires_in_hours": round((expires_at - time.time()) / 3600, 2),
        "parameter": TOKEN_PARAMETER_NAME,
        "refreshed_at": record["refreshed_at"],
        "elapsed_seconds": round(time.time() - started, 2),
    }
    logger.info(
        "dhan token refreshed: %s (token %s...)", json.dumps(result), access_token[:12]
    )
    return result
