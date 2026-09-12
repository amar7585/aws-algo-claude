"""
Strategy Orchestrator - AWS Lambda function

Decides which playbooks are valid for the market as it stands, and invokes
them. It evaluates no playbook itself and emits no signal.

NOT ON A CRON. intraday-market-sentiment invokes this function asynchronously
at the end of its own run, handing over the snapshot it has just written. The
trigger IS snapshot completion, which is why there is no schedule here, no
staleness window to configure and no race with the writer - the snapshot
cannot be missing or stale, because its arrival is what started this run.

    intraday-market-sentiment  --(Event, the snapshot)-->  this
    this  --(Event, the context)-->  strategy-range-liquidity-sweep, ...

WHAT IT READS. The snapshot arrives in the payload. Everything else comes from
Neon - the daily read, and the candle history the classification needs - plus
one live call to Dhan for the current price, which is the only thing neither
the snapshot nor a closed bar can give: both are up to five minutes old by
construction, and the routing question is what price is doing now.

Reading candle_15min and candle_5min is a DELIBERATE DEPARTURE from
intraday-market-sentiment, which fetches everything itself so a stalled loader
cannot feed it stale inputs. That rule does not transfer: a snapshot row is
never revisited, so a stale input there is wrong forever, while this function
persists nothing and a stale SMA costs one routing decision that the next run
corrects. The freshness assertion below turns a genuinely stalled loader into
a raise rather than a quietly wrong regime.

WHAT IT WRITES. Nothing. Not a row, not a signal, not a routing log. The
regime it decided on travels in the dispatched payload and each strategy
records it alongside whatever it found, so the decision is recoverable from
the consumer rather than duplicated by the producer.

WHAT IT DOES NOT DO. It does not place orders, hold position state, size
anything, or decide whether a setup is worth taking. The coarse regime gate is
the whole of its judgement.

Modules:
    config.py      this function's tunables, and the registry
    params.py      the Dhan token
    dhan.py        the live price, and why a partial bar is wanted here
    db.py          the Neon reads - there are no writes
    indicators.py  SMA/RSI/ATR/VWAP, pure Python
    classify.py    bias, regime, score, confidence on the 15-minute frame
    registry.py    the regime gate and the asynchronous dispatch

Epoch/IST handling, the Neon connection and the shared connection-string read
come from the neon-access layer.
"""

import logging
import time

from neon_access import (
    connect,
    ist_datetime,
    ist_midnight_epoch,
    now_epoch,
    read_neon_connection_string,
)

import indicators
from classify import classify
from config import (
    ATR_PERIOD,
    CANDLE_LAG_TOLERANCE_BARS,
    CLASSIFY_INTERVAL_MINUTES,
    CONTEXT_VERSION,
    HISTORY_5MIN_BARS,
    HISTORY_15MIN_BARS,
    HISTORY_DAILY_BARS,
    NIFTY_INSTRUMENT_TYPE,
    NIFTY_SECURITY_ID,
    RSI_PERIOD,
    SNAPSHOT_INTERVAL_MINUTES,
    SNAPSHOT_INTERVAL_SECONDS,
    VOLUME_AVERAGE_BARS,
)
from db import (
    closed_candles,
    daily_candles,
    daily_sentiment,
    resolve_instrument,
)
from dhan import DhanClient, live_price, session_candles, to_candles
from params import read_token_record
from registry import dispatch, shortlist

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def read_snapshot(event):
    """
    Pull the snapshot out of the invocation payload, or fail saying what came.

    The payload is intraday-market-sentiment's row. Only three fields are
    required here - the snapshot timestamp and the instrument identity - and
    the rest is carried through to the strategies untouched, so a column added
    upstream needs no change in this function.

    security_id AND instrument_type are both required. security_id alone is
    not an identity: `13` is both NIFTY (IDX_I/INDEX) and ABB (NSE_EQ/EQUITY),
    and defaulting the type would resolve to whichever the database returned
    first.
    """
    if not isinstance(event, dict):
        raise RuntimeError(f"expected a JSON object payload, got {type(event).__name__}")

    snapshot = event.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            "payload carries no `snapshot` object - this function is invoked by "
            f"intraday-market-sentiment with the row it just wrote. Keys present: "
            f"{sorted(event)}"
        )

    missing = [
        field
        for field in ("snapshot_ts", "security_id", "instrument_type")
        if snapshot.get(field) in (None, "")
    ]
    if missing:
        raise RuntimeError(
            f"snapshot is missing {', '.join(missing)} - cannot resolve what "
            f"moment or which instrument this run describes. Keys present: "
            f"{sorted(snapshot)}"
        )
    return snapshot


