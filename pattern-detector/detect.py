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
    CONT_VOLUME_Z_MIN,
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


# ============================================================================
# CONTINUATION / BREAKOUT branch - a SECOND vocabulary beside the reversal turn.
#
# A reversal is a sweep-and-reverse: the close is AGAINST the new extreme. A
# CONTINUATION is the opposite shape - a close THROUGH a named level in the
# break direction - which _reversal_direction returns None for, so the two never
# collide. PROVISIONAL, n=1 (2026-09-21). See config.py.
#
# It emits ALERTS, not a manager dispatch: a fresh KEY-LEVEL BREAK (fires on the
# break bar, retest or not) and a RETEST-HOLD ENTRY (fires on the resumption
# bar). "Fresh" = the trigger bar closed within the last snapshot interval, so a
# stateless re-derivation announces each event once rather than every 15 min.
#
# The level is a GATE (a continuation is defined by a level). The OI/PCR/buildup
# is ANNOTATED on the alert as `confirmed_by`, not required - during observation
# every real break is reported with whether the data agreed, the same reason the
# reversal branch reports rejections.
# ============================================================================


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key_levels(fno, daily):
    """
    The named breakout levels, split by side. Highs are broken UP, lows DOWN.
    orb_high/orb_low are the 15-min opening range (fno row); pd_high/pd_low the
    previous day's extremes (daily read), absent when daily is None.
    """
    highs = {"orb_high": _num(fno.get("orb_high"))}
    lows = {"orb_low": _num(fno.get("orb_low"))}
    if daily:
        highs["pd_high"] = _num(daily.get("pd_high"))
        lows["pd_low"] = _num(daily.get("pd_low"))
    highs = {name: lvl for name, lvl in highs.items() if lvl is not None}
    lows = {name: lvl for name, lvl in lows.items() if lvl is not None}
    return highs, lows


def _find_break(fut_bars, i, highs, lows):
    """
    Is bar i a fresh CLOSE-THROUGH of a key level? Up: the prior bar closed at or
    below a high level and bar i closes above it, bullish. Down: prior bar closed
    at or above a low level and bar i closes below it, bearish. The prior-bar test
    is what makes it a CROSSING (fired once) rather than any bar sitting beyond a
    level. Returns (direction, level_name, level_price) for the nearest level
    crossed, or None.
    """
    if i == 0:
        return None
    bar, prev = fut_bars[i], fut_bars[i - 1]
    if bar["close"] > bar["open"]:
        crossed = [(name, lvl) for name, lvl in highs.items()
                   if prev["close"] <= lvl < bar["close"]]
        if crossed:
            name, lvl = max(crossed, key=lambda nl: nl[1])  # the highest level cleared
            return (UP, name, lvl)
    if bar["close"] < bar["open"]:
        crossed = [(name, lvl) for name, lvl in lows.items()
                   if prev["close"] >= lvl > bar["close"]]
        if crossed:
            name, lvl = min(crossed, key=lambda nl: nl[1])  # the lowest level cleared
            return (DOWN, name, lvl)
    return None


def _find_retest_entry(bars, break_index, direction, level):
    """
    After a break at break_index, the retest reclaim entry, or None. Operates on
    the INDEX bars, because the level is a spot level.

    A breakout-retest is a DIP back through the broken level followed by a
    RECLAIM. After the break, price first closes back on the FAR side of the level
    - the retest of it as support (up) or resistance (down). The ENTRY is the
    first later bar that closes back ACROSS the level in the break direction: that
    reclaim bar is the entry ("entry on retest"). The entry is timed to the CLOSE
    of that bar; because price is scanned every 15 minutes, it is announced at the
    first snapshot after that bar closes - not on the far high/low it eventually
    reaches, which is what put an earlier version a whole scan late.

    A pure continuation that never dips back through the level has no reclaim, so
    this returns None - correct, that is the no-retest case, whose entry is
    deliberately deferred (no data yet).
    """
    dipped = False
    dip_ts = None
    for j in range(break_index + 1, len(bars)):
        bar = bars[j]
        if direction == UP:
            if bar["close"] < level:
                dipped, dip_ts = True, bar["ts"]
            elif dipped and bar["close"] > level:
                return {"ts": bar["ts"], "price": round(bar["close"], 2),
                        "retest_ts": dip_ts}
        else:
            if bar["close"] > level:
                dipped, dip_ts = True, bar["ts"]
            elif dipped and bar["close"] < level:
                return {"ts": bar["ts"], "price": round(bar["close"], 2),
                        "retest_ts": dip_ts}
    return None


