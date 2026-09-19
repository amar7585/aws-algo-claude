"""
Assembling classify()'s inputs from the measurement snapshot, and the buildup.

The RULES are the market-classifier LAYER's, shared unchanged with
daily-market-sentiment. This module only turns the intraday_fno_data row plus
today's bars into what classify() reads - the scored bar's stored scalars, the
structure read over today's session, and the volatility inputs. It is the
5-minute-frame counterpart of what daily-market-sentiment/sentiment.py does for
the daily frame.

WHY THE STRUCTURE READ IS HERE, NOT IN MEASUREMENT. structure is one of the
three classification answers, so it is judgement, and it runs here off today's
session bars handed over in the payload. The SMAs and RSI are the other way
round - measurement fetches the ~200-bar history they need and stores the
scalars, and this function reads them off the row rather than re-fetching.

session_elapsed comes from the RUN CLOCK, not snapshot_ts: at a 09:45 run the
bar stamped 09:45 has zero seconds of data in it, so 30 minutes have elapsed,
not 35. expected_move and session_elapsed used to live in
intraday-market-sentiment/classification.py; they moved here with the scoring.
"""

import logging
import math

from market_classifier import classify, read_structure

from config import (
    BUILDUP_EPSILON_PCT,
    EXPECTED_MOVE_K,
    SESSION_MINUTES,
    SESSION_START,
    TRADING_DAYS_PER_YEAR,
)

logger = logging.getLogger()

FRAME = "5min"

# The indicator scalars measurement stored on the fno row and the classifier
# scores from. close comes from `spot`.
INDICATOR_COLUMNS = ("sma9", "sma50", "sma100", "sma200", "rsi")

# buildup labels - moved here from intraday-market-sentiment/sentiment.py.
LONG_BUILDUP = "LONG_BUILDUP"
SHORT_BUILDUP = "SHORT_BUILDUP"
LONG_UNWINDING = "LONG_UNWINDING"
SHORT_COVERING = "SHORT_COVERING"
FLAT = "FLAT"


def buildup(price_change_pct, oi_change_pct, epsilon=None):
    """
    The four-way read of price against open interest.

        price up,   OI up    LONG_BUILDUP    new longs, conviction
        price down, OI up    SHORT_BUILDUP   new shorts, conviction
        price up,   OI down  SHORT_COVERING  shorts closing, not new buying
        price down, OI down  LONG_UNWINDING  longs closing, not new selling

    The distinction that matters is the second column: a rally on rising OI is
    money coming in, a rally on falling OI is money leaving - they look identical
    on a price chart. `epsilon` (BUILDUP_EPSILON_PCT) is the move below which a
    change counts as none; 0.0 means pure sign.
    """
    if price_change_pct is None or oi_change_pct is None:
        return None
    epsilon = BUILDUP_EPSILON_PCT if epsilon is None else epsilon
    if abs(price_change_pct) <= epsilon or abs(oi_change_pct) <= epsilon:
        return FLAT
    if price_change_pct > 0:
        return LONG_BUILDUP if oi_change_pct > 0 else SHORT_COVERING
    return SHORT_BUILDUP if oi_change_pct > 0 else LONG_UNWINDING


def expected_move(price, vix):
    """The VIX-implied one-sigma move for a whole session, or None."""
    if price is None or vix is None:
        return None
    return float(price) * (float(vix) / 100) / math.sqrt(TRADING_DAYS_PER_YEAR) * (
        EXPECTED_MOVE_K
    )


def session_elapsed(now):
    """
    The fraction of the trading session that has actually happened, 0 < f <= 1.

    From the wall clock of the run, not from snapshot_ts. Clamped at both ends -
    the 15:35 closing sweep is past the close and would otherwise read above 1.
    """
    start = now.replace(
        hour=SESSION_START.hour, minute=SESSION_START.minute, second=0, microsecond=0
    )
    elapsed = (now - start).total_seconds() / 60
    return min(max(elapsed / SESSION_MINUTES, 1e-6), 1.0)


def scored_bar(fno):
    """The bar classify() scores: close is `spot`, plus the stored SMAs and RSI."""
    bar = {"ts": int(fno["snapshot_ts"]), "close": float(fno["spot"])}
    for column in INDICATOR_COLUMNS:
        bar[column] = fno[column]
    return bar


def classify_snapshot(fno, session_bars, vix_baseline, now):
    """
    Run the shared classifier over one measurement snapshot.

    fno           the intraday_fno_data row (dict) - the measurement.
    session_bars  today's session bars, oldest first, for the structure read.
    vix_baseline  the previous snapshot's VIX (the expansion test's baseline).
    now           the run wall clock (IST-aware), for session_elapsed.
    """
    bar = scored_bar(fno)
    spot = float(fno["spot"])
    vix = fno.get("vix")
    day_range = None
    if fno.get("day_high") is not None and fno.get("day_low") is not None:
        day_range = float(fno["day_high"]) - float(fno["day_low"])
    structure = read_structure(session_bars, FRAME)
    elapsed = session_elapsed(now)
    result = classify(
        frame=FRAME,
        bar=bar,
        structure=structure,
        vix=vix,
        vix_baseline=vix_baseline,
        day_range=day_range,
        expected_move=expected_move(spot, vix),
        vwap=fno.get("vwap"),
        session_elapsed=elapsed,
        structure_bars=len(session_bars),
    )
    logger.info(
        "classified snapshot %s on %d session bars, %.1f%% elapsed",
        fno["snapshot_ts"], len(session_bars), elapsed * 100,
    )
    return result
