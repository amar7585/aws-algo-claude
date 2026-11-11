"""
The trend-day pullback setup, as a pure function of the alert and the bars.

    evaluate(detection, index_bars, daily) -> {"status": "candidate" | "stand_down", ...}

WHAT IT IS (Amar, 2026-11-09). On a trend day - the classifier's day_trend is up
or down - a key level (PDH/PDL/ORH/ORL) has been broken in the trend's direction
and price comes back to it and HOLDS. The trade is the hold, with the trend.
Reference session 2026-11-06: PDH broken 09:45, held 10:50-12:05, then +441 to
the close. It is NOT a sweep and NOT a fade: sweeps belong to the sweep
playbook on no-trend days.

THE RULES, all on the INDEX (the chart the levels are drawn on):
  setup   v2's C2 (retest / hold) or R1 (hold candle), in the trend's direction,
          at PDH/PDL/ORH/ORL (its own level, or one within NEAR of it)
  window  trigger candle closes 09:45-14:30
  entry   the index close of the trigger candle
  stop    S3 - exit on a 5-min close below min(PDH, ORH) for a long (above
          max(PDL, ORL) for a short): the line where the day trend turns off.
          On a GAP day - the whole opening range above PDH (below PDL) - that
          line is the opening-range low (high) instead: max(min(PDH, ORH), ORL)
          for a long, min(max(PDL, ORL), ORH) for a short. The same line as
          market-classifier's day_trend off line (Amar, 2026-11-11, option C).
          Nominal stop for the risk figure = that line minus the buffer.
  T1      the session extreme before the trigger candle; then stop to
          break-even on the remainder. None when the entry is already beyond it.
  T2      the remainder held to the 15:10 close.
No risk-reward gate: the replay found a 1.5 gate kept the worse setups.

EVERY GATE CHECK RUNS, even after one fails, so a stand-down names all its
reasons. MISSING DATA RAISES (fail loudly): an alert whose trigger candle,
opening range or daily levels are absent cannot be judged, and a quiet
stand-down would hide it.

Replay (78 sessions, 88 holds): on days that ended as trends that way, S3 was
never stopped before T1 (32 of 47 reached it); on days that did not, it lost.
The trend read itself is right about half the time - that, not the stop, is
this playbook's risk.
"""

import statistics

from config import (
    HOLD_FAMILIES,
    KEY_LEVELS,
    STOP_BUFFER_LOOKBACK_BARS,
    STOP_BUFFER_PCT,
    STOP_BUFFER_RANGE_FACTOR,
    WINDOW_FROM_MINUTE,
    WINDOW_TO_MINUTE,
)

UP, DOWN = "up", "down"
BAR_SECONDS = 300
IST_OFFSET_SECONDS = 19800
SESSION_OPEN_MINUTE = 9 * 60 + 15
ORB_END_MINUTE = 9 * 60 + 30


def ist_minute(ts):
    return ((int(ts) + IST_OFFSET_SECONDS) % 86400) // 60


def clock(ts):
    m = ist_minute(ts)
    return f"{m // 60:02d}:{m % 60:02d}"


def key_level(detection):
    """The alert's key level: its own if key, else the first key level within NEAR of it."""
    if detection.get("level") in KEY_LEVELS:
        return detection["level"]
    return next((x for x in detection.get("other_levels") or () if x in KEY_LEVELS), None)


def gate(detection):
    """Every reason this alert is not this playbook's setup (empty = it is)."""
    reasons = []
    trend, direction, family = detection.get("day_trend"), detection.get("direction"), detection.get("family")
    if trend not in (UP, DOWN):
        reasons.append(f"day trend is {trend!r} - this playbook runs on trend days only")
    elif direction != trend:
        reasons.append(f"{direction} alert against the {trend} day trend")
    if family not in HOLD_FAMILIES:
        reasons.append(f"{family} is not a hold ({'/'.join(HOLD_FAMILIES)}) - breaks and sweeps are not traded here")
    if key_level(detection) is None:
        reasons.append(f"at {detection.get('level')} - not PDH/PDL/ORH/ORL")
    close_minute = ist_minute(int(detection["trigger_ts"]) + BAR_SECONDS)
    if not WINDOW_FROM_MINUTE <= close_minute <= WINDOW_TO_MINUTE:
        reasons.append(f"trigger candle closes {clock(int(detection['trigger_ts']) + BAR_SECONDS)}, "
                       f"outside {clock(WINDOW_FROM_MINUTE * 60 - IST_OFFSET_SECONDS)}-"
                       f"{clock(WINDOW_TO_MINUTE * 60 - IST_OFFSET_SECONDS)}")
    return reasons


