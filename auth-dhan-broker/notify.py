"""Telegram delivery."""

import json
import logging
import urllib.parse
import urllib.request

from neon_access import SSL_CONTEXT

from config import HTTP_TIMEOUT_SECONDS, NEXT_SESSION_HORIZON_DAYS
from params import read_telegram_config

logger = logging.getLogger()


def format_holiday_notice(today, holiday, next_session, schedules):
    """The holiday message. Plain text, same chat as the daily brief."""
    lines = [
        f"Market holiday - {today:%a %d %b %Y}",
        "",
        holiday,
        "",
        "No access token minted.",
        "Session schedules disabled:",
    ]
    lines += [f"  {name} ({result})" for name, result in schedules.items()]
    lines += [""]
    if next_session:
        lines.append(f"Next session: {next_session:%a %d %b %Y}")
    else:
        # Only reachable when the calendar has run out of rows, which is worth
        # saying out loud - it is the one failure this design cannot see.
        lines.append(
            f"Next session: unknown - no open weekday found in the next "
            f"{NEXT_SESSION_HORIZON_DAYS} days. The holiday calendar may need "
            f"reseeding."
        )
    return "\n".join(lines)


def send_telegram(text):
    """Deliver one message. Raises if Telegram refuses it.

    Called only AFTER the schedules are disabled. The notice is courtesy; the
    gate is the job, and a Telegram outage must not leave the schedules armed
    on a holiday.
    """
    token, chat_id = read_telegram_config()
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    with urllib.request.urlopen(
        request, timeout=HTTP_TIMEOUT_SECONDS, context=SSL_CONTEXT
    ) as response:
        body = json.loads(response.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"telegram refused the message: {body}")
    logger.info("telegram holiday notice delivered to chat %s", chat_id)
