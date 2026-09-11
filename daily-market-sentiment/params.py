"""
The parameters only this function reads.

The Neon connection string is shared and lives in neon_access.ssm.
"""

import json
import logging

from neon_access import get_parameter, now_epoch

from config import TELEGRAM_PARAMETER_NAME, TOKEN_PARAMETER_NAME

logger = logging.getLogger()


def read_access_token():
    """Read the Dhan token and refuse to use a dead one."""
    record = json.loads(get_parameter(TOKEN_PARAMETER_NAME))
    token, expires_at = record.get("access_token"), record.get("expires_at")
    if not token or not expires_at:
        raise RuntimeError(
            f"{TOKEN_PARAMETER_NAME} must hold access_token and expires_at"
        )
    remaining = int(expires_at) - now_epoch()
    if remaining <= 0:
        raise RuntimeError(
            f"dhan token expired {abs(remaining)}s ago - auth-dhan-broker has "
            f"not refreshed it"
        )
    logger.info("dhan token ok, %.1f hours remaining", remaining / 3600)
    return token


def read_telegram_config():
    config = json.loads(get_parameter(TELEGRAM_PARAMETER_NAME))
    token, chat_id = config.get("bot_token"), config.get("chat_id")
    if not token or not chat_id:
        raise RuntimeError(f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id")
    return token, chat_id
