"""
Intraday Market Sentiment - AWS Lambda function

Writes one row to algo.intraday_market_sentiment every fifteen minutes through
the session, plus the ten raw option legs behind it to algo.option_chain_snapshot.

Runs at 09:35, 09:50, 10:05 ... 15:35 IST on weekdays - 25 invocations a
trading day. The five-minute offset is the point: at each of those times a
5-minute bucket has just closed, so the bar the snapshot describes is final
rather than still forming.

EVERYTHING COMES FROM THE DHAN API, WITH ONE EXCEPTION. This function does not
read candle_5min or any other table that intraday-data-loader writes; it
fetches its own candles and its own chains. The exception is its own previous
row, read back to provide the baseline for every *_change_pct - Dhan serves
only a live option chain and has no historical-chain endpoint, so an intraday
OI delta cannot be had any other way.

NOTHING HERE IS EVER PARTIAL. intraday-data-loader stores the in-progress
bucket deliberately and corrects it by primary key later. A snapshot row is
never revisited, so this function reads only closed bars - see
sentiment.last_closed().

Writes only to Neon project "AI Trader APP" (nameless-mountain-15353651),
database Algo, schema algo.

Modules:
    config.py     this function's tunables
    params.py     the Dhan token and client id (named params, not secrets -
                  secrets.py at the zip root shadows the stdlib one)
    dhan.py       charts + expiry list + option chain, and the measured facts
    expiry.py     which two expiries a snapshot describes
    chain.py      one chain -> ATM, straddle, PCR, OI walls, max pain, IV
    sentiment.py  session stats and the buildup label
    db.py         Neon access, the baseline read, the two-table write

Epoch/IST handling, the Neon connection and the shared connection-string read
come from the neon-access layer.
"""

import datetime
import logging
import time

from neon_access import (
    IST,
    connect,
    ist_datetime,
    ist_midnight_epoch,
    now_epoch,
    read_neon_connection_string,
)

import chain as chain_lib
import expiry as expiry_lib
from config import (
    AGGREGATE_STRIKES_PER_SIDE,
    CANDLE_INTERVAL_MINUTES,
    CANDLE_INTERVAL_SECONDS,
    FIRST_RUN,
    FUTURES_INSTRUMENT_TYPE,
    FUTURES_SYMBOL_TEMPLATE,
    FUTURES_UNDERLYING_SCRIP,
    FUTURES_UNDERLYING_SEG,
    LAST_RUN,
    MONTH_ABBREVIATIONS,
    NIFTY_INSTRUMENT_TYPE,
    NIFTY_SECURITY_ID,
    RAW_STRIKES_PER_SIDE,
    VIX_INSTRUMENT_TYPE,
    VIX_SECURITY_ID,
)
from db import (
    previous_snapshot,
    resolve_by_symbol,
    resolve_instrument,
    write_snapshot,
)
from dhan import DhanClient, require_oi, session_candles, to_candles
from params import read_client_id, read_token_record
from sentiment import buildup, last_closed, pct_change, session_stats

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def fetch_session(client, instrument, day, with_oi=False):
    """Today's closed-and-open 5-minute bars for one instrument."""
    return session_candles(
        to_candles(
            client.intraday_candles(
                instrument, CANDLE_INTERVAL_MINUTES, day, with_oi=with_oi
            )
        ),
        day,
    )


def bar_at(candles, snapshot_ts, label):
    """
    The bar stamped `snapshot_ts`, or the newest closed one before it.

    The index sets snapshot_ts and every other series is aligned to it, so
    that basis, VIX and spot all describe the same instant. A series that is
    short a bar at exactly that stamp falls back to its newest earlier one and
    says so - a futures bar can be missing where the index has one, and taking
    that series' own latest bar instead would compute basis across two
    different minutes without a word.
    """
    exact = [c for c in candles if c["ts"] == snapshot_ts]
    if exact:
        return exact[0]
    earlier = [c for c in candles if c["ts"] < snapshot_ts]
    if not earlier:
        raise RuntimeError(
            f"{label} has no bar at or before {ist_datetime(snapshot_ts):%H:%M} "
            f"- the series does not cover this snapshot"
        )
    fallback = max(earlier, key=lambda c: c["ts"])
    logger.warning(
        "%s has no bar at %s - falling back to %s, %d minute(s) stale",
        label, ist_datetime(snapshot_ts).strftime("%H:%M"),
        ist_datetime(fallback["ts"]).strftime("%H:%M"),
        (snapshot_ts - fallback["ts"]) // 60,
    )
    return fallback


def future_symbol(nearest_expiry):
    """
    The current-month future's trading symbol, from the NEAREST expiry's
    month - the same rule intraday-data-loader uses, so the two functions
    cannot disagree about which contract is current.
    """
    month, year = expiry_lib.futures_month(nearest_expiry)
    return FUTURES_SYMBOL_TEMPLATE.format(
        month=MONTH_ABBREVIATIONS[month - 1], year=year
    )


