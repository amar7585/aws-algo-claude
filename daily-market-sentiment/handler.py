"""
Daily Market Sentiment - AWS Lambda function

Runs once each trading morning at 09:35 IST:

  1. daily candles for NIFTY and INDIA VIX  -> algo.candle_daily
  2. the daily sentiment read for NIFTY     -> algo.daily_market_sentiment
  3. pushes the read to Telegram
  4. the width profile for today's session  -> algo.intraday_width_profile
     (the normal 5-min candle width at each clock slot over the previous 20
     sessions; intraday-market-sentiment reads it from 09:45 - 2026-11-12)

Four Dhan calls per run, seven with the morning brief (2026-11-22: expiry list,
option chain, VIX 5-min - read, never stored). Writes only to Neon project "AI Trader APP"
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
    sentiment.py   regime, structure, the daily read
    width_profile.py  the normal 5-min width per clock slot, pure
    notify.py      Telegram (and the old 09:35 message)
    brief.py       the morning brief - every line pure (2026-11-22)
    chain.py       option-chain aggregates, a VERBATIM copy of intraday-fno-poller/chain.py

Epoch/IST handling, the Neon connection and the shared connection-string read
come from the neon-access layer.
"""

import datetime
import logging
import time

from neon_access import (
    connect,
    ist_datetime,
    ist_midnight_epoch,
    now_epoch,
    read_neon_connection_string,
    today_ist,
)

import brief as brief_lib
import chain as chain_lib
import evening as evening_lib
import width_profile
from config import (
    ADR_SESSIONS,
    AGGREGATE_STRIKES_PER_SIDE,
    COLD_START_DAYS,
    EVENING_BRIEF,
    MORNING_BRIEF_V2,
    VIX_RANK_SESSIONS,
    NIFTY_INSTRUMENT_TYPE,
    NIFTY_SECURITY_ID,
    PROFILE_INTERVAL_MINUTES,
    PROFILE_LOOKBACK_DAYS,
    PROFILE_MIN_SAMPLES,
    PROFILE_SESSIONS,
    SESSION_START,
    VIX_INSTRUMENT_TYPE,
    VIX_SECURITY_ID,
)
from db import (
    count_day_fno_rows,
    latest_candle_ts,
    load_daily_candles,
    read_day_alerts,
    read_day_bars,
    read_day_positions,
    read_day_sentiments,
    read_day_snapshots,
    read_prev_session_snapshot,
    resolve_instrument,
    upsert_daily_candles,
    upsert_sentiment,
    upsert_width_profile,
)
from dhan import DhanClient, session_candles, to_candles
from notify import format_brief, send_telegram
from params import read_client_id, read_token_record
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


def refresh_width_profile(conn, client, instrument, today):
    """
    Today's width profile: one fetch of the weeks before today, the medians,
    one upsert. Raises on anything short - see width_profile.build().

    Runs LAST in the handler, after the daily row and the brief, so a failure
    here never costs the daily read; it still fails the run, so error-notifier
    reports it before intraday-market-sentiment's first run at 09:45 needs it.
    """
    candles = to_candles(
        client.intraday_candles(
            instrument, PROFILE_INTERVAL_MINUTES, today,
            from_day=today - datetime.timedelta(days=PROFILE_LOOKBACK_DAYS),
        )
    )
    rows, used = width_profile.build(
        candles, today, sessions=PROFILE_SESSIONS, min_samples=PROFILE_MIN_SAMPLES
    )
    written = upsert_width_profile(
        conn, instrument, ist_midnight_epoch(today), rows,
        [ist_midnight_epoch(day) for day in used], now_epoch(),
    )
    by_slot = {ist_datetime(r["slot_ts"]).strftime("%H:%M"): r["normal_width_pct"] for r in rows}
    logger.info(
        "width profile for %s: %d slots from %d sessions %s..%s (%d candles "
        "fetched); normal 09:15 %.3f%%, 12:00 %.3f%%, 15:25 %.3f%%",
        today, written, len(used), used[0], used[-1], len(candles),
        by_slot["09:15"], by_slot["12:00"], by_slot["15:25"],
    )
    return written, used


# ---------------------------------------------------------------------------
# The morning brief (Amar, 2026-11-22) - see brief.py
# ---------------------------------------------------------------------------
def _guard(fetch, *args):
    """A brief input, or the exception that stopped it - brief.py turns that
    into a "missing" line. Never raises: an input must not cost the brief."""
    try:
        return fetch(*args)
    except Exception as exc:  # noqa: BLE001 - reported in the brief and the log
        logger.warning("morning brief input %s failed: %s", getattr(fetch, "__name__", fetch), exc)
        return exc


