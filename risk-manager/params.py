"""
The Dhan token for risk-manager's quote of the open paper options (2026-11-28) - read from auth-dhan-broker's
/algo/dhan/token, never minted here.

A copy of intraday-fno-poller/params.py (itself copied from intraday-market-sentiment): a copy rather than a shared
module, as in every other function - sharing it would mean a layer republish and repointing every pinned ARN.
"""

import base64
import json
import logging
import os

from neon_access import get_parameter, now_epoch

from config import TOKEN_PARAMETER_NAME

logger = logging.getLogger()


def jwt_claims(access_token):
    """The token's own payload claims (Dhan's expiryTime string is IST with no marker - the JWT is unambiguous)."""
    parts = access_token.split(".")
    if len(parts) != 3:
        raise RuntimeError(f"dhan access token is not a three-part JWT ({len(parts)} segments)")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def read_token_record():
    """Read /algo/dhan/token and refuse to use a dead token."""
    record = json.loads(get_parameter(TOKEN_PARAMETER_NAME))
    token, expires_at = record.get("access_token"), record.get("expires_at")
    if not token or not expires_at:
        raise RuntimeError(f"{TOKEN_PARAMETER_NAME} must hold access_token and expires_at")
    remaining = int(expires_at) - now_epoch()
    if remaining <= 0:
        raise RuntimeError(f"dhan token expired {abs(remaining)}s ago - auth-dhan-broker has not refreshed it")
    return record


def read_client_id(record):
    """The Dhan client id (the option-chain family and the quotes want a client-id header): the JWT's own claim."""
    from_jwt = jwt_claims(record["access_token"]).get("dhanClientId")
    if from_jwt:
        return str(from_jwt)
    if record.get("client_id"):
        return str(record["client_id"])
    if os.environ.get("DHAN_CLIENT_ID"):
        return os.environ["DHAN_CLIENT_ID"]
    raise RuntimeError(f"the access token carries no dhanClientId claim and no fallback is set ({TOKEN_PARAMETER_NAME})")
