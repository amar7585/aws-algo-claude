"""
Intraday Data Loader - AWS Lambda function

Keeps algo.candle_5min, algo.candle_15min and algo.candle_1hr current for
NIFTY and the current-month NIFTY future.

Runs every 5 minutes from 10:00 to 15:30 IST on weekdays, plus one closing
sweep at 15:35 - 68 invocations a trading day. Which intervals a given run
fetches is decided by the clock:

    fetch interval I when (run time - 09:15) is a whole multiple of I minutes

so 10:05 fetches 5-minute only, 10:30 fetches 5 and 15, and 10:15 fetches all
three. That single rule reproduces the session-aligned bucket boundaries
rather than restating them as a table of times that can drift out of step with
the data. The 15:35 sweep is the one explicit exception: it fetches everything
so that the last bars of the session are finalised.

PARTIAL CANDLES ARE STORED DELIBERATELY. Dhan returns the in-progress bucket,
and this function writes it. A bar is therefore correct-so-far rather than
final, and is corrected by the primary-key upsert on a later pass. Only the
newest bar per table is ever partial, so historical reads stay replayable.
Consumers distinguish the two with no extra column:

    a bar is closed  iff  candle_ts + interval_seconds <= now

Writes only to Neon project "AI Trader APP" (nameless-mountain-15353651),
database Algo, schema algo.

Modules:
    config.py   this function's tunables
    params.py   the Dhan token and client id (named params, not secrets -
                secrets.py at the zip root shadows the stdlib one)
    dhan.py     charts + expiry client, and the measured facts about them
    db.py       Neon access and the upsert

Epoch/IST handling, the Neon connection and the shared connection-string read
come from the neon-access layer.
"""

import datetime
import logging
import time

from neon_access import IST, connect, ist_datetime, read_neon_connection_string

from config import (
    CLOSING_SWEEP,
    COLD_START_DAYS,
    FUTURES_INSTRUMENT_TYPE,
    FUTURES_SYMBOL_TEMPLATE,
    FUTURES_UNDERLYING_SCRIP,
    FUTURES_UNDERLYING_SEG,
    INTERVAL_TABLES,
    MAX_WINDOW_DAYS,
    MONTH_ABBREVIATIONS,
    NIFTY_INSTRUMENT_TYPE,
    NIFTY_SECURITY_ID,
    SESSION_START,
)
from db import latest_candle_ts, resolve_by_symbol, resolve_instrument, upsert_candles
from dhan import DhanClient, session_candles, to_candles
from params import read_client_id, read_token_record

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Resolved once per IST date and reused across warm invocations, so the expiry
# list is not re-fetched 68 times a day. A cold container simply resolves again.
_future_cache = {}


def _minutes(t):
    return t.hour * 60 + t.minute


def intervals_due(run_time):
    """
    Which intervals this run should fetch. See the module docstring.

    Off-grid runs - a manual invocation at 10:07, say - fall back to the
    smallest interval rather than doing nothing, so an ad-hoc call is still
    useful.
    """
    if run_time >= CLOSING_SWEEP:
        return sorted(INTERVAL_TABLES)
    offset = _minutes(run_time) - _minutes(SESSION_START)
    if offset < 0:
        return []
    due = sorted(i for i in INTERVAL_TABLES if offset % i == 0)
    return due or [min(INTERVAL_TABLES)]


def fetch_window(last_ts, interval, now):
    """
    The window to ask Dhan for.

    Two things decide the start, and both matter:

    * fromDate is EXCLUSIVE, so resuming from the newest stored candle_ts
      returns only genuinely new bars - no duplicate, no gap. But the newest
      stored bar MAY BE PARTIAL, and resuming from its own timestamp would
      never re-fetch it, leaving it partial forever. So the start steps back
      one whole interval, which re-fetches exactly that bar and no more.

    * Dhan caps an intraday window at 90 days per call and answers HTTP 400
      DH-905 past it, so the start is clamped. That also bounds the cold
      start, which is why COLD_START_DAYS cannot usefully exceed 90 without
      chunking the fetch - deliberately not built.
    """
    to_dt = now + datetime.timedelta(minutes=1)  # toDate is non-inclusive
    if last_ts is None:
        from_dt = to_dt - datetime.timedelta(days=COLD_START_DAYS)
    else:
        from_dt = ist_datetime(last_ts) - datetime.timedelta(minutes=interval)
    floor = to_dt - datetime.timedelta(days=MAX_WINDOW_DAYS)
    return max(from_dt, floor), to_dt


