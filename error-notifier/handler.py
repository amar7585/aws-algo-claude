"""
Error Notifier - AWS Lambda function

Pushes failures from every other function in this system to Telegram.

Each function's CloudWatch log group carries a subscription filter that streams
matching events here; this decodes them, drops repeats, and sends one message.

WHY A LOG SUBSCRIPTION RATHER THAN A try/except IN EACH FUNCTION.
A catch block cannot see the two failures that matter most:

  * a TIMEOUT kills the process - no `except` ever runs
  * an IMPORT ERROR fires before the handler module loads at all, which is
    exactly the failure a detached layer produces

Both still reach the log. The subscription also touches none of the four
existing functions - no redeploys, no duplicated Telegram code, and one place
to handle noise.

WHY NOT A CLOUDWATCH ALARM. An alarm can only say "Errors >= 1". The log event
carries the actual exception, so the message names the instrument, the
interval and the HTTP status.

Modules:
    config.py   this function's tunables
    notify.py   Telegram

No layers. See config.py for why neon-access is deliberately not attached.
"""

import base64
import datetime
import gzip
import json
import logging
import re
import time

from config import (
    MAX_EVENT_CHARS,
    MAX_EVENTS_PER_MESSAGE,
    MAX_MESSAGE_CHARS,
    SIGNATURE_CHARS,
    SUPPRESSION_SECONDS,
)
from notify import send_telegram

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Display-only. Every STORED time value in this system is epoch seconds and
# that conversion lives once, in the neon-access layer - but this function
# stores nothing and attaching that layer would pull in pg8000 (see config.py),
# so the three lines are repeated rather than the driver imported.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# signature -> epoch of last send. Warm-container only; see SUPPRESSION_SECONDS.
_last_sent = {}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NUMBER = re.compile(r"\d+")


def decode_payload(event):
    """CloudWatch Logs delivers gzipped, base64-encoded JSON."""
    data = (event or {}).get("awslogs", {}).get("data")
    if not data:
        raise RuntimeError(
            f"not a CloudWatch Logs subscription event: keys {sorted(event or {})}"
        )
    return json.loads(gzip.decompress(base64.b64decode(data)).decode())


def signature(message):
    """
    A stable key for 'the same failure again'.

    Request ids and counts differ between otherwise identical failures, so both
    are flattened before the message is truncated to a signature. Without that,
    every repeat of one Dhan outage would look novel and send its own message.
    """
    flattened = _NUMBER.sub("N", _UUID.sub("UUID", message))
    return " ".join(flattened.split())[:SIGNATURE_CHARS]


def should_send(sig, now):
    last = _last_sent.get(sig)
    if last is not None and now - last < SUPPRESSION_SECONDS:
        return False
    _last_sent[sig] = now
    return True


def format_message(log_group, log_stream, events):
    function = log_group.rsplit("/", 1)[-1]
    first = datetime.datetime.fromtimestamp(events[0]["timestamp"] / 1000, IST)
    lines = [f"[!] {function}", f"{first:%d %b %Y %H:%M:%S} IST", ""]

    for event in events[:MAX_EVENTS_PER_MESSAGE]:
        text = " ".join(event["message"].split())
        if len(text) > MAX_EVENT_CHARS:
            text = text[:MAX_EVENT_CHARS] + " ..."
        lines.append(text)
        lines.append("")

    hidden = len(events) - MAX_EVENTS_PER_MESSAGE
    if hidden > 0:
        lines.append(f"... and {hidden} more in the same batch")
        lines.append("")

    lines.append(f"stream {log_stream}")
    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "\n... truncated"
    return message


def lambda_handler(event, context):
    payload = decode_payload(event)

    # CloudWatch sends one of these when a subscription filter is created, to
    # prove it can reach the destination. It carries no log events.
    if payload.get("messageType") == "CONTROL_MESSAGE":
        logger.info("control message from %s - nothing to send", payload.get("logGroup"))
        return {"status": "control_message"}

    log_group = payload.get("logGroup", "")
    log_stream = payload.get("logStream", "")

    # NEVER act on our own log group. If this function's log group were ever
    # subscribed to this function, one error would log, which would trigger
    # this, which would error... an unbounded loop that bills for itself. The
    # README says not to create that subscription; this makes it inert even if
    # somebody does.
    own = getattr(context, "log_group_name", None)
    if own and log_group == own:
        logger.warning(
            "refusing events from my own log group %s - that subscription is a "
            "feedback loop and should be deleted", log_group
        )
        return {"status": "refused_self_subscription", "log_group": log_group}

    events = payload.get("logEvents") or []
    if not events:
        logger.info("no log events in payload from %s", log_group)
        return {"status": "no_events", "log_group": log_group}

    now = int(time.time())
    fresh = [e for e in events if should_send(signature(e.get("message", "")), now)]
    suppressed = len(events) - len(fresh)

    if not fresh:
        logger.info(
            "all %d event(s) from %s suppressed as repeats", suppressed, log_group
        )
        return {"status": "suppressed", "log_group": log_group,
                "suppressed": suppressed}

    send_telegram(format_message(log_group, log_stream, fresh))
    logger.info(
        "alerted on %d event(s) from %s, %d suppressed",
        len(fresh), log_group, suppressed,
    )
    return {
        "status": "sent",
        "log_group": log_group,
        "alerted": len(fresh),
        "suppressed": suppressed,
    }