def fetch_chain(client, nifty, today, fallback_spot):
    """Today's nearest-expiry chain, summarised exactly as the poller does
    (chain.py is its verbatim copy), plus the OI AT each wall."""
    dates = [datetime.date.fromisoformat(d) for d in
             client.expiry_list(int(nifty["security_id"]), nifty["exchange_segment"])]
    upcoming = sorted(d for d in dates if d >= today)
    if not upcoming:
        raise RuntimeError(f"every expiry Dhan returned is before {today}: {dates}")
    expiry = upcoming[0]
    data = client.option_chain(int(nifty["security_id"]), nifty["exchange_segment"], expiry)
    spot = float(data.get("last_price") or fallback_spot or 0) or None
    if not spot:
        raise RuntimeError("no spot to centre the chain on (no last_price, no 15-min close)")
    summary = chain_lib.summarise(data, spot, AGGREGATE_STRIKES_PER_SIDE)
    keys = dict(chain_lib.strikes_sorted(data))

    def oi_at(strike, side):
        leg = (data["oc"].get(keys.get(strike)) or {}).get(side) or {}
        return int(leg["oi"]) if leg.get("oi") is not None else None

    return {"summary": summary, "expiry": expiry, "expiries": upcoming, "spot": spot,
            "call_oi": oi_at(summary["max_oi_call"], chain_lib.CE),
            "put_oi": oi_at(summary["max_oi_put"], chain_lib.PE)}


def fetch_vix_now(client, vix, today):
    """The latest CLOSED INDIA VIX 5-min candle: its close and its close clock."""
    candles = session_candles(to_candles(client.intraday_candles(vix, 5, today)), today)
    closed = [c for c in candles if c["ts"] + 300 <= now_epoch()]
    if not closed:
        raise RuntimeError(f"no closed VIX 5-min candle yet today ({len(candles)} fetched)")
    last = closed[-1]
    return {"value": last["close"], "clock": ist_datetime(last["ts"] + 300).strftime("%H:%M")}


def morning_brief(conn, client, nifty, vix, today, nifty_daily, vix_daily, opening, sentiment):
    """(text, missing) - the live reads, each guarded, then brief.build()."""
    day_start = ist_midnight_epoch(today)
    fallback = opening["close"] if isinstance(opening, dict) else None
    inputs = {
        "today": today,
        "today_start": day_start,
        "clock": ist_datetime(now_epoch()).strftime("%H:%M"),
        "ist_date": lambda ts: ist_datetime(ts).date(),
        # completed sessions only - the daily endpoint lags a session (dhan.py)
        "daily": [b for b in nifty_daily if b["ts"] < day_start],
        "vix_daily": [b for b in vix_daily if b["ts"] < day_start],
        "opening": opening,
        "sentiment": sentiment,
        "chain": _guard(fetch_chain, client, nifty, today, fallback),
        "vix_now": _guard(fetch_vix_now, client, vix, today),
        "prev_snap": _guard(read_prev_session_snapshot, conn, nifty, day_start),
        "adr_sessions": ADR_SESSIONS,
        "vix_rank_sessions": VIX_RANK_SESSIONS,
    }
    return brief_lib.build(inputs)


def report_missing(missing, mode):
    """
    A brief that went out with a gap is LOGGED AT ERROR, not raised.

    error-notifier's subscription filter matches ERROR, so the gap still reaches
    Telegram as an error alert. Raising instead would make Lambda's async
    retries (2, plus the schedule's own 2) re-run the whole function and send
    the brief again - and in shadow mode re-send the OLD 09:35 brief too.
    (Raised with Amar 2026-11-22 before the switch is turned on.)
    """
    if missing:
        logger.error("morning brief (%s) has %d missing section(s): %s", mode, len(missing),
                     "; ".join(f"{name}: {why}" for name, why in missing))


def deliver(event, text):
    if event.get("skip_telegram"):
        logger.info("telegram suppressed by event flag\n%s", text)
    else:
        send_telegram(text)


# ---------------------------------------------------------------------------
# The ~15:45 evening brief (Amar, 2026-11-23, step 2) - see evening.py
# ---------------------------------------------------------------------------
def _eguard(fetch, *args):
    """An evening-brief input, or the exception that stopped it (never raises)."""
    try:
        return fetch(*args)
    except Exception as exc:  # noqa: BLE001 - reported in the brief and the log
        logger.warning("evening brief input %s failed: %s", getattr(fetch, "__name__", fetch), exc)
        return exc


