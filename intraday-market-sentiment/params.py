"""
The parameters only this function reads.

The Neon connection string is shared and lives in neon_access.ssm.

Same shape as intraday-data-loader's params.py, and deliberately a copy rather
than a shared module: it would have to live in the neon-access layer to be
shared, and that layer is a Lambda layer where every change means republishing
AND repointing every function's pinned ARN. Twenty lines of duplication is
cheaper than that coupling.
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
    reads 5.5 hours late.
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
    The Dhan client id, for the option-chain calls.

    The charts endpoints need only access-token. The option-chain family -
    both expirylist and the chain itself - also wants a client-id header.

    IT COMES FROM THE TOKEN ITSELF. The access token is a JWT whose payload
    carries `dhanClientId` alongside the `exp` claim, so the id is by
    construction the client the token was minted for and cannot drift out of
    step with a rotated token. The two fallbacks exist only for a token that
    somehow lacks the claim, and the source is always logged.

    UNLIKE intraday-data-loader, this is resolved BEFORE any fetch. That
    function reads the id late so a missing one cannot abort a run whose index
    candles are already worth committing; here every Dhan call feeds a single
    row that is written once at the end, so there is nothing to protect by
    deferring - failing early is the clearer behaviour.
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