def levels(bars, daily):
    """PDH/PDL from the daily read, ORH/ORL from today's 09:15-09:25 index candles. Raises when absent."""
    if not isinstance(daily, dict) or daily.get("pd_high") is None or daily.get("pd_low") is None:
        raise RuntimeError("no daily read with pd_high / pd_low - PDH/PDL cannot be placed")
    if daily.get("stale"):
        raise RuntimeError(f"daily read for trade_date {daily.get('trade_date')} is stale - "
                           f"its pd_high / pd_low would be an older session's")
    orb = [b for b in bars if SESSION_OPEN_MINUTE <= ist_minute(b["ts"]) < ORB_END_MINUTE]
    if sorted(ist_minute(b["ts"]) for b in orb) != [SESSION_OPEN_MINUTE, SESSION_OPEN_MINUTE + 5, SESSION_OPEN_MINUTE + 10]:
        raise RuntimeError(f"opening range incomplete: index candles at {[clock(b['ts']) for b in orb]}")
    return {"pdh": float(daily["pd_high"]), "pdl": float(daily["pd_low"]),
            "orh": max(float(b["high"]) for b in orb), "orl": min(float(b["low"]) for b in orb)}


def evaluate(detection, index_bars, daily):
    reasons = gate(detection)
    if reasons:
        return {"status": "stand_down", "reasons": reasons}

    bars = sorted(({k: (int(v) if k == "ts" else float(v)) for k, v in b.items() if k in ("ts", "open", "high", "low", "close")}
                   for b in index_bars), key=lambda b: b["ts"])
    trigger_ts = int(detection["trigger_ts"])
    i = next((n for n, b in enumerate(bars) if b["ts"] == trigger_ts), None)
    if i is None:
        raise RuntimeError(f"the trigger candle {clock(trigger_ts)} is not in index_bars "
                           f"({len(bars)} candles, last {clock(bars[-1]['ts']) if bars else '-'})")
    lv = levels(bars, daily)
    up = detection["direction"] == UP
    sign = 1 if up else -1
    trigger = bars[i]
    entry = trigger["close"]
    key = key_level(detection)
    last = bars[max(0, i + 1 - STOP_BUFFER_LOOKBACK_BARS):i + 1]
    buffer = max(entry * STOP_BUFFER_PCT / 100,
                 STOP_BUFFER_RANGE_FACTOR * statistics.fmean(b["high"] - b["low"] for b in last))
    gap = lv["orl"] > lv["pdh"] if up else lv["orh"] < lv["pdl"]
    off_line = max(min(lv["pdh"], lv["orh"]), lv["orl"]) if up else min(max(lv["pdl"], lv["orl"]), lv["orh"])
    nominal = off_line - sign * buffer
    risk = (entry - nominal) * sign
    if risk <= 0:
        return {"status": "stand_down",
                "reasons": [f"entry {entry:.2f} is already beyond the trend-off line {off_line:.2f}"]}
    prior = bars[:i]
    t1 = (max(b["high"] for b in prior) if up else min(b["low"] for b in prior)) if prior else None
    t1_room = None if t1 is None else (t1 - entry) * sign
    if t1_room is not None and t1_room <= 0:
        t1, t1_room = None, None
    return {
        "status": "candidate",
        "side": "LONG" if up else "SHORT",
        "trigger_ts": trigger_ts,
        "entry": entry,
        "key_level": key,
        "key_price": lv[key],
        "levels": lv,
        "off_line": off_line,
        "gap": gap,
        "off_line_name": (("ORL" if gap else "PDH" if lv["pdh"] <= lv["orh"] else "ORH") if up
                          else ("ORH" if gap else "PDL" if lv["pdl"] >= lv["orl"] else "ORL")),
        "buffer": buffer,
        "stop_nominal": nominal,
        "risk": risk,
        "t1": t1,
        "t1_room": t1_room,
        "rr": None if t1_room is None else t1_room / risk,
    }
