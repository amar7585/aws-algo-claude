"""
The regime gate and the dispatch.

Two jobs, and the split between them is the whole point of this function:

  shortlist()  decides WHICH playbooks are even eligible, from the regime
               and bias the sentiment row already carries. A playbook fired on
               the wrong kind of day is the main way this loses money, so that
               decision is a lookup in a table rather than a judgement inside
               a strategy that wants to trade.

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


def routing_key(regime, bias):
    """The registry key for a market that looks like this."""
    return f"{regime}|{bias}"


def shortlist(regime, bias):
    """
    The strategy functions valid for this regime AND bias.

    ROUTING IS ON THE COMBINATION. "sideways" says the swing read found no
    progression; it does not say whether the market is leaning up, leaning
    down, or genuinely balanced, and a playbook that is right on a balanced
    day can be wrong on a sideways day with a bearish lean.

    AN UNKNOWN COMBINATION RAISES rather than returning nothing. A key this
    function has never heard of means the market-classifier layer and this map
    have drifted apart - a new regime or bias value appeared and nobody told
    the router - and the symptom of returning [] would be a session that
    routes nothing and looks merely quiet, indistinguishable from a correct
    stand-down. Those two must not look the same.

    A key that is PRESENT and maps to [] is a different thing entirely: a
    deliberate "no playbook is valid on this kind of day", which is currently
    the answer for eight of the nine cells.
    """
    key = routing_key(regime, bias)
    if key not in STRATEGY_REGISTRY:
        raise RuntimeError(
            f"routing key {key!r} is not in STRATEGY_REGISTRY "
            f"({sorted(STRATEGY_REGISTRY)}) - the market-classifier layer and "
            f"the registry have drifted apart, and routing nothing would look "
            f"like a quiet session rather than a misconfiguration"
        )
    functions = list(STRATEGY_REGISTRY[key])
    if not functions:
        logger.info("%s: no playbook is valid on this kind of day", key)
    else:
        logger.info("%s: eligible %s", key, ", ".join(functions))
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
