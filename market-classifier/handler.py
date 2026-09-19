"""
Market Classifier - AWS Lambda function

Turns one MEASUREMENT snapshot into JUDGEMENT: reads the algo.intraday_fno_data
row intraday-market-sentiment just wrote (handed over in the invoke payload),
scores it with the shared market-classifier layer, derives the futures-OI
buildup, writes algo.intraday_sentiments, and invokes pattern-detector.

NOT ON A CRON. intraday-market-sentiment invokes this once it has written its
measurement row - the completion of that write is the only honest trigger, so
there is no schedule here and no race with the writer. The chain is
    loader -> intraday-market-sentiment -> this -> pattern-detector -> strategy-manager

ONE RULE SET, BOTH FRAMES. classify() here is the SAME layer function
daily-market-sentiment runs on daily candles. This function assembles the
5-minute-frame inputs (the scored bar's stored scalars, the structure read over
today's bars, the VIX baseline and the session-elapsed fraction) and calls it;
it re-implements no rule. See inputs.py.

WHAT IT WRITES. algo.intraday_sentiments only. The measurement it scored is
already in algo.intraday_fno_data, written by the function that measured it.

Writes only to Neon project "AI Trader APP" (nameless-mountain-15353651),
database Algo, schema algo.

Modules:
    config.py    tunables and the pattern-detector wiring
    inputs.py    assembling classify()'s inputs, and the buildup label
    db.py        Neon access, the judgement-row write
    dispatch.py  handing the judgement to pattern-detector

connect(), now_epoch and the IST helpers come from the neon-access layer; the
scoring rules from the market-classifier layer.
"""

import datetime
import logging
import time

from neon_access import connect, ist_datetime, now_epoch, read_neon_connection_string

from db import write_sentiment
from dispatch import dispatch_to_pattern_detector
from inputs import buildup, classify_snapshot

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The fno keys this function must have to score. Everything else it carries
# through. A payload without these means intraday-market-sentiment sent an old
# shape - raising here names the real problem rather than failing deep in the
# scoring on a missing indicator.
REQUIRED_FNO_KEYS = (
    "security_id", "instrument_type", "snapshot_ts", "captured_at",
    "spot", "sma9", "sma50", "sma100", "sma200", "rsi",
)


def read_event(event):
    """Validate the sentiment function's payload and return its parts."""
    if not isinstance(event, dict):
        raise RuntimeError(
            f"expected intraday-market-sentiment's payload object, got "
            f"{type(event).__name__}"
        )
    fno = event.get("fno")
    if not isinstance(fno, dict):
        raise RuntimeError(
            f"payload carries no `fno` measurement row - keys present: "
            f"{sorted(event)}"
        )
    missing = [k for k in REQUIRED_FNO_KEYS if fno.get(k) is None]
    if missing:
        raise RuntimeError(
            f"fno row is missing {missing} - it was not fully measured. The "
            f"SMAs and RSI are written by intraday-market-sentiment; a row "
            f"without them predates this wiring. Keys present: {sorted(fno)}"
        )

    session_bars = event.get("session_bars")
    if not isinstance(session_bars, list) or not session_bars:
        raise RuntimeError(
            "payload carries no `session_bars` - the structure read needs "
            "today's session bars"
        )

    instrument = event.get("instrument")
    if not isinstance(instrument, dict):
        raise RuntimeError("payload carries no `instrument` object")

    now_raw = event.get("now")
    if not now_raw:
        raise RuntimeError(
            "payload carries no `now` run clock - session_elapsed cannot be "
            "computed, and guessing it would bias the volatility read"
        )
    now = datetime.datetime.fromisoformat(now_raw)

    return fno, instrument, session_bars, event.get("vix_baseline"), now, event.get("daily")


def build_row(fno, result, now):
    """The judgement row for algo.intraday_sentiments."""
    return {
        "security_id": fno["security_id"],
        "instrument_type": fno["instrument_type"],
        "snapshot_ts": int(fno["snapshot_ts"]),
        "captured_at": int(fno["captured_at"]),
        "prev_snapshot_ts": fno.get("prev_snapshot_ts"),
        "bias": result["bias"],
        "structure": result["structure"],
        "regime": result["regime"],
        "volatility": result["volatility"],
        "score": int(result["score"]),
        "max_score": int(result["max_score"]),
        "confidence": result["confidence"],
        "buildup": buildup(
            fno.get("fut_price_change_pct"), fno.get("fut_oi_change_pct")
        ),
        "swing_direction": result["swing_direction"],
        "swing_high": result["swing_high"],
        "swing_low": result["swing_low"],
        "structure_determined": result["structure_determined"],
        "range_used": result["range_used"],
        "session_elapsed": result["session_elapsed"],
        "volatility_expanding": result["volatility_expanding"],
        "created_at": now_epoch(),
    }


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    fno, instrument, session_bars, vix_baseline, now, daily = read_event(event or {})
    snapshot_ts = int(fno["snapshot_ts"])

    result = classify_snapshot(fno, session_bars, vix_baseline, now)
    row = build_row(fno, result, now)

    conn = connect(read_neon_connection_string())
    try:
        write_sentiment(conn, row)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Write, then invoke - a failed dispatch raises with the row already
    # committed, and the upsert makes a retry rewrite the identical row.
    dispatched = dispatch_to_pattern_detector(row, fno, instrument, daily)

    elapsed = time.monotonic() - started
    logger.info(
        "done in %.2fs: snapshot %s bias %s structure %s regime %s buildup %s "
        "(%+d/%d, confidence %.1f) - dispatched to %s",
        elapsed, ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        row["bias"], row["structure"], row["regime"], row["buildup"],
        row["score"], row["max_score"], row["confidence"], dispatched,
    )
    return {
        "status": "success",
        "snapshot": ist_datetime(snapshot_ts).strftime("%Y-%m-%d %H:%M"),
        "instrument": instrument.get("trading_symbol"),
        "bias": row["bias"],
        "structure": row["structure"],
        "regime": row["regime"],
        "volatility": row["volatility"],
        "buildup": row["buildup"],
        "score": f"{row['score']:+d}/{row['max_score']}",
        "confidence": row["confidence"],
        "dispatched_to": dispatched,
        "elapsed_seconds": round(elapsed, 2),
    }
