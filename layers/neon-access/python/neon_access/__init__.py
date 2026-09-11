"""
neon_access - the plumbing every function in this repo needs.

Deliberately small. Only things used by more than one function live here;
anything specific to a single function stays in that function's package. The
rule matters because this is a Lambda layer: changing it means publishing a new
layer version AND repointing every function's pinned ARN, so churn here is
expensive in a way that churn in a function's own module is not.

What is here, and why it is shared rather than copied:

  clock   epoch and IST handling. Every stored time value in this system is
          epoch seconds, and getting that wrong is silent rather than loud, so
          there is exactly one implementation.
  neon    connecting to Neon with pg8000.
  ssm     reading parameters, including the one connection string all the
          database functions share.
"""

from neon_access.clock import (
    IST,
    IST_OFFSET_SECONDS,
    ist_datetime,
    ist_midnight_epoch,
    now_epoch,
    today_ist,
)
from neon_access.neon import SSL_CONTEXT, connect
from neon_access.ssm import get_parameter, read_neon_connection_string

__all__ = [
    "IST",
    "IST_OFFSET_SECONDS",
    "ist_datetime",
    "ist_midnight_epoch",
    "now_epoch",
    "today_ist",
    "SSL_CONTEXT",
    "connect",
    "get_parameter",
    "read_neon_connection_string",
]
