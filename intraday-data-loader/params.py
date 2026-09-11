"""
The parameters only this function reads.

The Neon connection string is shared and lives in neon_access.ssm.
"""

import base64
import json
import logging
import os

from neon_access import get_parameter, now_epoch

from config import TOKEN_PARAMETER_NAME

logger = logging.getLogger()


def jwt_claims(access_token):
    """
    The token's own payload claims.

    auth-dhan-broker decodes the same JWT to read `exp`, because Dhan's
    `expiryTime` string is IST with no timezone marker and parsing it as UTC
    reads 5.5 hours late. The decode is repeated here rather than shared: it is
    six lines, and neon-access is a Lambda layer where every change means
    republishing and repointing every function's pinned ARN.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        raise RuntimeError(
            f"dhan access token is not a three-part JWT ({len(parts)} segments)"
        )
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


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


def read_client_id(record):
    """
    The Dhan client id, for the expiry-list call.

    The charts endpoints need only access-token - that is how
    daily-market-sentiment runs. The option-chain family, which expirylist
    belongs to, also wants a client-id header.

    IT COMES FROM THE TOKEN ITSELF. The access token is a JWT whose payload
    carries `dhanClientId` alongside the `exp` claim - verified against the
    live parameter on 2026-09-11, claims: dhanClientId, exp, iat, iss,
    partnerId, tokenConsumerType, userRegion.

    That makes the id authoritative rather than configured: it is by
    construction the client the token was minted for, so it cannot drift out
    of step with a rotated token the way a separately-held copy can. It also
    keeps the value out of a second place - architecture.md moved the Neon
    connection string out of per-function environment variables for the same
    reason.

    The two fallbacks exist only for a token that somehow lacks the claim, and
    the source is always logged.
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