def oi_change(current_total, baseline, baseline_key, expiry_ts, baseline_expiry_key):
    """
    An OI delta, or None when the baseline describes a different contract.

    Across an expiry roll the previous row's totals belong to a contract that
    no longer exists here. A percentage between the two would be arithmetic on
    unrelated numbers that reads exactly like a real collapse in open
    interest, so it is left null instead.
    """
    if not baseline:
        return None
    if baseline.get(baseline_expiry_key) != expiry_ts:
        logger.info(
            "expiry rolled (%s -> %s) - %s left null",
            baseline.get(baseline_expiry_key), expiry_ts, baseline_key,
        )
        return None
    return pct_change(current_total, baseline.get(baseline_key))


def lambda_handler(event, context):
    event = event or {}
    now = (
        datetime.datetime.fromisoformat(event["now"]).replace(tzinfo=IST)
        if event.get("now")
        else datetime.datetime.now(IST)
    )
    today = now.date()

    if today.weekday() >= 5:
        logger.info("%s is a %s - not a trading day", today, today.strftime("%A"))
        return {"status": "skipped_non_trading_day", "date": today.isoformat()}

    # Second line of defence only. Three EventBridge rules already produce
    # exactly the 25 in-window times; this catches a manual invocation or a
    # rule edited by hand.
    if not FIRST_RUN <= now.time() <= LAST_RUN:
        logger.info("%s is outside %s-%s", now.strftime("%H:%M"), FIRST_RUN, LAST_RUN)
        return {"status": "skipped_outside_session", "time": now.strftime("%H:%M")}

    started = time.monotonic()
    record = read_token_record()
    client = DhanClient(record["access_token"], read_client_id(record))
    conn = connect(read_neon_connection_string())
    try:
        index = resolve_instrument(conn, NIFTY_SECURITY_ID, NIFTY_INSTRUMENT_TYPE)
        vix_instrument = resolve_instrument(conn, VIX_SECURITY_ID, VIX_INSTRUMENT_TYPE)

        # ---- the index sets the clock for everything else -----------------
        index_bars = fetch_session(client, index, today)
        if not index_bars:
            # A holiday needs no calendar: the exchange published no bars, so
            # there is nothing to describe and nothing is written.
            logger.info("%s returned no session bars - not a trading day", today)
            return {"status": "skipped_non_trading_day", "date": today.isoformat()}

        run_epoch = int(now.timestamp())
        spot_bar = last_closed(index_bars, run_epoch, CANDLE_INTERVAL_SECONDS, "NIFTY")
        snapshot_ts = spot_bar["ts"]
        index_stats = session_stats(index_bars, snapshot_ts)
        spot = index_stats["close"]
        logger.info(
            "snapshot %s (run %s), spot %.2f",
            ist_datetime(snapshot_ts).strftime("%H:%M"), now.strftime("%H:%M"), spot,
        )

        # ---- expiries, then the future they imply --------------------------
        nearest_date, monthly_date = expiry_lib.select(
            client.expiry_list(FUTURES_UNDERLYING_SCRIP, FUTURES_UNDERLYING_SEG),
            today,
        )
        near_expiry_ts = ist_midnight_epoch(nearest_date)
        mth_expiry_ts = ist_midnight_epoch(monthly_date)

        future = resolve_by_symbol(
            conn, future_symbol(nearest_date), FUTURES_INSTRUMENT_TYPE
        )
        future_bars = require_oi(
            fetch_session(client, future, today, with_oi=True),
            future["trading_symbol"],
        )
        future_bar = bar_at(future_bars, snapshot_ts, future["trading_symbol"])

        # ---- india vix ------------------------------------------------------
        vix_bars = fetch_session(client, vix_instrument, today)
        vix_bar = bar_at(vix_bars, snapshot_ts, "INDIA VIX") if vix_bars else None
        vix_stats = session_stats(vix_bars, snapshot_ts) if vix_bars else None
        if not vix_bars:
            logger.warning("INDIA VIX returned no bars - vix columns left null")

        # ---- the two chains, both centred on the SAME spot -----------------
        near_data = client.option_chain(
            FUTURES_UNDERLYING_SCRIP, FUTURES_UNDERLYING_SEG, nearest_date
        )
        mth_data = client.option_chain(
            FUTURES_UNDERLYING_SCRIP, FUTURES_UNDERLYING_SEG, monthly_date
        )
        near = chain_lib.summarise(near_data, spot, AGGREGATE_STRIKES_PER_SIDE)
        mth = chain_lib.summarise(mth_data, spot, AGGREGATE_STRIKES_PER_SIDE)
        logger.info(
            "nearest %s: atm %s straddle %s pcr_oi %s over %d strikes (step %s)",
            nearest_date, near["atm_strike"], near["straddle"], near["pcr_oi"],
            near["strikes_scoped"], near["strike_step"],
        )

        # ---- the one thing read back from the database ---------------------
        baseline = previous_snapshot(conn, index, snapshot_ts)

        basis = future_bar["close"] - spot
        row = {
            "security_id": index["security_id"],
            "instrument_type": index["instrument_type"],
            "snapshot_ts": snapshot_ts,
            "captured_at": run_epoch,
            "prev_snapshot_ts": baseline["snapshot_ts"] if baseline else None,

            "spot": spot,
            "chain_spot": near_data.get("last_price"),
            "spot_change_pct": pct_change(
                spot, baseline["spot"] if baseline else None
            ),
            "day_high": index_stats["high"],
            "day_low": index_stats["low"],
            "vwap": index_stats["vwap"],
            "orb_high": index_stats["orb_high"],
            "orb_low": index_stats["orb_low"],

            "fut_security_id": future["security_id"],
            "fut_symbol": future["trading_symbol"],
            "fut_price": future_bar["close"],
            "fut_oi": future_bar["open_interest"],
            "basis": basis,
            "basis_pct": basis / spot * 100 if spot else None,
            "fut_price_change_pct": pct_change(
                future_bar["close"], baseline["fut_price"] if baseline else None
            ),
            "fut_oi_change_pct": pct_change(
                future_bar["open_interest"], baseline["fut_oi"] if baseline else None
            ),
            "buildup": None,  # filled below, once both deltas exist

            "vix": vix_bar["close"] if vix_bar else None,
            "vix_open": vix_stats["open"] if vix_stats else None,
            "vix_day_high": vix_stats["high"] if vix_stats else None,
            "vix_day_low": vix_stats["low"] if vix_stats else None,
            "vix_change_pct": pct_change(
                vix_bar["close"] if vix_bar else None,
                baseline["vix"] if baseline else None,
            ),

            "near_expiry_ts": near_expiry_ts,
            "mth_expiry_ts": mth_expiry_ts,
            "created_at": now_epoch(),
        }
        row["buildup"] = buildup(
            row["fut_price_change_pct"], row["fut_oi_change_pct"]
        )

        for prefix, summary, expiry_ts, expiry_key in (
            ("near", near, near_expiry_ts, "near_expiry_ts"),
            ("mth", mth, mth_expiry_ts, "mth_expiry_ts"),
        ):
            row.update({
                f"{prefix}_atm_strike": summary["atm_strike"],
                f"{prefix}_ce_ltp": summary["ce_ltp"],
                f"{prefix}_pe_ltp": summary["pe_ltp"],
                f"{prefix}_straddle": summary["straddle"],
                f"{prefix}_straddle_pct": summary["straddle_pct"],
                f"{prefix}_pcr_oi": summary["pcr_oi"],
                f"{prefix}_pcr_volume": summary["pcr_volume"],
                f"{prefix}_ce_oi_total": summary["ce_oi_total"],
                f"{prefix}_pe_oi_total": summary["pe_oi_total"],
                f"{prefix}_ce_oi_change_pct": oi_change(
                    summary["ce_oi_total"], baseline,
                    f"{prefix}_ce_oi_total", expiry_ts, expiry_key,
                ),
                f"{prefix}_pe_oi_change_pct": oi_change(
                    summary["pe_oi_total"], baseline,
                    f"{prefix}_pe_oi_total", expiry_ts, expiry_key,
                ),
                f"{prefix}_max_oi_call": summary["max_oi_call"],
                f"{prefix}_max_oi_put": summary["max_oi_put"],
                f"{prefix}_max_pain": summary["max_pain"],
                f"{prefix}_ce_iv": summary["ce_iv"],
                f"{prefix}_pe_iv": summary["pe_iv"],
                f"{prefix}_iv_skew": summary["iv_skew"],
            })

        # Raw legs, NEAREST EXPIRY ONLY - 5 strikes x CE/PE = 10 rows.
        legs = [
            dict(
                leg,
                security_id=index["security_id"],
                instrument_type=index["instrument_type"],
                snapshot_ts=snapshot_ts,
                expiry_ts=near_expiry_ts,
                created_at=row["created_at"],
            )
            for leg in chain_lib.raw_legs(near_data, spot, RAW_STRIKES_PER_SIDE)
        ]

        legs_written = write_snapshot(conn, row, legs)
        elapsed = time.monotonic() - started
        logger.info(
            "done in %.1fs: snapshot %s, %d legs, buildup %s",
            elapsed, ist_datetime(snapshot_ts).strftime("%H:%M"),
            legs_written, row["buildup"],
        )
        return {
            "status": "success",
            "date": today.isoformat(),
            "snapshot": ist_datetime(snapshot_ts).strftime("%H:%M"),
            "captured": now.strftime("%H:%M"),
            "spot": spot,
            "basis": round(basis, 2),
            "buildup": row["buildup"],
            "near_expiry": nearest_date.isoformat(),
            "monthly_expiry": monthly_date.isoformat(),
            "near_straddle": near["straddle"],
            "near_pcr_oi": near["pcr_oi"],
            "legs_written": legs_written,
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass
