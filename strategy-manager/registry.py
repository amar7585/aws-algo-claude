"""
The trend-day shortlist and the dispatch.

  trend_shortlist()  the playbooks for an alert on a TREND day (day_trend up/down):
                     TREND_PLAYBOOKS.
  dispatch()         invokes them, asynchronously, each with the full context.

Until 2026-11-19 a regime|bias registry (shortlist / routing_key) also routed
alerts on no-trend days. Its only filled cell sent them to the sweep, which runs
on every tick anyway and was always left out, so it dispatched nothing; it was
removed with bias (Amar, 2026-11-19).

THE GATE HERE IS COARSE, AND DELIBERATELY SO. The fine gate - VIX behaviour,
levels, time of day - belongs inside each strategy, because only the strategy
knows what its own playbook requires. A strategy gating again on arrival is not
redundancy to be cleaned up.
"""

import json
import logging

import boto3

from config import (
    CONTEXT_VERSION,
    INVOCATION_TYPE,
    MAX_PAYLOAD_BYTES,
    TREND_PLAYBOOKS,
)

logger = logging.getLogger()

TREND_STATES = ("up", "down")


def trend_shortlist(day_trend):
    """
    The playbooks for a v2 alert on a TREND day (day_trend up/down).

    See config.TREND_PLAYBOOKS. Empty (unset)
    is a deliberate stand-down, logged as such, not a miss.
    """
    if day_trend not in TREND_STATES:
        raise RuntimeError(f"trend_shortlist called with day_trend {day_trend!r}")
    functions = list(TREND_PLAYBOOKS)
    if not functions:
        logger.info("day trend %s: no TREND_PLAYBOOKS configured - nothing routed", day_trend)
    else:
        logger.info("day trend %s: eligible %s", day_trend, ", ".join(functions))
    return functions


def dispatch(context, functions, client=None):
    """
    Invoke each shortlisted strategy with `context`.

    ASYNCHRONOUS. This function returning is not a claim that any strategy
    succeeded - each has its own log group and error-notifier reports its
    failures directly from there. A synchronous invoke would fold every
    strategy's runtime into this function's timeout and let one slow playbook
    delay the rest.

    EVERY INVOKE IS ATTEMPTED BEFORE ANYTHING RAISES. Raising on the first
    failure would hide the state of the others: with two eligible strategies
    and a typo in the first function name, the second would never be called
    and the log would name only the typo. Errors are collected and raised
    together at the end, so a run reports everything that went wrong once.
    """
    if not functions:
        return []

    client = client or boto3.client("lambda")
    payload = json.dumps(context).encode()

    # Checked, not trusted. Lambda caps an asynchronous payload at 256 KB and
    # answers an oversized one with an error that names bytes and not the
    # reason. The dominant term is the candle history, so the message names it.
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise RuntimeError(
            f"context is {len(payload)} bytes against a {MAX_PAYLOAD_BYTES} "
            f"limit - it carries the snapshot row "
            f"({len(context.get('snapshot') or ())} keys) and the daily row "
            f"({len(context.get('daily') or ())} keys)"
        )

    dispatched, failures = [], []
    for name in functions:
        try:
            response = client.invoke(
                FunctionName=name,
                InvocationType=INVOCATION_TYPE,
                Payload=payload,
            )
            status = response.get("StatusCode")
            # 202 Accepted is the success code for an Event invoke; 200 is the
            # synchronous one. Anything else is a refusal that did not raise.
            if status not in (200, 202):
                failures.append(f"{name} -> StatusCode {status}")
                continue
            # An Event invoke that Lambda accepted can still carry a
            # FunctionError when INVOCATION_TYPE has been overridden to
            # RequestResponse. Surfacing it costs nothing and hides nothing.
            if response.get("FunctionError"):
                failures.append(f"{name} -> {response['FunctionError']}")
                continue
            dispatched.append(name)
            logger.info(
                "invoked %s (%s, context v%d, %d bytes)",
                name, INVOCATION_TYPE, CONTEXT_VERSION, len(payload),
            )
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below
            failures.append(f"{name} -> {type(exc).__name__}: {exc}")

    if failures:
        raise RuntimeError(
            f"{len(failures)} of {len(functions)} strategy invocations failed: "
            + "; ".join(failures)
        )
    return dispatched
