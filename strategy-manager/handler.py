"""
Strategy Manager - AWS Lambda function

Decides which playbooks are valid for the market as it stands, and invokes
them. It evaluates no playbook itself and emits no signal.

A PURE ROUTER. It opens no database connection, makes no API call, computes no
indicator and writes no row. Everything it decides on arrives in the payload.
That is a deliberate narrowing from the previous version, which classified the
15-minute frame itself and fetched candles on the playbooks' behalf:

  * THE CLASSIFICATION MOVED UP. intraday-market-sentiment now runs the shared
    market-classifier layer and STORES the result on
    algo.intraday_market_sentiment. The manager used to recompute its own
    classification, so the rules lived in two places and the regime a strategy
    acted on was never persisted anywhere - it existed only in a log line.
  * THE DATA FETCH MOVED DOWN. Each playbook fetches the bars it needs. The
    manager was fetching a fixed window on their behalf and guessing at it.

NOT ON A CRON. intraday-market-sentiment invokes this function asynchronously
at the end of its own run, handing over the snapshot it has just written AND
the daily read it looked up. The trigger IS snapshot completion, which is why
there is no schedule here, no staleness window to configure and no race with
the writer - the snapshot cannot be missing or stale, because its arrival is
what started this run.

    intraday-data-loader --(Event)--> intraday-market-sentiment
    intraday-market-sentiment --(Event, both sentiments)--> this
    this --(Event, the context)--> strategy-range-liquidity-sweep, ...

WHAT IT WRITES. Nothing. The regime it routed on is already stored on the
snapshot row by the function that computed it, so there is nothing here worth
duplicating into a second table.

WHAT IT DOES NOT DO. It does not place orders, hold position state, size
anything, or decide whether a setup is worth taking. The regime-and-bias gate
is the whole of its judgement.

Modules:
    config.py    the payload contract and the registry
    registry.py  the routing key, the gate and the asynchronous dispatch
"""

import logging
import time

from neon_access import ist_datetime, now_epoch

from config import CONTEXT_VERSION
from registry import dispatch, routing_key, shortlist

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The snapshot keys this function itself reads. Everything else on the row is
# carried through untouched for the playbooks.
#
# CHECKED, NOT ASSUMED. A snapshot arriving without its classification means
# intraday-market-sentiment wrote a row the old way - before the classifier
# was wired in - and routing on a missing regime would resolve to the key
# "None|None", miss the registry, and raise a confusing drift error instead of
# naming the real problem.
REQUIRED_SNAPSHOT_KEYS = (
    "snapshot_ts",
    "security_id",
    "instrument_type",
    "regime",
    "bias",
)


def read_event(event):
    """
    Validate the sentiment function's payload and return its parts.

    The daily read is allowed to be absent. daily-market-sentiment runs at
    09:50 and can fail, and a playbook that needs the previous day's levels
    should say so itself rather than have the router refuse to route. What is
    NOT allowed is a daily row that is silently stale - the sentiment function
    marks that with `stale`, and it is logged here so the decision is visible
    in the routing log as well as in the writer's.
    """
    if not isinstance(event, dict):
        raise RuntimeError(
            f"expected intraday-market-sentiment's payload object, got "
            f"{type(event).__name__}"
        )
    snapshot = event.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"payload carries no `snapshot` object - keys present: "
            f"{sorted(event)}"
        )
    missing = [k for k in REQUIRED_SNAPSHOT_KEYS if snapshot.get(k) is None]
    if missing:
        raise RuntimeError(
            f"snapshot is missing {missing} - it cannot be routed. The "
            f"classification columns are written by intraday-market-sentiment "
            f"via the market-classifier layer; a row without them predates "
            f"that wiring. Keys present: {sorted(snapshot)}"
        )

    instrument = event.get("instrument")
    if not isinstance(instrument, dict):
        raise RuntimeError("payload carries no `instrument` object")

    daily = event.get("daily")
    if daily is None:
        logger.warning(
            "no daily read in the payload - daily-market-sentiment has not "
            "produced a row for this session. Playbooks that need the previous "
            "day's levels will stand down on their own gate."
        )
    elif daily.get("stale"):
        logger.warning(
            "the daily read is STALE - trade_date %s, written %s, before "
            "today. This morning's 09:50 run did not land.",
            daily.get("trade_date"), daily.get("created_at"),
        )
    return snapshot, instrument, daily


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    snapshot, instrument, daily = read_event(event)

    snapshot_ts = int(snapshot["snapshot_ts"])
    regime, bias = snapshot["regime"], snapshot["bias"]
    logger.info(
        "snapshot %s: %s, structure %s, volatility %s, score %s/%s, "
        "confidence %s",
        ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        routing_key(regime, bias),
        snapshot.get("structure"), snapshot.get("volatility"),
        snapshot.get("score"), snapshot.get("max_score"),
        snapshot.get("confidence"),
    )

    # The whole contract. Two rows and an instrument - the snapshot carries its
    # own classification, so there is no separate `classification` block, and
    # no candles: playbooks fetch their own.
    dispatch_context = {
        "context_version": CONTEXT_VERSION,
        "dispatched_at": now_epoch(),
        "instrument": instrument,
        "snapshot": snapshot,
        "daily": daily,
    }

    eligible = shortlist(regime, bias)
    dispatched = dispatch(dispatch_context, eligible)

    elapsed = time.monotonic() - started
    logger.info(
        "done in %.2fs: %s, %d eligible, %d invoked",
        elapsed, routing_key(regime, bias), len(eligible), len(dispatched),
    )
    return {
        "status": "success",
        "snapshot": ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        "instrument": instrument.get("trading_symbol"),
        "routing_key": routing_key(regime, bias),
        "regime": regime,
        "bias": bias,
        "structure": snapshot.get("structure"),
        "volatility": snapshot.get("volatility"),
        "score": snapshot.get("score"),
        "confidence": snapshot.get("confidence"),
        "daily_read": None if not daily else {
            "trade_date": daily.get("trade_date"),
            "regime": daily.get("regime"),
            "bias": daily.get("bias"),
            "stale": daily.get("stale"),
        },
        "eligible": eligible,
        "dispatched": dispatched,
        "context_version": CONTEXT_VERSION,
        "elapsed_seconds": round(elapsed, 2),
    }