def assert_candles_fresh(candles, snapshot_ts, interval_minutes):
    """
    Refuse to route on a stalled loader.

    The snapshot names the last closed 5-minute bar at the moment
    intraday-market-sentiment ran, so candle_5min should already carry it.

    ONE BAR OF TOLERANCE, BECAUSE OF A RACE. intraday-data-loader fires every
    five minutes and intraday-market-sentiment at :35/:50/:05/:20, so the
    loader's write of that bar and this function's read of it land in the same
    minute. Demanding equality would raise on the ordinary case.

    Beyond one bar it is not a race. It is a loader that has stopped, and the
    SMAs would be computed from a series that ends before the market does -
    which produces a plausible regime rather than an error, which is exactly
    the failure this repo does not allow to pass quietly.
    """
    if not candles:
        raise RuntimeError(
            f"algo.candle_{interval_minutes}min returned no closed bars at or "
            f"before {ist_datetime(snapshot_ts).strftime('%Y-%m-%d %H:%M')} - "
            f"intraday-data-loader has written nothing for this instrument"
        )
    newest = candles[-1]["ts"]
    lag_bars = (snapshot_ts - newest) / (interval_minutes * 60)
    if lag_bars > CANDLE_LAG_TOLERANCE_BARS:
        raise RuntimeError(
            f"algo.candle_{interval_minutes}min is {lag_bars:.0f} bars behind "
            f"the snapshot: newest closed bar "
            f"{ist_datetime(newest).strftime('%H:%M')} against snapshot "
            f"{ist_datetime(snapshot_ts).strftime('%H:%M')}. Tolerance is "
            f"{CANDLE_LAG_TOLERANCE_BARS} bar for the loader/sentiment race; "
            f"more than that is a stalled intraday-data-loader"
        )
    if lag_bars > 0:
        logger.info(
            "candle_%dmin is %.0f bar behind the snapshot (the loader race) - "
            "the strategies are told which bar was actually scanned",
            interval_minutes, lag_bars,
        )
    return newest


