"""
The regime gate and the dispatch.

Two jobs, and the split between them is the whole point of this function:

  shortlist()  decides WHICH playbooks are even eligible, from the regime
               alone. A playbook fired in the wrong regime is the main way
               this loses money, so that decision is a lookup in a table
               rather than a judgement inside a strategy that wants to trade.

  dispatch()   invokes them, asynchronously, each with the full context.

THE GATE HERE IS COARSE, AND DELIBERATELY SO. It answers "is this playbook
valid for this kind of day at all". The fine gate - VIX behaviour, how much of
the day's range is already spent, opening-range width, time of day - belongs
inside each strategy, because only the strategy knows what its own playbook
requires, and because a strategy that trusts an upstream gate it cannot see is
a strategy that fires on a bad day when the upstream changes.

So a strategy gating again on arrival is not redundancy to be cleaned up. Both
gates are load-bearing.
"""

import json
import logging

import boto3

from config import (
    CONTEXT_VERSION,
    INVOCATION_TYPE,
    MAX_PAYLOAD_BYTES,
    STRATEGY_REGISTRY,
)

logger = logging.getLogger()


def shortlist(regime):
    """
    The strategy functions valid for `regime`.

    An UNKNOWN regime raises rather than returning nothing. A regime this
    function has never heard of means classify.py and this map have drifted
    apart, and the symptom of returning [] would be a session that routes
    nothing and looks merely quiet - indistinguishable from a correct
    stand-down. Those two must not look the same.

    A regime that is present and maps to [] is a different thing entirely:
    that is a deliberate "no playbook is valid on this kind of day", which is
    the correct answer on a trending day for every playbook here so far.
    """
    if regime not in STRATEGY_REGISTRY:
        raise RuntimeError(
            f"regime {regime!r} is not in STRATEGY_REGISTRY "
            f"({sorted(STRATEGY_REGISTRY)}) - classify.py and the registry "
            f"have drifted apart, and routing nothing would look like a quiet "
            f"session rather than a misconfiguration"
        )
    functions = list(STRATEGY_REGISTRY[regime])
    if not functions:
        logger.info("regime %s: no playbook is valid on this kind of day", regime)
    else:
        logger.info("regime %s: eligible %s", regime, ", ".join(functions))
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
            f"limit - it carries "
            f"{len(context.get('candles', {}).get('bars', []))} candle bars; "
            f"lower HISTORY_5MIN_BARS or stop passing the series"
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
