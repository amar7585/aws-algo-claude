"""
Entry, stop, targets, risk-reward and the grade.

Everything here is arithmetic on levels already found. It decides nothing
about whether to trade - the gate did that - and it sizes nothing, which the
playbook rules out explicitly.

---------------------------------------------------------------------------
THE STOP BUFFER, AND A SECOND DISAGREEMENT INSIDE THE PLAYBOOK.

The rule is unambiguous, and so is its reason: the stop goes beyond the sweep
extreme plus a buffer, because "placing the stop at the sweep extreme with no
buffer is the most common way this setup gets stopped and then works".

Both worked examples then place the stop exactly AT the sweep extreme. Example
1 stops at 24,166.45 with the sweep extreme at 24,166.45 (risk 19.20), and
Example 2 stops at 24,075.25 with the sweep extreme at 24,075.25 (risk 15.60).
Neither carries a buffer of any size.

The rule wins, for the same reason as the sweep band: it is stated with its
rationale, while the examples are arithmetic that silently omits it. The
consequence is that this function quotes a wider risk and a lower
risk-reward than the playbook's examples do for the same two setups. Measured,
with only the fixed 0.03% term binding: Example 1 grades 1:1.56 against the
playbook's 1:2.15, and Example 2 grades 1:1.81 against its 1:2.79. Both still
clear the 1.5 floor, which is the useful part - applying the rule the playbook
states does not invalidate the setups it illustrates.

(Example 2's 1:1.81 reflected the playbook's bias-adjusted "cluster" target.
That rule is gone since 2026-11-14 - the sweep uses no bias (Amar) - so T2 is
always the opposite side of the opening range. The rule had never fired live
anyway: it compared the classifier's bias against "BULLISH"/"BEARISH" while
the classifier writes lower case, from 2026-09-13 on. Measured with it fixed,
it would have removed 3 winners and no losers on 41 sessions.)
---------------------------------------------------------------------------
"""

import logging

from config import (
    MIN_RISK_REWARD,
    STOP_BUFFER_ATR_FACTOR,
    STOP_BUFFER_LOOKBACK_BARS,
    STOP_BUFFER_PCT,
)
from levels import HIGH

logger = logging.getLogger()

GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"


def stop_buffer(bars, upto_index, level):
    """
    The larger of a fixed fraction of price and half the recent 5-minute range.

    Two terms because either alone fails somewhere: the percentage is blind to
    how violently the last half hour has moved, and the range term collapses
    to nothing in a dead tape. `max` is the playbook's own wording.
    """
    fixed = level * STOP_BUFFER_PCT / 100.0
    window = bars[max(0, upto_index - STOP_BUFFER_LOOKBACK_BARS + 1) : upto_index + 1]
    if window:
        mean_range = sum(bar["high"] - bar["low"] for bar in window) / len(window)
    else:
        mean_range = 0.0
    buffer_points = max(fixed, mean_range * STOP_BUFFER_ATR_FACTOR)
    return round(buffer_points, 2), round(fixed, 2), round(mean_range, 2)


def build(event, bars, orb, vwap, stacked, previous_day=None):
    """
    Turn a confirmed sweep into a candidate with prices, or say why not.

    Returns (candidate, reason). Exactly one of the two is None.
    """
    side = event["side"]
    short = side == HIGH  # a swept high is faded short
    entry = event["entry_price"]
    extreme = event["sweep_extreme"]
    level = event["level"]

    buffer_points, fixed_term, range_term = stop_buffer(
        bars, event["sweep_index"], level
    )
    stop = extreme + buffer_points if short else extreme - buffer_points
    risk = abs(entry - stop)
    if risk <= 0:
        return None, "entry is already beyond the stop"

    orb_mid = (orb["high"] + orb["low"]) / 2 if orb else None

    # ---- T1: VWAP or the opening-range midpoint, whichever comes first ----
    t1_candidates = [
        ("VWAP", vwap),
        ("opening-range midpoint", orb_mid),
    ]
    reachable = [
        (name, value)
        for name, value in t1_candidates
        if value is not None and (value < entry if short else value > entry)
    ]
    if reachable:
        # "Whichever price reaches first" is the nearest one in the direction
        # of the trade.
        t1_name, t1 = min(
            reachable, key=lambda pair: abs(pair[1] - entry)
        )
    else:
        t1_name, t1 = None, None

    # ---- T2: the opposite side of the opening range - no bias adjustment ---
    # (the bias-adjusted cluster target was removed 2026-11-14; see the module
    # docstring)
    opposite = (orb["low"] if short else orb["high"]) if orb else None
    t2, t2_name = opposite, "opposite side of the opening range"

    if t2 is None:
        return None, "no opposite range level to target - opening range unknown"

    reward = abs(t2 - entry)
    if (t2 > entry) if short else (t2 < entry):
        return None, f"target {t2} is on the wrong side of entry {entry}"

    risk_reward = reward / risk

    # ---- T3: stacked-pool sweeps only -------------------------------------
    t3, t3_name = None, None
    if stacked and previous_day:
        far = previous_day.get("pd_low") if short else previous_day.get("pd_high")
        if far is not None and ((far < t2) if short else (far > t2)):
            t3, t3_name = float(far), "PDL" if short else "PDH"

    candidate = {
        "direction": "short" if short else "long",
        "entry": round(entry, 2),
        "entry_kind": "close",
        "stop": round(stop, 2),
        "risk": round(risk, 2),
        "stop_buffer": buffer_points,
        "stop_buffer_terms": {"fixed": fixed_term, "half_recent_range": range_term},
        "t1": round(t1, 2) if t1 is not None else None,
        "t1_name": t1_name,
        "t2": round(t2, 2),
        "t2_name": t2_name,
        "t3": round(t3, 2) if t3 is not None else None,
        "t3_name": t3_name,
        "reward": round(reward, 2),
        "risk_reward": round(risk_reward, 2),
        "stacked": [pool["name"] for pool in stacked],
    }

    if risk_reward < MIN_RISK_REWARD:
        return None, (
            f"risk-reward 1:{risk_reward:.2f} is below the {MIN_RISK_REWARD} "
            f"floor (risk {risk:.2f}, reward {reward:.2f})"
        )

    candidate["grade"], candidate["grade_reason"] = _grade(
        event, candidate, stacked
    )
    return candidate, None


def _grade(event, candidate, stacked):
    """
    A, B or C, and always the reason - the playbook requires C say why.

    A is the stacked-pool sweep with a clean reclaim: two pools taken at once
    and the close back inside arriving on the sweep bar itself rather than the
    one after it. That combination is what the playbook calls the
    highest-grade version of the setup.
    """
    clean = not event.get("rejection_on_next")
    if stacked and clean:
        return GRADE_A, (
            f"stacked pool ({', '.join(p['name'] for p in stacked)}) taken and "
            f"reclaimed on the sweep bar itself"
        )
    marginal = []
    if not clean:
        marginal.append("reclaim came on the bar after the sweep")
    if candidate["risk_reward"] < MIN_RISK_REWARD * 1.2:
        marginal.append(
            f"risk-reward 1:{candidate['risk_reward']:.2f} is close to the "
            f"{MIN_RISK_REWARD} floor"
        )
    if candidate["t1"] is None:
        marginal.append("no T1 between entry and the opposite side")
    if marginal:
        return GRADE_C, "; ".join(marginal)
    return GRADE_B, "single pool, clean reclaim"