def daily_atr(candles, period=None):
    """
    Wilder ATR14 on daily bars, or None when there is not enough history.

    None rather than raising, because this is a gate INPUT rather than a
    classification input: the sweep playbook compares the day's range so far
    against it, and a strategy that cannot compute that comparison should
    stand down and say so rather than have this function fail the whole
    dispatch. The classification, which every strategy depends on, is the part
    that raises.

    TRUE range, so it includes the overnight gap. The playbook's own threshold
    was calibrated against a gap-free 10-day mean of high-low and is re-scaled
    for this definition in the strategy's config, where the comparison happens.
    """
    period = ATR_PERIOD if period is None else period
    if len(candles) < period:
        logger.warning(
            "%d daily bars is fewer than the %d Wilder ATR needs - the range "
            "gate will have no atr14 to compare against",
            len(candles), period,
        )
        return None
    series = indicators.wilder_atr(candles, period=period)
    return series[-1]


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    run_epoch = now_epoch()

    snapshot = read_snapshot(event)
    snapshot_ts = int(snapshot["snapshot_ts"])

    # Every read is bounded by the instant the snapshot's own bar closed,
    # never by the wall clock. Two runs over the same snapshot then see the
    # same bars and reach the same decision, which is what makes a re-run of
    # this function meaningful rather than merely repeated.
    as_of = snapshot_ts + SNAPSHOT_INTERVAL_SECONDS
    session_day = ist_datetime(snapshot_ts).date()
    trade_date = ist_midnight_epoch(session_day)

    logger.info(
        "snapshot %s (%s), as_of %s",
        ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        snapshot.get("instrument_type"),
        ist_datetime(as_of).strftime("%H:%M"),
    )

    token = read_token_record()["access_token"]
    conn = connect(read_neon_connection_string())
    try:
        instrument = resolve_instrument(
            conn,
            snapshot.get("security_id", NIFTY_SECURITY_ID),
            snapshot.get("instrument_type", NIFTY_INSTRUMENT_TYPE),
        )

        # ---- the classification frame --------------------------------------
        bars_15min = closed_candles(
            conn, CLASSIFY_INTERVAL_MINUTES, instrument, as_of, HISTORY_15MIN_BARS
        )
        assert_candles_fresh(bars_15min, snapshot_ts, CLASSIFY_INTERVAL_MINUTES)
        indicators.decorate(
            bars_15min,
            rsi_period=RSI_PERIOD,
            atr_period=ATR_PERIOD,
            volume_bars=VOLUME_AVERAGE_BARS,
        )
        classification = classify(bars_15min, CLASSIFY_INTERVAL_MINUTES)

        # ---- the frame the strategies scan ---------------------------------
        # 5-minute bars, because that is the frame the sweep anatomy is
        # defined on - penetration, rejection and the no-extension
        # confirmation are all per 5-minute candle.
        bars_5min = closed_candles(
            conn, SNAPSHOT_INTERVAL_MINUTES, instrument, as_of, HISTORY_5MIN_BARS
        )
        newest_5min = assert_candles_fresh(
            bars_5min, snapshot_ts, SNAPSHOT_INTERVAL_MINUTES
        )

        # ---- the daily context ---------------------------------------------
        daily_row = daily_sentiment(conn, instrument, trade_date)
        atr14 = daily_atr(
            daily_candles(conn, instrument, trade_date, HISTORY_DAILY_BARS)
        )
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass

    # ---- the one live read -------------------------------------------------
    client = DhanClient(token)
    live = live_price(
        session_candles(
            to_candles(
                client.intraday_candles(
                    instrument, SNAPSHOT_INTERVAL_MINUTES, session_day
                )
            ),
            session_day,
        ),
        SNAPSHOT_INTERVAL_SECONDS,
        run_epoch,
    )
    logger.info(
        "live %s at %s (%s)",
        live["price"],
        ist_datetime(live["ts"]).strftime("%H:%M"),
        "still forming" if live["partial"] else "closed",
    )

    # ---- the payload contract ---------------------------------------------
    dispatch_context = {
        "context_version": CONTEXT_VERSION,
        "dispatched_at": now_epoch(),
        "instrument": instrument,
        "snapshot": snapshot,
        "daily": daily_row,
        "daily_atr14": atr14,
        "classification": classification,
        "live": live,
        "candles": {
            "interval_minutes": SNAPSHOT_INTERVAL_MINUTES,
            "as_of": as_of,
            "newest_ts": newest_5min,
            "bars": bars_5min,
        },
    }

    eligible = shortlist(classification["regime"])
    dispatched = dispatch(dispatch_context, eligible)

    elapsed = time.monotonic() - started
    logger.info(
        "done in %.1fs: regime %s, %d eligible, %d invoked",
        elapsed, classification["regime"], len(eligible), len(dispatched),
    )
    return {
        "status": "success",
        "snapshot": ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        "instrument": instrument["trading_symbol"],
        "bias": classification["bias"],
        "regime": classification["regime"],
        "score": classification["score"],
        "confidence": classification["confidence"],
        "daily_atr14": atr14,
        "live_price": live["price"],
        "bars_scanned": len(bars_5min),
        "newest_bar": ist_datetime(newest_5min).strftime("%H:%M"),
        "eligible": eligible,
        "dispatched": dispatched,
        "context_version": CONTEXT_VERSION,
        "elapsed_seconds": round(elapsed, 2),
    }