def assert_alignment(candles, interval):
    """
    Every bar must start on a session-aligned bucket boundary.

    Dhan's intraday buckets run from 09:15, not from the top of the hour -
    measured, see dhan.py. The hourly trigger times in intervals_due() are
    derived from that, so if Dhan ever changed it the schedule would quietly
    fetch at the wrong moments and leave bars partial. This turns that into a
    loud failure.
    """
    step = interval * 60
    origin = _minutes(SESSION_START) * 60
    for candle in candles:
        stamp = ist_datetime(candle["ts"])
        offset = (stamp.hour * 3600 + stamp.minute * 60 + stamp.second) - origin
        if offset % step:
            raise RuntimeError(
                f"{interval}-minute bar stamped {stamp:%Y-%m-%d %H:%M:%S} is not "
                f"on a bucket boundary measured from {SESSION_START:%H:%M} - "
                f"Dhan's interval alignment has changed and the schedule no "
                f"longer matches the data"
            )


def current_future_symbol(client, today):
    """
    The trading symbol of the current-month NIFTY future, derived from the
    live expiry list - nothing about the contract is hardcoded.

    The nearest option expiry's MONTH is the current futures month, and it
    rolls itself: once September's monthly expiry has passed, the nearest
    expiry is already in October. Measured 2026-09-11.
    """
    if _future_cache.get("date") == today:
        return _future_cache["symbol"]

    raw = client.expiry_list(FUTURES_UNDERLYING_SCRIP, FUTURES_UNDERLYING_SEG)
    upcoming = sorted(
        d for d in (datetime.date.fromisoformat(str(x)[:10]) for x in raw) if d >= today
    )
    if not upcoming:
        raise RuntimeError(
            f"every expiry Dhan returned is before {today}: {raw!r} - cannot "
            f"resolve the current-month future"
        )
    nearest = upcoming[0]
    symbol = FUTURES_SYMBOL_TEMPLATE.format(
        month=MONTH_ABBREVIATIONS[nearest.month - 1], year=nearest.year
    )
    logger.info("nearest expiry %s -> current future %s", nearest, symbol)
    _future_cache.update(date=today, symbol=symbol)
    return symbol


def refresh(conn, client, instrument, interval, now):
    """Fetch and store one instrument at one interval."""
    table = INTERVAL_TABLES[interval]
    last_ts = latest_candle_ts(conn, instrument, table)
    from_dt, to_dt = fetch_window(last_ts, interval, now)
    mode = (
        f"resume from {from_dt:%Y-%m-%d %H:%M}"
        if last_ts
        else f"cold start {COLD_START_DAYS}d"
    )

    candles = session_candles(
        to_candles(client.intraday_candles(instrument, interval, from_dt, to_dt))
    )
    assert_alignment(candles, interval)
    written = upsert_candles(conn, instrument, table, candles)
    logger.info(
        "%s %sm: %s -> %d candles, %d written",
        instrument["trading_symbol"], interval, mode, len(candles), written,
    )
    return written


def lambda_handler(event, context):
    event = event or {}
    now = (
        datetime.datetime.fromisoformat(event["now"]).replace(tzinfo=IST)
        if event.get("now")
        else datetime.datetime.now(IST)
    )
    today = now.date()

    # Weekend gate. A holiday needs none: the fetch returns nothing new and
    # nothing is written.
    if today.weekday() >= 5:
        logger.info("%s is a %s - not a trading day", today, today.strftime("%A"))
        return {"status": "skipped_non_trading_day", "date": today.isoformat()}

    intervals = event.get("intervals") or intervals_due(now.time())
    if not intervals:
        logger.info("%s is before the session - nothing due", now.strftime("%H:%M"))
        return {"status": "skipped_outside_session", "time": now.strftime("%H:%M")}

    started = time.monotonic()
    record = read_token_record()
    client = DhanClient(record["access_token"])
    conn = connect(read_neon_connection_string())
    written = {}
    try:
        # The index is resolved and stored FIRST, and each interval commits as
        # it goes. The future needs an extra API call to resolve, and if that
        # call fails the exception still propagates - but the index candles
        # are already committed rather than lost alongside it.
        nifty = resolve_instrument(conn, NIFTY_SECURITY_ID, NIFTY_INSTRUMENT_TYPE)
        for interval in intervals:
            written[f"{nifty['trading_symbol']}/{interval}m"] = refresh(
                conn, client, nifty, interval, now
            )

        # Only the expiry call needs the client id, and the index candles are
        # committed by now - so a missing one raises here rather than before
        # anything was written.
        client.use_client_id(read_client_id(record))
        future = resolve_by_symbol(
            conn, current_future_symbol(client, today), FUTURES_INSTRUMENT_TYPE
        )
        for interval in intervals:
            written[f"{future['trading_symbol']}/{interval}m"] = refresh(
                conn, client, future, interval, now
            )

        elapsed = time.monotonic() - started
        logger.info(
            "done in %.1fs: intervals %s, %d rows",
            elapsed, intervals, sum(written.values()),
        )
        return {
            "status": "success",
            "date": today.isoformat(),
            "time": now.strftime("%H:%M"),
            "intervals": intervals,
            "rows_written": sum(written.values()),
            "detail": written,
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass
