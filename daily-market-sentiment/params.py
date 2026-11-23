"""
The parameters only this function reads.

The Neon connection string is shared and lives in neon_access.ssm.
"""

import base64
import json
import logging
import os

from neon_access import get_parameter, now_epoch

from config import TELEGRAM_PARAMETER_NAME, TOKEN_PARAMETER_NAME

logger = logging.getLogger()


def read_token_record():
    """Read /algo/dhan/token and refuse to use a dead token."""
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
    return record


def read_access_token():
    """The Dhan access token (charts need only this)."""
    return read_token_record()["access_token"]


def jwt_claims(access_token):
    """The token's own payload claims (copied from intraday-fno-poller/params.py)."""
    parts = access_token.split(".")
    if len(parts) != 3:
        raise RuntimeError(
            f"dhan access token is not a three-part JWT ({len(parts)} segments)"
        )
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def read_client_id(record):
    """
    The Dhan client id, for the option-chain calls (the morning brief,
    2026-11-22). Copied from intraday-fno-poller/params.py: it comes from the
    token's own `dhanClientId` claim, so it cannot drift from a rotated token;
    the record's client_id and DHAN_CLIENT_ID are fallbacks only.
    """
    claims = jwt_claims(record["access_token"])
    from_jwt = claims.get("dhanClientId")
    if from_jwt:
        logger.info("client id from the access token's dhanClientId claim")
        return str(from_jwt)
    from_record = record.get("client_id")
    if from_record:
        logger.info("client id from %s", TOKEN_PARAMETER_NAME)
        return str(from_record)
    from_env = os.environ.get("DHAN_CLIENT_ID")
    if from_env:
        logger.info("client id from DHAN_CLIENT_ID (token carried no claim)")
        return from_env
    raise RuntimeError(
        "the access token carries no dhanClientId claim and no fallback is "
        f"set - add client_id to {TOKEN_PARAMETER_NAME} or set DHAN_CLIENT_ID. "
        f"Claims present: {sorted(claims)}"
    )


def read_telegram_config():
    config = json.loads(get_parameter(TELEGRAM_PARAMETER_NAME))
    token, chat_id = config.get("bot_token"), config.get("chat_id")
    if not token or not chat_id:
        raise RuntimeError(f"{TELEGRAM_PARAMETER_NAME} must hold bot_token and chat_id")
    return token, chat_id
