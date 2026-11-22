"""
What happens to a sweep trade after its entry - the NEW RULES (2026-11-14).

Measured on 104 sessions before it was written (Amar's step 1). Every exit on
elapsed time or slow progress did WORSE than holding on no-trend days, because
the winners there are slow and most dip first. What held up was an exit taken
when price says the sweep has failed, with no number in it:

    EXIT       a candle closes back beyond the swept level - out, and WATCHING
    RE-ENTRY   while watching, a candle closes beyond the best close the trade
               made before the exit (a new best close the trade's way) - back
               in at that close, same structural stop and T2. Only when a new
               entry would be allowed on that candle (the caller says).
    ENDS       the stop trades (STOP HIT, or WATCH ENDED while out), T2 trades
               (TARGET REACHED, or WATCH ENDED while out), the re-entry is not
               allowed when it comes, or 15:00 passes while watching.
    CLOSED AT 15:15
               a trade still in after the 15:10 candle is closed at that
               candle's close - the 15:15 price (Amar 2026-11-20). From 15:15
               the closing auction freezes the index and its 15:25 candle is
               one jump to the official close, a price nobody traded, so no
               stop, target or exit is judged on a candle from 15:15 on.
               config.LAST_LIVE_INDEX_BAR says why.

On no-trend days this netted the same as holding (+28.0R vs +25.8R over 49
ideas) with far fewer full-size losses (9 vs 24). One exit and one re-entry
per trade, as measured: a re-entered trade holds to its stop or T2.

T1 (Amar 2026-11-22, "2 of 3"). Enter 3 lots; when T1 - the opening-range
midpoint - trades, book 2 of them there (T1 REACHED). The last lot carries on
under every rule above, unchanged: the stop never moves, T2 is the same, and
the exit, the re-entry and the 15:15 close apply to what is still open. No T1
when the entry is already past the midpoint (trade.build): all 3 lots run to
T2. A re-entered position gets T1 only when it was not booked before and the
midpoint is still ahead of the re-entry close. T1 never ends a trade, so the
trade's last step - and the register's outcome - stays one of the kinds above.
Replayed before it was built (110 sessions, deployed code TRtj+AW8, only this
function swapped): gate as live 6 trades +5.10R, 6 won (all to T2: +6.85R,
5 won); gate off 24 trades +8.15R, 14 won (+10.48R, 10 won). config.LOTS has
every split measured.

PURE. A trade's whole path is a function of the bars, so it is re-derived on
every run - nothing is stored, and a re-run reaches the same steps.

Within one candle the stop is checked first, then T2, then T1, then the close:
the same conservative order as sweep.outcome, with T1 after T2 as replayed (a
candle that trades both is a T2 for the whole position - it never happened in
the 110 replayed sessions).
"""

import gate as gate_lib
from config import LAST_LIVE_INDEX_BAR, LOTS, T1_LOTS_BOOKED
from levels import HIGH, ist_time

ENTRY = "ENTRY"
T1 = "T1 REACHED"
EXIT = "EXIT"
REENTRY = "RE-ENTRY"
WATCH_ENDED = "WATCH ENDED"
TARGET = "TARGET REACHED"
STOP = "STOP HIT"
AUCTION_CLOSE = "CLOSED AT 15:15"

# Read with the step's price: "... at 15:15 - out at 25139.95 (entry ...)" - the 15:10 candle's close.
_AUCTION_REASON = "the closing auction freezes the index at 15:15 - out"

OPEN, WATCHING, REENTERED, ENDED = "open", "watching", "re-entered", "ended"


