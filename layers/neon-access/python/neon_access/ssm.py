"""
SSM Parameter Store reads.

Nothing in this repo holds a credential in a function environment variable if
it can be avoided: env vars are encrypted at rest but readable by anyone with
lambda:GetFunctionConfiguration, and rotating one means editing every function
that carries it.

Only the Neon connection string is here, because it is the one parameter more
than one function reads. Function-specific parameters - the Dhan token, the
Telegram config - stay in the function that owns them.

Note this module is `ssm`, not `secrets`. A module named secrets.py shadows the
stdlib module of that name, and in a Lambda package the zip root is first on
sys.path, so it shadows it for boto3 too.
"""

import logging
import os

import boto3

logger = logging.getLogger()

NEON_PARAMETER_NAME = os.environ.get("NEON_PARAMETER_NAME", "/algo/neon/connection")


def get_parameter(name):
    ssm = boto3.client("ssm")
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def read_neon_connection_string():
    """
    SSM is the source of truth. NEON_CONNECTION_STRING still wins when set,
    which keeps local testing working and leaves an override in place if the
    parameter ever has to be bypassed. The source is logged either way so a
    stale env var cannot quietly shadow a rotated parameter.
    """
    override = os.environ.get("NEON_CONNECTION_STRING")
    if override:
        logger.info("neon connection string: environment override")
        return override
    logger.info("neon connection string: %s", NEON_PARAMETER_NAME)
    return get_parameter(NEON_PARAMETER_NAME)
