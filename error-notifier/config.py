"""
Configuration for error-notifier.

Deliberately no neon-access layer. That layer's package imports pg8000 at
module load, so attaching it would drag the database driver into a function
that never touches the database - and would need BOTH layers to import at all.
This function is stdlib plus boto3, which ships with the runtime, the same
shape as auth-dhan-broker.
"""

import os

TELEGRAM_PARAMETER_NAME = os.environ.get(
    "TELEGRAM_PARAMETER_NAME", "/algo/telegram/brief"
)

# Repeat suppression. A Dhan outage fails every intraday run, and at 68 runs a
# day that is 68 identical messages. Repeats of the same signature inside this
# window are dropped.
#
# BEST EFFORT, not a guarantee: the window lives in a module global, so it only
# holds while Lambda keeps the container warm. A cold start forgets it. This
# reduces a flood; it does not promise exactly one message.
SUPPRESSION_SECONDS = int(os.environ.get("SUPPRESSION_SECONDS", "1800"))

# How much of a normalised message forms its signature.
SIGNATURE_CHARS = int(os.environ.get("SIGNATURE_CHARS", "120"))

# Telegram caps a message at 4096 characters.
MAX_EVENTS_PER_MESSAGE = int(os.environ.get("MAX_EVENTS_PER_MESSAGE", "5"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "3500"))
MAX_EVENT_CHARS = int(os.environ.get("MAX_EVENT_CHARS", "600"))

HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "20"))