def follow(bars, event, candidate, reentry_allowed):
    """
    The steps of one trade after its confirming candle, oldest first.

    Each step: {"kind", "index", "price", "reason"}. `reentry_allowed(i)`
    returns (allowed, reason) for candle i. Also returns the state after the
    newest candle and the index the trade ended on (None while it is alive).
    """
    side, level = event["side"], event["level"]
    short = side == HIGH
    toward = -1 if short else 1          # the trade's favourable direction
    stop, t2 = candidate["stop"], candidate["t2"]
    start = event["confirm_index"]
    steps, state, best = [], OPEN, None

    def stop_traded(bar):
        return bar["high"] >= stop if short else bar["low"] <= stop

    def target_traded(bar):
        return bar["low"] <= t2 if short else bar["high"] >= t2

    def back_beyond(bar):
        return bar["close"] > level if short else bar["close"] < level

    # T1 - the range midpoint, only when it lies ahead of the entry (trade.build).
    t1, mid, booked = candidate.get("t1"), candidate.get("range_mid"), False

    def t1_traded(bar):
        return bar["low"] <= t1 if short else bar["high"] >= t1

    for i in range(start + 1, len(bars)):
        bar = bars[i]
        # The 15:10 candle, or the first candle after it should 15:10 be missing.
        last_live = ist_time(bar["ts"]) >= LAST_LIVE_INDEX_BAR
        if state in (OPEN, REENTERED):
            if stop_traded(bar):
                steps.append({"kind": STOP, "index": i, "price": stop, "reason": "the stop traded"})
                return steps, ENDED, i
            if target_traded(bar):
                steps.append({"kind": TARGET, "index": i, "price": t2, "reason": "T2 traded"})
                return steps, ENDED, i
            if not booked and t1 is not None and t1_traded(bar):
                # 2 of 3 lots out at T1; the last lot carries on - same stop, same T2.
                steps.append({"kind": T1, "index": i, "price": t1,
                              "reason": f"the range midpoint traded - {T1_LOTS_BOOKED} of {LOTS} lots booked, "
                                        f"the last lot runs to T2, stop unchanged"})
                booked = True
            if state == OPEN and back_beyond(bar):
                best = max(toward * b["close"] for b in bars[start:i + 1]) * toward
                steps.append({"kind": EXIT, "index": i, "price": bar["close"],
                              "reason": f"closed back beyond the level {level:.2f}", "watch_for": best})
                state = WATCHING
            if last_live:
                # Still in after the last candle the index trades: out at its close.
                # Exited on this very candle: the watch ends here too, not on a
                # frozen candle (no re-entry is allowed after 15:00 anyway).
                if state == WATCHING:
                    steps.append({"kind": WATCH_ENDED, "index": i, "price": bar["close"],
                                  "reason": "the closing auction freezes the index at 15:15 - "
                                            "the watch ends with no re-entry"})
                else:
                    steps.append({"kind": AUCTION_CLOSE, "index": i, "price": bar["close"],
                                  "reason": _AUCTION_REASON})
                return steps, ENDED, i
            continue
        # ---- watching ------------------------------------------------------
        if stop_traded(bar):
            steps.append({"kind": WATCH_ENDED, "index": i, "price": stop,
                          "reason": "the stop traded while out - the idea failed"})
            return steps, ENDED, i
        if target_traded(bar):
            steps.append({"kind": WATCH_ENDED, "index": i, "price": t2,
                          "reason": "T2 traded while out - the move happened without a re-entry"})
            return steps, ENDED, i
        if gate_lib.scan_window(bar["ts"] + 300) == gate_lib.WINDOW_CLOSED:
            steps.append({"kind": WATCH_ENDED, "index": i, "price": bar["close"],
                          "reason": "15:00 passed with no re-entry"})
            return steps, ENDED, i
        if toward * bar["close"] > toward * best:
            allowed, why = reentry_allowed(i)
            if not allowed:
                steps.append({"kind": WATCH_ENDED, "index": i, "price": bar["close"],
                              "reason": f"a new best close came, but a re-entry is not allowed: {why}"})
                return steps, ENDED, i
            steps.append({"kind": REENTRY, "index": i, "price": bar["close"],
                          "reason": f"a new best close the trade's way (beyond {best:.2f})"})
            state = REENTERED
            if not booked:
                # The re-entered position gets T1 only while the midpoint is still ahead of it.
                ahead = mid is not None and (mid < bar["close"] if short else mid > bar["close"])
                t1 = mid if ahead else None
    return steps, state, None
