"""
Daily Market Sentiment - AWS Lambda function

Runs once each trading morning at 10:00 IST:

  1. daily candles for NIFTY and INDIA VIX  -> algo.candle_daily
  2. the daily sentiment read for NIFTY     -> algo.daily_market_sentiment
  3. pushes the read to Telegram

Three Dhan calls per run. Writes only to Neon project "AI Trader APP"
(nameless-mountain-15353651), database Algo, schema algo.

It does NOT populate candle_5min / candle_15min / candle_1hr; the separate
every-5-minute intraday function owns those. The one intraday call here
supplies today's open and the 09:15-09:30 range for the sentiment row.

Modules:
    config.py      this function's tunables
    params.py      the Dhan token and Telegram config (named params, not
                   secrets - secrets.py at the zip root shadows the stdlib one)
    dhan.py        charts API client, and the measured facts about it
    db.py          Neon access and the upserts
    indicators.py  SMA / RSI / ATR, pure Python
    sentiment.py   regime, score, bias, the daily read
    notify.py      Telegram

Epoch/IST handling, the Neon connection and the shared connection-string read
come from the neon-access layer.
"""

import datetime
import logging
import time

from neon_access import connect, ist_datetime, read_neon_connection_string, today_ist

from config import (
    COLD_START_DAYS,
    NIFTY_INSTRUMENT_TYPE,
    NIFTY_SECURITY_ID,
    SESSION_START,
    VIX_INSTRUMENT_TYPE,
    VIX_SECURITY_ID,
)
from db import (
    latest_candle_ts,
    load_daily_candles,
    resolve_instrument,
    upsert_daily_candles,
    upsert_sentiment,
)
from dhan import DhanClient, session_candles, to_candles
from notify import format_brief, send_telegram
from params import read_access_token
from sentiment import build_daily_sentiment

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def refresh_daily_candles(conn, client, instrument, today):
    """Incremental from the last stored candle; 300-day window on a cold start."""
    last_ts = latest_candle_ts(conn, instrument)
    if last_ts:
        from_date = ist_datetime(last_ts).date()
        mode = f"incremental from {from_date}"
    else:
        from_date = today - datetime.timedelta(days=COLD_START_DAYS)
        mode = f"cold start {COLD_START_DAYS}d from {from_date}"
    candles = to_candles(
        client.daily_candles(instrument, from_date, today + datetime.timedelta(days=1))
    )
    written = upsert_daily_candles(conn, instrument, candles)
    logger.info(
        "%s: %s -> %d fetched, %d written",
        instrument["trading_symbol"], mode, len(candles), written,
    )
    return written


def lambda_handler(event, context):
    event = event or {}
    today = (
        datetime.date.fromisoformat(event["trade_date"])
        if event.get("trade_date")
        else today_ist()
    )

    # Weekend gate. A holiday needs none: the fetch returns nothing new and no
    # sentiment row is written.
    if today.weekday() >= 5:
        logger.info("%s is a %s - not a trading day", today, today.strftime("%A"))
        return {"status": "skipped_non_trading_day", "date": today.isoformat()}

    started = time.monotonic()
    client = DhanClient(read_access_token())
    conn = connect(read_neon_connection_string())
    try:
        # Always resolved on the FULL identity - security_id alone is not
        # unique. See resolve_instrument() in db.py.
        nifty = resolve_instrument(conn, NIFTY_SECURITY_ID, NIFTY_INSTRUMENT_TYPE)
        vix = resolve_instrument(conn, VIX_SECURITY_ID, VIX_INSTRUMENT_TYPE)

        rows = refresh_daily_candles(conn, client, nifty, today)
        rows += refresh_daily_candles(conn, client, vix, today)

        nifty_daily = load_daily_candles(conn, nifty)
        vix_daily = load_daily_candles(conn, vix)
        if len(nifty_daily) < 2:
            raise RuntimeError(
                f"only {len(nifty_daily)} {nifty['trading_symbol']} daily candles "
                f"stored - nothing to compute from"
            )

        # One intraday call: today's open and the 09:15-09:30 range. fromDate
        # is exclusive, so dhan.intraday_candles starts at 00:00.
        intraday = session_candles(
            to_candles(client.intraday_candles(nifty, 15, today)), today
        )
        if intraday:
            opening = intraday[0]
            if ist_datetime(opening["ts"]).time() != SESSION_START:
                raise RuntimeError(
                    f"first 15-min candle is stamped "
                    f"{ist_datetime(opening['ts']):%H:%M}, expected 09:15 - the "
                    f"opening range would be wrong. fromDate is exclusive."
                )
            session_open = opening["open"]
            min15_high, min15_low = opening["high"], opening["low"]
        else:
            logger.info("no intraday candles for %s - market did not open", today)
            session_open = min15_high = min15_low = None

        sentiment = build_daily_sentiment(
            nifty,
            nifty_daily,
            vix_daily[-1]["close"] if vix_daily else None,
            session_open,
            min15_high,
            min15_low,
        )
        upsert_sentiment(conn, sentiment)
        rows += 1

        brief = format_brief(nifty, sentiment)
        if event.get("skip_telegram"):
            logger.info("telegram suppressed by event flag\n%s", brief)
        else:
            send_telegram(brief)

        elapsed = time.monotonic() - started
        logger.info(
            "done in %.1fs: %s, %d rows, %d api calls, regime=%s",
            elapsed, nifty["trading_symbol"], rows, client.calls, sentiment["regime"],
        )
        return {
            "status": "success",
            "date": today.isoformat(),
            "symbol": nifty["trading_symbol"],
            "trade_date": ist_datetime(sentiment["trade_date"]).date().isoformat(),
            "rows_written": rows,
            "api_calls": client.calls,
            "regime": sentiment["regime"],
            "score": sentiment["score"],
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass
