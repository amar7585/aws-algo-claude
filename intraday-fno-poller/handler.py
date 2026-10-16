"""
Intraday F&O Poller - AWS Lambda function

Writes one row to algo.fno_5m_snapshot and ten raw option legs to
algo.fno_5m_leg every FIVE minutes through the session: the futures price, open
interest and volume (including the last closed bar's own OI change), the option
chain aggregates for the nearest and monthly expiries, and the raw ATM +-2 legs.

ON ITS OWN CRON. Three EventBridge rules, 72 runs a day, one minute past each
five-minute mark (see config.py). It invokes nothing and is invoked by nothing:
the 15-minute chain (loader -> intraday-market-sentiment -> market-classifier ->
pattern-detector -> strategy-manager) is untouched, and NOTHING READS THIS
FUNCTION'S TABLES YET. Phase 1 is storing - Dhan serves only a LIVE option
chain, so a five-minute reading not taken is gone for good.

It does not classify, score, read the daily read, or compute SMA/RSI, breadth or
VIX. Those stay on the 15-minute row, and the fast row joins to it at read time
on snapshot_ts.

Reads Neon project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. Dhan calls: the index's and the future's 5-minute bars, the expiry
list (cached per IST date), and the nearest and monthly chains.

Modules:
    config.py    this function's tunables
    params.py    the Dhan token and client id
    dhan.py      charts + expiry list + option chain
    expiry.py    nearest / monthly selection
    chain.py     ATM, PCR, OI walls, max pain, raw legs
    reading.py   which bar is which, and what changed (pure)
    db.py        the two-table write, the previous-tick read
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
)
from db import previous_tick, resolve_by_symbol, resolve_instrument, write_snapshot
from dhan import DhanClient, require_oi, session_candles, to_candles
from params import read_client_id, read_token_record
from reading import (
    bar_at,
    bar_before,
    change,
    last_closed,
    newest_bar,
    pct_change,
    same_contract,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

INTERVAL_SECONDS = CANDLE_INTERVAL_MINUTES * 60

# The expiry list changes on the exchange's timetable, not every five minutes,
# so a warm container reads it once per IST date. Same idea as the loader's
# per-date future cache; a cold container simply fetches it again.
_EXPIRY_CACHE = {"date": None, "dates": None}


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


def expiry_dates(client, today):
    if _EXPIRY_CACHE["date"] != today:
        _EXPIRY_CACHE["dates"] = client.expiry_list(
            FUTURES_UNDERLYING_SCRIP, FUTURES_UNDERLYING_SEG
        )
        _EXPIRY_CACHE["date"] = today
    return _EXPIRY_CACHE["dates"]


def future_symbol(nearest_expiry):
    """
    The current-month future's trading symbol, from the NEAREST expiry's month -
    the same rule intraday-data-loader and intraday-market-sentiment use, so the
    three functions cannot disagree about which contract is current.
    """
    month, year = expiry_lib.futures_month(nearest_expiry)
    return FUTURES_SYMBOL_TEMPLATE.format(
        month=MONTH_ABBREVIATIONS[month - 1], year=year
    )


def expiry_block(prefix, summary, expiry_ts, baseline):
    """
    The aggregate columns for one expiry, with OI changes against the baseline.

    The changes are None when the baseline row describes a different expiry:
    its OI totals belong to a contract that no longer exists here, and a
    percentage between the two reads exactly like a real collapse in OI.
    """
    ce_total, pe_total = summary["ce_oi_total"], summary["pe_oi_total"]
    comparable = same_contract(baseline, f"{prefix}_expiry_ts", expiry_ts)
    if baseline and not comparable:
        logger.info(
            "%s expiry rolled (%s -> %s) - OI changes left null",
            prefix, baseline.get(f"{prefix}_expiry_ts"), expiry_ts,
        )
    ce_base = baseline.get(f"{prefix}_ce_oi_total") if comparable else None
    pe_base = baseline.get(f"{prefix}_pe_oi_total") if comparable else None
    return {
        f"{prefix}_expiry_ts": expiry_ts,
        f"{prefix}_atm_strike": summary["atm_strike"],
        f"{prefix}_ce_ltp": summary["ce_ltp"],
        f"{prefix}_pe_ltp": summary["pe_ltp"],
        f"{prefix}_straddle": summary["straddle"],
        f"{prefix}_straddle_pct": summary["straddle_pct"],
        f"{prefix}_pcr_oi": summary["pcr_oi"],
        f"{prefix}_pcr_volume": summary["pcr_volume"],
        f"{prefix}_ce_oi_total": ce_total,
        f"{prefix}_pe_oi_total": pe_total,
        f"{prefix}_ce_oi_change": change(ce_total, ce_base),
        f"{prefix}_pe_oi_change": change(pe_total, pe_base),
        f"{prefix}_ce_oi_change_pct": pct_change(ce_total, ce_base),
        f"{prefix}_pe_oi_change_pct": pct_change(pe_total, pe_base),
        f"{prefix}_max_oi_call": summary["max_oi_call"],
        f"{prefix}_max_oi_put": summary["max_oi_put"],
        f"{prefix}_max_pain": summary["max_pain"],
        f"{prefix}_ce_iv": summary["ce_iv"],
        f"{prefix}_pe_iv": summary["pe_iv"],
        f"{prefix}_iv_skew": summary["iv_skew"],
    }


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
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

    # Second line of defence only. The cron produces exactly the in-window
    # times; this catches a manual invocation.
    if not FIRST_RUN <= now.time() <= LAST_RUN:
        logger.info("%s is outside %s-%s", now.strftime("%H:%M"), FIRST_RUN, LAST_RUN)
        return {"status": "skipped_outside_session", "time": now.strftime("%H:%M")}

    started = time.monotonic()
    record = read_token_record()
    client = DhanClient(record["access_token"], read_client_id(record))
    conn = connect(read_neon_connection_string())
    try:
        index = resolve_instrument(conn, NIFTY_SECURITY_ID, NIFTY_INSTRUMENT_TYPE)

        # ---- the index sets the clock for everything else -----------------
        index_bars = fetch_session(client, index, today)
        if not index_bars:
            # A holiday needs no calendar: the exchange published no bars, so
            # there is nothing to read and nothing is written.
            logger.info("%s returned no session bars - not a trading day", today)
            return {"status": "skipped_non_trading_day", "date": today.isoformat()}

        run_epoch = int(now.timestamp())
        snapshot_bar = newest_bar(index_bars, "NIFTY")
        snapshot_ts = snapshot_bar["ts"]
        spot = snapshot_bar["close"]
        index_closed = last_closed(index_bars, run_epoch, INTERVAL_SECONDS, "NIFTY")

        # ---- expiries, then the future they imply --------------------------
        nearest_date, monthly_date = expiry_lib.select(
            expiry_dates(client, today), today
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
        future_snapshot = bar_at(future_bars, snapshot_ts, future["trading_symbol"])
        future_closed = last_closed(
            future_bars, run_epoch, INTERVAL_SECONDS, future["trading_symbol"]
        )
        if future_closed["ts"] != index_closed["ts"]:
            logger.warning(
                "closed bars differ: index %s, future %s",
                ist_datetime(index_closed["ts"]).strftime("%H:%M"),
                ist_datetime(future_closed["ts"]).strftime("%H:%M"),
            )

        # The closed bar's OI change comes from Dhan's OWN consecutive bars in
        # this one response, so it does not depend on a previous row existing
        # and a missed tick cannot corrupt it. It is only trusted when the two
        # bars really are adjacent - a gap in the series would make it a
        # multi-bar change under a single-bar name.
        previous_bar = bar_before(future_bars, future_closed)
        if previous_bar and future_closed["ts"] - previous_bar["ts"] == INTERVAL_SECONDS:
            bar_oi_change = change(
                future_closed["open_interest"], previous_bar["open_interest"]
            )
        else:
            bar_oi_change = None
            if previous_bar:
                logger.warning(
                    "future bars %s and %s are not adjacent - fut_bar_oi_change null",
                    ist_datetime(previous_bar["ts"]).strftime("%H:%M"),
                    ist_datetime(future_closed["ts"]).strftime("%H:%M"),
                )

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
        baseline = previous_tick(
            conn, index, snapshot_ts, ist_midnight_epoch(today)
        )
        fut_base = (
            baseline
            if same_contract(baseline, "fut_security_id", future["security_id"])
            else None
        )
        if baseline and not fut_base:
            logger.info(
                "future rolled (%s -> %s) - fut_price_change_pct and the "
                "fut_oi change columns left null",
                baseline.get("fut_security_id"), future["security_id"],
            )

        fut_price, fut_oi = future_snapshot["close"], future_snapshot["open_interest"]
        basis = fut_price - spot
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
            "idx_bar_ts": index_closed["ts"],
            "idx_bar_volume": index_closed["volume"],

            "fut_security_id": future["security_id"],
            "fut_symbol": future["trading_symbol"],
            "fut_price": fut_price,
            "fut_oi": fut_oi,
            "basis": basis,
            "basis_pct": basis / spot * 100 if spot else None,
            "fut_price_change_pct": pct_change(
                fut_price, fut_base["fut_price"] if fut_base else None
            ),
            "fut_oi_change": change(fut_oi, fut_base["fut_oi"] if fut_base else None),
            "fut_oi_change_pct": pct_change(
                fut_oi, fut_base["fut_oi"] if fut_base else None
            ),
            "fut_bar_ts": future_closed["ts"],
            "fut_bar_volume": future_closed["volume"],
            "fut_bar_oi": future_closed["open_interest"],
            "fut_bar_oi_change": bar_oi_change,

            "created_at": now_epoch(),
        }
        row.update(expiry_block("near", near, near_expiry_ts, baseline))
        row.update(expiry_block("mth", mth, mth_expiry_ts, baseline))

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
        if not legs:
            # A row without its legs would look complete to every reader.
            raise RuntimeError(
                f"the {nearest_date} chain produced no raw legs around spot {spot} "
                f"- refusing to write a tick with no legs"
            )

        legs_written = write_snapshot(conn, row, legs)

        elapsed = time.monotonic() - started
        logger.info(
            "done in %.1fs: snapshot %s, closed bar %s, spot %.2f, fut_bar vol %s "
            "dOI %s, %d legs",
            elapsed, ist_datetime(snapshot_ts).strftime("%H:%M"),
            ist_datetime(future_closed["ts"]).strftime("%H:%M"), spot,
            future_closed["volume"], bar_oi_change, legs_written,
        )
        return {
            "status": "success",
            "date": today.isoformat(),
            "snapshot": ist_datetime(snapshot_ts).strftime("%H:%M"),
            "closed_bar": ist_datetime(future_closed["ts"]).strftime("%H:%M"),
            "captured": now.strftime("%H:%M"),
            "spot": spot,
            "fut_symbol": future["trading_symbol"],
            "fut_bar_volume": future_closed["volume"],
            "fut_bar_oi_change": bar_oi_change,
            "near_expiry": nearest_date.isoformat(),
            "monthly_expiry": monthly_date.isoformat(),
            "near_pcr_oi": near["pcr_oi"],
            "legs_written": legs_written,
            "dhan_calls": client.calls,
            "elapsed_seconds": round(elapsed, 2),
        }
    finally:
        # Any exception propagates - Lambda must record an error. Never return
        # a {"statusCode": 500} shape; Lambda counts that as a success.
        try:
            conn.close()
        except Exception:
            pass
