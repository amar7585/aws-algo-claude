"""
The fine gate.

THE GATE IS NOT A FORMALITY, and the playbook says why: the identical candle
pattern on a trending day is a breakout retest, and fading it is how this
setup loses. So a failed check stops the scan. It does not become a
lower-confidence candidate, it does not get averaged with the checks that
passed, and nothing below reports a near miss as a pass.

strategy-manager has already applied the coarse gate - this function is
only invoked for the regime/bias combinations its registry allows.
Everything here is the part
the manager cannot know: what THIS playbook needs of the VIX, of how
much range the day has already spent, of the opening range's width, and of the
clock. Re-checking the regime as well is deliberate; a strategy that trusts an
upstream gate it cannot see will fire on a bad day the moment that gate moves.
"""

import logging

from config import (
    ALLOWED_BIASES,
    ALLOWED_REGIMES,
    LATE_END,
    MAX_RANGE_USED_VS_ATR,
    MAX_VIX_CHANGE_PCT,
    MIN_ORB_RANGE_PCT,
    PRIME_END,
    SCAN_START,
)
from levels import ist_time

logger = logging.getLogger()

WINDOW_FORMING = "range still forming"
WINDOW_PRIME = "prime"
WINDOW_LATE = "late - stacked pool and a near target only"
WINDOW_CLOSED = "closed to new entries"


def scan_window(snapshot_ts):
    """Which of the playbook's four time windows `snapshot_ts` falls in."""
    moment = ist_time(snapshot_ts)
    if moment < SCAN_START:
        return WINDOW_FORMING
    if moment < PRIME_END:
        return WINDOW_PRIME
    if moment < LATE_END:
        return WINDOW_LATE
    return WINDOW_CLOSED


def evaluate(snapshot, classification, daily_atr14, orb, snapshot_ts):
    """
    Run every check. Returns (passed, failures, details).

    EVERY CHECK RUNS EVEN AFTER ONE FAILS. Short-circuiting would report the
    first problem and hide the rest, so a session that failed on VIX would
    look like it might otherwise have traded when the range was also spent.
    The playbook wants the failing check named; naming all of them costs
    nothing.

    A check whose INPUT is missing fails as `unknown` rather than passing.
    That is the direction that matters: the 09:35 run has no daily row yet, so
    pd_high/pd_low and the range gate have nothing to compare against, and a
    missing input must not read as a satisfied condition.
    """
    failures, details = [], {}

    # ---- regime -----------------------------------------------------------
    regime = classification.get("regime")
    bias = classification.get("bias")
    details["regime"] = regime
    details["bias"] = bias
    if regime not in ALLOWED_REGIMES:
        failures.append(
            f"regime is {regime}, and this playbook is valid only in "
            f"{'/'.join(sorted(ALLOWED_REGIMES))} - the same pattern on a "
            f"trending day is a breakout retest"
        )
    if bias not in ALLOWED_BIASES:
        failures.append(f"bias {bias} is not in {'/'.join(sorted(ALLOWED_BIASES))}")

    # ---- india vix --------------------------------------------------------
    # The snapshot's vix_change_pct is measured against its previous snapshot,
    # so this is the 15-minute change rather than a change since the open.
    # "Not expanding" is the same measurement: there is no separate volatility
    # series here, and inventing one would be a different check wearing the
    # playbook's words.
    vix_change = snapshot.get("vix_change_pct")
    details["vix_change_pct"] = vix_change
    details["vix"] = snapshot.get("vix")
    if vix_change is None:
        failures.append(
            "vix_change_pct is unknown - the first snapshot of a session has "
            "no baseline, so the volatility check cannot be satisfied"
        )
    elif abs(float(vix_change)) > MAX_VIX_CHANGE_PCT:
        failures.append(
            f"india vix moved {float(vix_change):+.2f}% against a "
            f"+-{MAX_VIX_CHANGE_PCT}% limit"
        )

    # ---- range already spent ----------------------------------------------
    day_high, day_low = snapshot.get("day_high"), snapshot.get("day_low")
    adr = (
        float(day_high) - float(day_low)
        if day_high is not None and day_low is not None
        else None
    )
    details["adr"] = round(adr, 2) if adr is not None else None
    details["atr14"] = daily_atr14
    details["range_used"] = (
        round(adr / float(daily_atr14), 3)
        if adr is not None and daily_atr14
        else None
    )
    if adr is None:
        failures.append("the day's range so far is unknown")
    elif not daily_atr14:
        failures.append(
            "atr14 is unavailable - there is no baseline to measure the day's "
            "spent range against"
        )
    elif adr >= float(daily_atr14) * MAX_RANGE_USED_VS_ATR:
        failures.append(
            f"the day has already used {adr:.0f} of its {float(daily_atr14):.0f} "
            f"point true-ATR14 ({adr / float(daily_atr14):.2f}x against a "
            f"{MAX_RANGE_USED_VS_ATR}x limit) - there is nothing left to rotate "
            f"into"
        )

    # ---- opening-range width ----------------------------------------------
    price = snapshot.get("spot") or classification.get("close")
    orb_range = (orb["high"] - orb["low"]) if orb else None
    details["orb_range"] = round(orb_range, 2) if orb_range is not None else None
    details["orb_range_pct"] = (
        round(orb_range / float(price) * 100, 4)
        if orb_range is not None and price
        else None
    )
    if orb is None:
        failures.append("no opening range - the 09:15-09:30 bars are not present")
    elif not orb.get("complete"):
        failures.append(
            f"the opening range is built from {orb['bars']} of 3 five-minute "
            f"bars and is not complete"
        )
    elif not price:
        failures.append("no price to measure the opening range against")
    elif orb_range / float(price) * 100 < MIN_ORB_RANGE_PCT:
        failures.append(
            f"the opening range is {orb_range:.0f} points, "
            f"{orb_range / float(price) * 100:.3f}% of price against a "
            f"{MIN_ORB_RANGE_PCT}% minimum - too narrow to pay a 1:2"
        )

    # ---- the clock --------------------------------------------------------
    window = scan_window(snapshot_ts)
    details["window"] = window
    if window == WINDOW_FORMING:
        failures.append(
            "the opening range is still forming - no scanning before "
            f"{SCAN_START.strftime('%H:%M')}"
        )
    elif window == WINDOW_CLOSED:
        failures.append(
            f"no new entries after {LATE_END.strftime('%H:%M')}"
        )

    return not failures, failures, details