def evening_brief(conn, client, nifty, vix, today):
    """(text, missing, inputs) - every read guarded, then evening.build(). READ ONLY."""
    day_start = ist_midnight_epoch(today)
    done = lambda v: [b for b in v if b["ts"] < day_start] if isinstance(v, list) else v  # noqa: E731
    bars = _eguard(read_day_bars, conn, nifty, day_start)
    fallback = bars[-1]["close"] if isinstance(bars, list) and bars else None
    inputs = {
        "today": today,
        "today_start": day_start,
        "ist_date": lambda ts: ist_datetime(ts).date(),
        "ist_hm": lambda ts: ist_datetime(ts).strftime("%H:%M"),
        # completed sessions only - today's daily candle is not out until overnight (dhan.py)
        "daily": done(_eguard(load_daily_candles, conn, nifty)),
        "vix_daily": done(_eguard(load_daily_candles, conn, vix)),
        "bars": bars,
        "vix_now": _eguard(fetch_vix_now, client, vix, today),
        "sentiments": _eguard(read_day_sentiments, conn, nifty, day_start),
        "snapshots": _eguard(read_day_snapshots, conn, nifty, day_start),
        "fno15_rows": _eguard(count_day_fno_rows, conn, nifty, day_start),
        "chain": _eguard(fetch_chain, client, nifty, today, fallback),
        "alerts": _eguard(read_day_alerts, conn, nifty, day_start),
        "positions": _eguard(read_day_positions, conn, day_start),
        "adr_sessions": ADR_SESSIONS,
    }
    text, missing = evening_lib.build(inputs)
    return text, missing, inputs


