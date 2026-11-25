"""
The trend trade after entry - the DECIDED rule, "Previous-candle trail" (Amar 2026-11-22). PURE.

    stop      a 5-min CLOSE back inside the 15-min range (the stop line: ORH for a long, ORL for a short) - the live
              stop, unchanged; out at that close
    T1        entry + 2R, R = entry - (stop line - the playbook's buffer); 2 of 3 lots sold there (Amar 2026-11-22:
              3 lots for both playbooks, 2 at T1 - "only the lot size you need to change")
    to entry  once price is 1R in favour (a TOUCH), the stop moves to entry from the next candle
    trail     then the stop trails on the previous closed 5-min candle's low (high for a short), never back past
              entry; the protected stop is hit on a touch and fills at the candle's open if it opens through it
    order     within a candle: the protected stop first, then the range close, then T1, then the 1R test - so a
              candle that touches both the protected stop and T1 takes the stop
    15:10     the playbook does NOT close on its own at 15:10: the risk manager's time exit does, at the 15:10
              close, after these steps on that candle (the same price the replay used)

Replayed 2026-11-22 (trend-exits-2026-11-21/exits_grid.py, key edge|half|1R|prev5|2.0) on 166 candidates. This
port reproduces that replay trade for trade with half sold at T1, and P0's paths with 2 of 3 (checked before deploy).

steps() returns the record steps of one position through the newest candle, each on the candle whose CLOSE makes it
known: T1 / TRAIL / STOP on the candle that traded them; a stop move on the candle whose close set it (it applies
from the next one). The FIRST protected move carries the "stop to entry" message; later trail moves carry none.

A stop move is returned as soon as the candle that sets it is in `bars` (Amar 2026-11-25). Until then it came only
once the NEXT candle was in, so it went out one tick late: on 2026-11-25 the stop to entry set by the 10:55 candle
(known at 11:00) was sent at 11:07. The stops, prices and exits are unchanged - only when each move is known.
"""

T1_RATIO = 2.0
LOTS, T1_LOTS = 3, 2


def _ist_minute(ts):
    return ((int(ts) + 19800) % 86400) // 60


def steps(bars, entry_ts, side, entry, line, R, t1=None):
    """
    [{kind, candle_ts, price, lots?, stop?, first?}] after the entry candle, through the newest bar in `bars`
    (today's closed candles, oldest first). `side` LONG / SHORT; `line` the stop line; `R` the risk; `t1` = T1
    (entry + 2R if None). Ends at the exit, or open at the newest candle.
    """
    up = side == "LONG"
    sg = 1 if up else -1
    T1 = entry + sg * T1_RATIO * R if t1 is None else t1
    fav = lambda px: (px - entry) * sg  # noqa: E731
    i = next((n for n, b in enumerate(bars) if int(b["ts"]) == int(entry_ts)), None)
    if i is None:
        raise RuntimeError(f"the entry candle {entry_ts} is not in today's candles")
    out = []
    lots, prot, P, hit = LOTS, False, None, False
    for j in range(i + 1, len(bars)):
        b = bars[j]
        if prot:
            if (b["low"] <= P) if up else (b["high"] >= P):
                px = min(P, b["open"]) if up else max(P, b["open"])
                out.append({"kind": "TRAIL", "candle_ts": int(b["ts"]), "price": round(px, 2), "lots": lots,
                            "break_even": P == entry})
                return out
        elif (b["close"] < line) if up else (b["close"] > line):
            out.append({"kind": "STOP", "candle_ts": int(b["ts"]), "price": round(b["close"], 2), "lots": lots})
            return out
        if not hit and ((b["high"] >= T1) if up else (b["low"] <= T1)):
            hit = True
            out.append({"kind": "T1", "candle_ts": int(b["ts"]), "price": round(T1, 2), "lots": T1_LOTS})
            lots -= T1_LOTS
        reached = not prot and fav(b["high"] if up else b["low"]) >= R - 1e-9
        if _ist_minute(b["ts"]) >= 15 * 60 + 10:
            return out      # the time exit is the risk manager's (at this candle's close)
        # The stop for the NEXT candle is known at this close - return it now, on this candle, not once the next
        # candle is in (until 2026-11-25 it was, so every move went out one tick late).
        moved_from, first = P, False
        if reached:
            prot, P, first = True, entry, True
        if prot:
            v = b["low"] if up else b["high"]
            if (v > P) if up else (v < P):
                P = v
            if P != moved_from:
                out.append({"kind": "STOP MOVE", "candle_ts": int(b["ts"]), "stop": round(P, 2), "first": first})
    return out
