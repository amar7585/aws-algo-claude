"""
The parameters only this function reads and writes.

The Neon connection string is shared and lives in neon_access.ssm.

Nothing here logs or returns the token. Only its prefix and expiry are ever
logged; consumers read the token from the parameter itself.
"""

import json
import logging
import time

import boto3

from neon_access import get_parameter

from config import TELEGRAM_PARAMETER_NAME, TOKEN_PARAMETER_NAME

logger = logging.getLogger()

# Reused across warm invocations; creating a boto3 client is not cheap.
_ssm = boto3.client("ssm")


def write_stored_token(access_token, expires_at):
    """Persist the token as a single JSON blob.

    One parameter rather than two so the token and its expiry cannot drift
    apart: a torn write leaving a new token beside an old expiry would look
    healthy and behave wrongly.
    """
    record = {
        "access_token": access_token,
        "expires_at": expires_at,
        "refreshed_at": int(time.time()),
        "source": "totp",
    }
    _ssm.put_parameter(
        Name=TOKEN_PARAMETER_NAME,
        Value=json.dumps(record),
        Type="SecureString",
        Overwrite=True,
    )
    return record


def read_telegram_config():
    config = json.loads(get_parameter(TELEGRAM_PARAMETER_NAME))
    token, chat_id = config.get("bot_token"), config.get("chat_id")
    if not token or not chat_id:
        raise RuntimeError(f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id")
    return token, chat_id