def _continuation_confirmations(direction, fno, sentiment, prev_pcr):
    """
    The same-tick OI/PCR/buildup agreement for a continuation - ANNOTATED, not a
    gate. Buildup that is bullish-continuation (SHORT_COVERING/LONG_BUILDUP for
    up), PCR moving the break's way against the prior snapshot, and the far-side
    option OI unwinding. n=1: 2026-09-21 up-break read SHORT_COVERING + PCR
    1.12->1.20 + CE OI unwinding.
    """
    reasons = []
    buildup = sentiment.get("buildup")
    pcr = _num(fno.get("near_pcr_oi"))
    if direction == UP:
        if buildup in ("SHORT_COVERING", "LONG_BUILDUP"):
            reasons.append(f"buildup {buildup}")
        if prev_pcr is not None and pcr is not None and pcr > prev_pcr:
            reasons.append(f"PCR rising {prev_pcr:.2f}->{pcr:.2f}")
        ce = _num(fno.get("near_ce_oi_change_pct"))
        if ce is not None and ce <= -OI_CONFIRM_PCT:
            reasons.append(f"CE OI {ce:+.1f}%")
    else:
        if buildup in ("SHORT_BUILDUP", "LONG_UNWINDING"):
            reasons.append(f"buildup {buildup}")
        if prev_pcr is not None and pcr is not None and pcr < prev_pcr:
            reasons.append(f"PCR falling {prev_pcr:.2f}->{pcr:.2f}")
        pe = _num(fno.get("near_pe_oi_change_pct"))
        if pe is not None and pe <= -OI_CONFIRM_PCT:
            reasons.append(f"PE OI {pe:+.1f}%")
    return reasons


def _future_volume_z(fut_bars, ts):
    """
    Two-sided volume z of the FUTURE bar at ts, against its VOLUME_LOOKBACK
    predecessors. The break is timed on the index, but the abnormal-volume tell
    is the future's - it carries the real traded volume - so the two series are
    aligned by timestamp. None when the future has no bar at that instant.
    """
    index = next((i for i, bar in enumerate(fut_bars) if bar["ts"] == ts), None)
    if index is None:
        return None
    baseline = [b["volume"] for b in fut_bars[max(0, index - VOLUME_LOOKBACK):index]]
    return _volume_z(fut_bars[index]["volume"], baseline)


def detect_continuation(fno, sentiment, index_bars, fut_bars, daily,
                        prev_snapshot_ts, prev_pcr):
    """
    The list of FRESH continuation alerts on this snapshot - a `level_break` when
    a key level is broken on abnormal volume, and a `retest_entry` when a break's
    retest holds and resumes. Empty on most snapshots, by design: only events
    whose trigger bar closed within the last snapshot interval are returned, so a
    stateless re-run does not re-announce a break it already announced.

    The BREAK and the RETEST are read on the INDEX bars, because the levels are
    spot levels; the abnormal-volume tell is the FUTURE's, aligned by timestamp,
    because the future carries the real traded volume and a basis that would
    misplace a spot level if the break were measured on it directly.
    """
    snapshot_ts = int(fno["snapshot_ts"])
    fresh_after = int(prev_snapshot_ts) if prev_snapshot_ts else snapshot_ts - 900
    highs, lows = _key_levels(fno, daily)
    if not highs and not lows:
        return []

    def fresh(ts):
        return fresh_after < ts <= snapshot_ts

    alerts = []
    for i, bar in enumerate(index_bars):
        broken = _find_break(index_bars, i, highs, lows)
        if broken is None:
            continue
        direction, level_name, level = broken
        z = _future_volume_z(fut_bars, bar["ts"])
        if z is None or abs(z) < CONT_VOLUME_Z_MIN:
            continue
        confirmed_by = _continuation_confirmations(direction, fno, sentiment, prev_pcr)
        if fresh(bar["ts"]):
            alerts.append({
                "kind": "level_break",
                "direction": direction,
                "level": level_name,
                "level_price": round(level, 2),
                "break_ts": bar["ts"],
                "volume_z": round(z, 2),
                "confirmed_by": confirmed_by,
            })
        entry = _find_retest_entry(index_bars, i, direction, level)
        if entry is not None and fresh(entry["ts"]):
            alerts.append({
                "kind": "retest_entry",
                "direction": direction,
                "level": level_name,
                "level_price": round(level, 2),
                "break_ts": bar["ts"],
                "retest_ts": entry["retest_ts"],
                "entry_ts": entry["ts"],
                "entry_price": entry["price"],
                "volume_z": round(z, 2),
                "confirmed_by": confirmed_by,
            })
    return alerts