def evening_run(event, today):
    """
    The evening schedule's run: READS ONLY (decision E2) - no candle, no daily read,
    no width profile is written. EVENING_BRIEF=on sends; unset logs it (shadow).

    Same failure rules as the morning brief (Amar, 2026-11-22):
    - a brief with a gap goes out with the gap marked and the gap is LOGGED AT
      ERROR (error-notifier reports it), not raised - a raise would make Lambda's
      async retries re-send the brief;
    - a run that fails before its brief still tells the phone, then raises.
    A day with no stored candle and no 15-min row is a holiday: nothing is sent.
    """
    started = time.monotonic()
    mode = "live" if EVENING_BRIEF else "shadow"
    sent, client = False, None
    try:
        record = read_token_record()
        client_id = _eguard(read_client_id, record)
        client = DhanClient(record["access_token"], None if isinstance(client_id, Exception) else client_id)
        conn = connect(read_neon_connection_string())
        try:
            nifty = resolve_instrument(conn, NIFTY_SECURITY_ID, NIFTY_INSTRUMENT_TYPE)
            vix = resolve_instrument(conn, VIX_SECURITY_ID, VIX_INSTRUMENT_TYPE)
            day_start = ist_midnight_epoch(today)
            bars = read_day_bars(conn, nifty, day_start)
            if not bars and not read_day_sentiments(conn, nifty, day_start):
                logger.info("evening brief: no 5-min candle and no 15-min row stored for %s - no session "
                            "(a holiday), nothing sent", today)
                return {"status": "skipped_no_session", "brief": "evening", "date": today.isoformat()}
            try:
                text, missing, _ = evening_brief(conn, client, nifty, vix, today)
            except Exception as exc:  # noqa: BLE001 - a brief bug must not cost the phone its message
                logger.error("evening brief FAILED - a failure line was sent instead: %s", exc, exc_info=True)
                text = (f"NIFTY EVENING BRIEF - {today:%a %d %b %Y}: the brief FAILED ({type(exc).__name__}: "
                        f"{str(exc)[:300]}) - error-notifier has the detail.").encode("ascii", "replace").decode()
                missing = [("BRIEF", f"FAILED: {exc}")]
            if EVENING_BRIEF:
                deliver(event, text)
                sent = True
            else:
                logger.info("EVENING BRIEF (shadow - not sent):\n%s", text)
            if missing:
                logger.error("evening brief (%s) has %d missing section(s): %s", "sent" if sent else "shadow",
                             len(missing), "; ".join(f"{name}: {why}" for name, why in missing))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        if EVENING_BRIEF and not sent:
            try:
                deliver(event, f"NIFTY EVENING BRIEF - {today:%a %d %b %Y}: the run FAILED "
                               f"({type(exc).__name__}: {str(exc)[:300]}) - no brief. "
                               f"error-notifier has the detail.".encode("ascii", "replace").decode())
            except Exception as send_exc:  # noqa: BLE001 - the original error is what matters
                logger.error("could not send the evening failure line: %s", send_exc)
        raise
    elapsed = time.monotonic() - started
    logger.info("evening brief (%s) done in %.1fs: %d api calls, %d missing", mode, elapsed, client.calls, len(missing))
    return {"status": "success", "brief": "evening", "mode": mode, "date": today.isoformat(),
            "missing": len(missing), "api_calls": client.calls, "elapsed_seconds": round(elapsed, 2)}


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

    # The ~15:45 schedule (step 2, 2026-11-23): the evening brief only - it reads,
    # and none of the 09:35 work below runs.
    if event.get("brief") == "evening":
        return evening_run(event, today)
    if event.get("brief") not in (None, "morning"):
        raise ValueError(f"unknown brief {event.get('brief')!r} - expected 'evening' or none")

    started = time.monotonic()
    record = read_token_record()
    # The client id is needed only by the brief's option-chain calls; a missing
    # one must not cost the daily read, so it is resolved under a guard.
    client_id = _guard(read_client_id, record)
    client = DhanClient(record["access_token"], None if isinstance(client_id, Exception) else client_id)
    conn = connect(read_neon_connection_string())
    brief_sent = False
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

        # The whole VIX series, not just its last close: the expansion test
        # needs the previous session's close as a baseline, the same way the
        # intraday frame uses the previous snapshot's VIX.
        sentiment = build_daily_sentiment(
            nifty,
            nifty_daily,
            vix_daily,
            session_open,
            min15_high,
            min15_low,
        )
        upsert_sentiment(conn, sentiment)
        rows += 1

        brief_state = {"mode": "live" if MORNING_BRIEF_V2 else "shadow", "missing": None}
        opening_candle = intraday[0] if intraday else None
        if MORNING_BRIEF_V2:
            try:
                text, missing = morning_brief(conn, client, nifty, vix, today, nifty_daily,
                                              vix_daily, opening_candle, sentiment)
            except Exception as exc:  # noqa: BLE001 - a brief bug must not cost the run
                # ALWAYS SEND: a bug in the new brief falls back to the old message,
                # and the run goes on to the width profile the 09:45 runs need.
                # Raising here would retry into the same bug and never write it.
                logger.error("morning brief FAILED - the old daily-read message was sent instead: %s",
                             exc, exc_info=True)
                text, missing = format_brief(nifty, sentiment), []
                brief_state["missing"] = f"FAILED, old message sent: {exc}"
            deliver(event, text)
            brief_sent = True
            report_missing(missing, "sent")
            if brief_state["missing"] is None:
                brief_state["missing"] = len(missing)
        else:
            deliver(event, format_brief(nifty, sentiment))
            brief_sent = True

        # After the brief, so a failure here never costs the daily read or the brief.
        profile_rows, profile_sessions = refresh_width_profile(conn, client, nifty, today)
        rows += profile_rows

        # SHADOW (MORNING_BRIEF_V2 unset): the new brief is built LAST, after
        # everything the 09:45 chain needs is written, and only logged. Nothing
        # it does can raise into the run - a failure is logged at ERROR, which
        # error-notifier reports, without a retry re-sending the old brief.
        if not MORNING_BRIEF_V2:
            try:
                text, missing = morning_brief(conn, client, nifty, vix, today, nifty_daily,
                                              vix_daily, opening_candle, sentiment)
                logger.info("MORNING BRIEF V2 (shadow - not sent):\n%s", text)
                report_missing(missing, "shadow")
                brief_state["missing"] = len(missing)
            except Exception as exc:  # noqa: BLE001 - see the comment above
                logger.error("morning brief v2 (shadow) FAILED - the old brief was sent: %s",
                             exc, exc_info=True)
                brief_state["missing"] = f"FAILED: {exc}"

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
            "morning_brief": brief_state,
            "width_profile": {
                "slots": profile_rows,
                "sessions": len(profile_sessions),
                "from": profile_sessions[0].isoformat(),
                "to": profile_sessions[-1].isoformat(),
            },
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as exc:
        # ALWAYS SEND (Amar 2026-11-22): with the new brief live, a run that
        # fails before its brief still tells the phone, then raises as before
        # (fail loudly - error-notifier has the detail). A Lambda retry can
        # repeat this line; a retry that succeeds sends the real brief.
        if MORNING_BRIEF_V2 and not brief_sent:
            try:
                deliver(event, f"NIFTY MORNING BRIEF - {today:%a %d %b %Y}: the daily read FAILED "
                               f"({type(exc).__name__}: {str(exc)[:300]}) - no brief. "
                               f"error-notifier has the detail.".encode("ascii", "replace").decode())
            except Exception as send_exc:  # noqa: BLE001 - the original error is what matters
                logger.error("could not send the failure brief: %s", send_exc)
        raise
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass
