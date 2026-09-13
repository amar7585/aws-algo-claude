"""
The parameters only this function reads.

The Neon connection string is shared and lives in neon_access.ssm.

Same shape as intraday-data-loader's and intraday-market-sentiment's
params.py, and deliberately a copy rather than a shared module: it would have
to live in the neon-access layer to be shared, and that layer is a Lambda
layer where every change means republishing AND repointing every function's
pinned ARN. Twenty lines of duplication is cheaper than that coupling.

NO CLIENT ID HERE, unlike intraday-market-sentiment. That function calls the
option-chain family, which wants a client-id header as well as the token. This
one calls only /v2/charts/intraday, which needs the access token alone - so
the id is not read, and a missing dhanClientId claim cannot fail this run.
"""

import base64
import json
import logging

from neon_access import get_parameter, now_epoch

from config import TOKEN_PARAMETER_NAME

logger = logging.getLogger()


def jwt_claims(access_token):
    """
    The token's own payload claims.

    auth-dhan-broker decodes the same JWT to read `exp`, because Dhan's
    `expiryTime` string is IST with no timezone marker and parsing it as UTC
    reads 5.5 hours late - a dead token then looks live and nothing alarms.
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
