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
anything, or decide whether a setup is worth taking. day_trend is the whole of
its judgement (the regime|bias registry was removed with bias, 2026-11-19).

SINCE 2026-11-23 (P2b) THE CALLER IS risk-manager, never pattern-recorder directly:

    pattern-recorder --(Event)--> risk-manager --(Event, + `run`)--> this --(Event, context v4)--> playbooks

risk-manager adds `run` - its decision per playbook (mode entry_exit / exit_only / force_exit, the reason, and
the playbook's open position rows). A payload without `run` v1 is REFUSED, so no playbook can run without the
kill-switch and limit check. A playbook the risk manager gave no run is not invoked; on a tick, an alert
playbook (trend) is invoked when the risk manager lists it - to follow its open positions. Each playbook gets
its own entry of `run` (context v4), one dispatch each. Routing is otherwise unchanged.

SINCE 2026-11-14 pattern-recorder also hands over EVERY tick (source
"pattern-recorder-tick"); those go to TICK_PLAYBOOKS whatever the regime, and
alert routing leaves the TICK_PLAYBOOKS out.

SINCE 2026-11-09 the caller is pattern-recorder (v2), once per v2 alert, and the
payload adds v2's alert (`detection`, carrying day_trend) and today's closed
index candles (`index_bars`). ROUTING: day_trend up/down -> TREND_PLAYBOOKS
(strategy-trend-pullback); day_trend none, or no `detection` -> nothing (the
sweep runs on every tick). Both are carried through to the playbooks.

Modules:
    config.py    the payload contract and the playbook lists
    registry.py  the trend shortlist and the asynchronous dispatch
"""

import logging
import time

from neon_access import ist_datetime, now_epoch

from config import CONTEXT_VERSION, RUN_VERSION, TICK_PLAYBOOKS, TICK_SOURCE, TREND_PLAYBOOKS
from registry import TREND_STATES, dispatch, trend_shortlist

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The snapshot keys this function itself reads. Everything else on the row is
# carried through untouched for the playbooks.
#
# CHECKED, NOT ASSUMED. A snapshot arriving without its classification means
# intraday-market-sentiment wrote a row the old way - before the classifier
# was wired in - and that is named here rather than passed on.
REQUIRED_SNAPSHOT_KEYS = (
    "snapshot_ts",
    "security_id",
    "instrument_type",
    "regime",
)


def read_event(event):
    """
    Validate the sentiment function's payload and return its parts.

    The daily read is allowed to be absent. daily-market-sentiment runs at
    09:35 and can fail, and a playbook that needs the previous day's levels
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
            "today. This morning's 09:35 run did not land.",
            daily.get("trade_date"), daily.get("created_at"),
        )

    # v2's alert (pattern-recorder, 2026-11-09 on). Optional: a payload without
    # it routes on the registry exactly as before. When present it is carried
    # through to the playbooks, with the index candles v2 handed over.
    detection = event.get("detection")
    if detection is not None and not isinstance(detection, dict):
        raise RuntimeError(f"`detection` must be an object, got {type(detection).__name__}")
    index_bars = event.get("index_bars")
    if index_bars is not None and not isinstance(index_bars, list):
        raise RuntimeError(f"`index_bars` must be a list, got {type(index_bars).__name__}")
    tick = event.get("tick")
    if event.get("source") == TICK_SOURCE and not isinstance(tick, dict):
        raise RuntimeError("a tick hand-off must carry its `tick` object")
    # P2b: the risk manager's decision. No `run` = the payload did not come through risk-manager - refused, so
    # no playbook can ever run without the kill-switch and limit check.
    run = event.get("run")
    if not isinstance(run, dict) or run.get("version") != RUN_VERSION or not isinstance(run.get("playbooks"), dict):
        raise RuntimeError(f"payload carries no `run` v{RUN_VERSION} from risk-manager (got {type(run).__name__}"
                           f"{' v' + str(run.get('version')) if isinstance(run, dict) else ''}) - nothing may run "
                           f"without the risk manager's decision")
    for fn, r in run["playbooks"].items():
        if not isinstance(r, dict) or r.get("mode") not in ("entry_exit", "exit_only", "force_exit"):
            raise RuntimeError(f"run for {fn} has no valid mode: {r!r}")
    return snapshot, instrument, daily, detection, index_bars, event.get("source"), tick, run


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    snapshot, instrument, daily, detection, index_bars, source, tick, run = read_event(event)

    snapshot_ts = int(snapshot["snapshot_ts"])
    regime = snapshot["regime"]
    day_trend = (detection or {}).get("day_trend")
    logger.info(
        "snapshot %s: regime %s, structure %s, volatility %s",
        ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        regime, snapshot.get("structure"), snapshot.get("volatility"),
    )

    # The whole contract. Two rows and an instrument - the snapshot carries its
    # own classification, so there is no separate `classification` block.
    # ADDITIVE since 2026-11-09, so context_version stays 2 and the sweep
    # playbook reads it unchanged: v2's alert (`detection`) and the index
    # candles v2 handed over (`index_bars` - the stored candles lag up to 15
    # minutes behind a 5-min alert), present only when v2 sent them.
    dispatch_context = {
        "context_version": CONTEXT_VERSION,
        "dispatched_at": now_epoch(),
        "instrument": instrument,
        "snapshot": snapshot,
        "daily": daily,
    }
    if detection is not None:
        dispatch_context["source"] = source
        dispatch_context["detection"] = detection
    if index_bars is not None:
        dispatch_context["index_bars"] = index_bars

    # ROUTING. An every-tick hand-off (2026-11-14) goes to TICK_PLAYBOOKS
    # whatever the regime - they apply their own entry cell and keep following
    # an open trade. A trend day (v2's day_trend up/down) goes to the trend
    # playbooks - see config.TREND_PLAYBOOKS. Any other alert routes nowhere: the
    # range playbook already runs on every tick. Alert routing leaves the tick
    # playbooks out: the tick already ran them.
    if source == TICK_SOURCE:
        dispatch_context["source"] = source
        dispatch_context["tick"] = tick
        route = "every tick"
        # P2b: the tick playbooks, plus any alert playbook the risk manager asks to follow its positions.
        eligible = [f for f in list(TICK_PLAYBOOKS) + [t for t in TREND_PLAYBOOKS if t not in TICK_PLAYBOOKS]
                    if f in run["playbooks"]]
        logger.info("tick: eligible %s", ", ".join(eligible) or "none")
    else:
        if day_trend in TREND_STATES:
            route = f"day trend {day_trend}"
            eligible = trend_shortlist(day_trend)
        else:
            route = f"no day trend ({regime})"
            eligible = []
            logger.info("%s: nothing routed - the range playbook runs on every tick", route)
        skipped = [f for f in eligible if f in TICK_PLAYBOOKS]
        if skipped:
            logger.info("%s: %s left out - it runs on every tick", route, ", ".join(skipped))
            eligible = [f for f in eligible if f not in TICK_PLAYBOOKS]
        held = [f for f in eligible if f not in run["playbooks"]]
        if held:
            logger.info("%s: %s not invoked - the risk manager gave it no run", route, ", ".join(held))
            eligible = [f for f in eligible if f in run["playbooks"]]
    # P2b: each playbook gets ITS OWN entry of the risk manager's decision as `run`. One dispatch per playbook
    # (their payloads differ); every invoke is still attempted before anything raises.
    dispatched, failures = [], []
    for fn in eligible:
        try:
            dispatched += dispatch(dict(dispatch_context, run=run["playbooks"][fn]), [fn])
        except Exception as exc:  # noqa: BLE001 - collected and re-raised below
            failures.append(f"{fn}: {exc}")
    if failures:
        raise RuntimeError(f"{len(failures)} of {len(eligible)} playbook invocations failed: " + "; ".join(failures))
    logger.info("%s: modes %s", route, {f: run["playbooks"][f]["mode"] for f in eligible})

    elapsed = time.monotonic() - started
    logger.info(
        "done in %.2fs: %s, %d eligible, %d invoked",
        elapsed, route, len(eligible), len(dispatched),
    )
    return {
        "status": "success",
        "snapshot": ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        "instrument": instrument.get("trading_symbol"),
        "route": route,
        "day_trend": day_trend,
        "regime": regime,
        "structure": snapshot.get("structure"),
        "volatility": snapshot.get("volatility"),
        "daily_read": None if not daily else {
            "trade_date": daily.get("trade_date"),
            "regime": daily.get("regime"),
            "stale": daily.get("stale"),
        },
        "eligible": eligible,
        "dispatched": dispatched,
        "context_version": CONTEXT_VERSION,
        "elapsed_seconds": round(elapsed, 2),
    }
