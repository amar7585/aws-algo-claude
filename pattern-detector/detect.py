"""
The two-clock turn detection. EVERY THRESHOLD IS PROVISIONAL (see config.py).

SAME-TICK CLOCK: an ABNORMAL-VOLUME REVERSAL candle on the future - a bar whose
volume is |z| >= VOLUME_Z_MIN against its recent baseline (two-sided: the
anomaly can be a spike OR a collapse) and whose shape reverses - a marginal new
low closing bullish is an up-turn, a marginal new high closing bearish a
down-turn. This fired on 5/5 observed turns.

+1-TICK CLOCK: the turn is CONFIRMED one snapshot later by the futures buildup
turning the right way OR the relevant option OI moving. "buildup OR option-OI"
confirmed 5/5; buildup alone 4/5, so option OI is the second leg. Confirmation
is read off the CURRENT snapshot (the fno deltas and the sentiment buildup); the
candidate off the future bars one interval back. A turn fires ONLY when a
candidate is confirmed.

LEVEL PROXIMITY is ANNOTATED, NOT a gate: both 2026-09-18 lows sat on the 23300
max-OI-put / max-pain wall, but a turn away from a wall is logged with its
distance rather than dropped, so the premise stays measurable.

STATELESS: the candidate is re-derived from the stored future bars each run;
no pending turn is remembered between invokes.
"""

import logging
import statistics

from config import (
    CANDIDATE_WINDOW_SECONDS,
    LEVEL_EPS_PCT,
    OI_CONFIRM_PCT,
    VOLUME_LOOKBACK,
    VOLUME_Z_MIN,
)

logger = logging.getLogger()

UP = "up"       # a low reversing up
DOWN = "down"   # a high reversing down


def _volume_z(target_volume, baseline_volumes):
    """Two-sided z of one bar's volume against the preceding bars, or None."""
    if len(baseline_volumes) < 2:
        return None
    sd = statistics.pstdev(baseline_volumes)
    if sd == 0:
        return None
    return (target_volume - statistics.fmean(baseline_volumes)) / sd


def _reversal_direction(bar, prior_bars):
    """
    up when the bar makes a marginal new low against the recent bars and closes
    bullish; down when it makes a marginal new high and closes bearish; else
    None. The new extreme is the sweep, the close-through is the reversal.
    """
    if not prior_bars:
        return None
    bullish = bar["close"] > bar["open"]
    bearish = bar["close"] < bar["open"]
    if bar["low"] <= min(b["low"] for b in prior_bars) and bullish:
        return UP
    if bar["high"] >= max(b["high"] for b in prior_bars) and bearish:
        return DOWN
    return None


def find_candidate(fut_bars, snapshot_ts):
    """
    The most anomalous reversal bar in the candidate window, or None.

    fut_bars is oldest-first, each {ts, open, high, low, close, volume}. The
    window is the bars that closed within CANDIDATE_WINDOW_SECONDS before the
    snapshot; each is scored on its own preceding baseline so a later bar's
    z is not contaminated by the anomaly itself.
    """
    window_start = snapshot_ts - CANDIDATE_WINDOW_SECONDS
    best = None
    for i, bar in enumerate(fut_bars):
        if not (window_start < bar["ts"] <= snapshot_ts):
            continue
        baseline = [b["volume"] for b in fut_bars[max(0, i - VOLUME_LOOKBACK):i]]
        z = _volume_z(bar["volume"], baseline)
        if z is None or abs(z) < VOLUME_Z_MIN:
            continue
        direction = _reversal_direction(bar, fut_bars[max(0, i - 3):i])
        if direction is None:
            continue
        if best is None or abs(z) > abs(best["volume_z"]):
            best = {
                "candidate_ts": bar["ts"],
                "direction": direction,
                "volume_z": round(z, 2),
                "volume": bar["volume"],
            }
    return best


def _confirmations(direction, fno, sentiment):
    """The +1-tick confirmations present on the CURRENT snapshot."""
    reasons = []
    buildup = sentiment.get("buildup")
    if direction == UP:
        if buildup == "LONG_BUILDUP":
            reasons.append("buildup LONG_BUILDUP")
        pe = fno.get("near_pe_oi_change_pct")
        if pe is not None and float(pe) <= -OI_CONFIRM_PCT:
            reasons.append(f"PE OI {float(pe):+.1f}%")
    else:  # DOWN
        if buildup == "LONG_UNWINDING":
            reasons.append("buildup LONG_UNWINDING")
        ce = fno.get("near_ce_oi_change_pct")
        if ce is not None and float(ce) <= -OI_CONFIRM_PCT:
            reasons.append(f"CE OI {float(ce):+.1f}%")
    return reasons


def _level_distance_pct(direction, fno):
    """Distance from spot to the nearest relevant wall, as % of spot, or None."""
    spot = fno.get("spot")
    if not spot:
        return None
    walls = (
        [fno.get("near_max_oi_put"), fno.get("near_max_pain")]
        if direction == UP
        else [fno.get("near_max_oi_call"), fno.get("near_max_pain")]
    )
    walls = [float(w) for w in walls if w is not None]
    if not walls:
        return None
    return round(min(abs(float(spot) - w) for w in walls) / float(spot) * 100, 3)


def detect(fno, sentiment, fut_bars):
    """
    The detection dict. `turn` is True only when a candidate is confirmed;
    level proximity is reported, not required.
    """
    snapshot_ts = int(fno["snapshot_ts"])
    candidate = find_candidate(fut_bars, snapshot_ts)
    if candidate is None:
        return {
            "turn": False,
            "reason": "no abnormal-volume reversal candle on the future",
        }
    reasons = _confirmations(candidate["direction"], fno, sentiment)
    level = _level_distance_pct(candidate["direction"], fno)
    return {
        "turn": bool(reasons),
        "direction": candidate["direction"],
        "candidate_ts": candidate["candidate_ts"],
        "volume_z": candidate["volume_z"],
        "confirmed_by": reasons,
        "level_distance_pct": level,
        "at_level": level is not None and level <= LEVEL_EPS_PCT,
        "reason": None if reasons else "reversal candle unconfirmed by buildup/option OI",
    }
