"""
The day trend: which way today's opening range has broken, and whether that
break still holds (since option F, 2026-11-19 - before it: "has today become a
confirmed trend day yet").

WHY IT EXISTS (plan v3, agreed with Amar 2026-11-07/08). The classifier decides
the market type, and pattern-recorder alerts only in the trend's direction on a
trend day. regime / structure / bias could not say "trend day" in time: on
2026-11-06 - a clean up day that broke PDH and held it for an hour - regime read
volatile-expansion all day, bias went range-bound 10:45-11:30 (exactly the
hold), and structure reached "trending" only at 14:15. Those three are
unchanged; this is a separate answer stored beside them.

THE RULE (option F, Amar 2026-11-19), on the index's CLOSED 5-minute candles
from the 09:30 candle on:

    up    on   a close above the opening-range high
          off  a close below PDH when PDH lies inside the opening range;
               otherwise a close below the opening-range low
    down  on   a close below the opening-range low
          off  a close above PDL when PDL lies inside the opening range;
               otherwise a close above the opening-range high

As one line: up off = PDH if ORL <= PDH <= ORH else ORL;
             down off = PDL if ORL <= PDL <= ORH else ORH.

A close that switches one trend off can switch the other on in the same candle.
Between "on" and "off" the state holds - a pullback that stays above PDH (when
it is inside the range) or above the range low keeps an up day up, which is what
lets the 2026-11-06 PDH hold be read as a with-trend long.

HOW IT GOT HERE. T1 (2026-11-08) turned up on a close above BOTH PDH and the
opening-range high and off below the lower of the two; option C (2026-11-11)
moved the off line to the range low on a GAP day (the whole range above PDH),
after 2026-11-11 stayed "up" through a 330-point fall that never reached PDH.
Option F (2026-11-19) drops PDH from the ON test: on 2026-11-19 PDH sat 135
points above the range high, the range high broke at 09:45, and T1 turned up
only on the 14:00 row. When PDH is above the range, F turns on at the range high
and gives the trade the whole range - off at the range low. When PDH lies inside
or below the range, F is exactly option C. strategy-trend-pullback uses the same
line as its stop, so "trend off" means one thing in both.

Replay (pattern-recorder/replay/out/trend_rules_replay.md, 86 clean sessions
2026-07-01 -> 2026-11-19, the whole chain): T1/C turned on for 27 of 36
hindsight trend days (17 by 10:30), F for 36 (27 by 10:30). Trend playbook
+994 points over every candidate, +803 over the first per session, against
T1/C; better in Jul-Sep and in Oct-Nov. The cost, accepted: day_trend is on at
85% of classifier runs (46% under C) and in every session - it reads "the last
opening-range break", not "a confirmed trend day" - so pattern-recorder sends
fewer no-trend sweeps / rejections and more breaks / retests, and the playbook
carries wider stops (median risk 77 vs 61 points).

THE LEVELS.
  PDH / PDL     the daily read's pd_high / pd_low - the previous session's
                index high and low, carried in the payload from
                daily-market-sentiment. Measured 2026-11-08: equal to the
                previous session's 5-minute high / low on all 37 sessions that
                have a daily row. After a weekend Muhurat session that session
                is the previous one (Amar, 2026-11-09: the bars are kept, no
                special case).
  opening range the high / low of today's 09:15, 09:20 and 09:25 candles.

CLOSED CANDLES ONLY. The payload's session bars end with the candle still
forming at the run (at a 10:00 run the 10:00 candle has seconds in it), so a
candle counts only once its five minutes are over by the run clock. The 10:00
run judges up to the 09:55 candle - the same timing the replay measured.

STATELESS. Re-derived from 09:30 on every run, so a missed or retried run
cannot leave a wrong state behind.

FAILS LOUDLY (Amar, 2026-11-09). A missing or stale daily read, or an
incomplete opening range once the 09:30 candle has closed, raises: a stale
pd_high is a wrong level, not a missing one, and writing "none" would quietly
turn a trend day into a no-trend day for every alert after it.

On 2026-11-06 the first run, 09:45, already shows "up" under F (the 10:00 run
under T1); on 2026-11-19 the 10:00 run (the 14:00 run under T1).
"""

UP = "up"
DOWN = "down"
NONE = "none"

BAR_SECONDS = 300
IST_OFFSET_SECONDS = 19800
SESSION_OPEN_MINUTE = 9 * 60 + 15
RULE_FROM_MINUTE = 9 * 60 + 30   # the first candle after the opening range


def _ist_minute(ts):
    return ((int(ts) + IST_OFFSET_SECONDS) % 86400) // 60


def _levels(opening, daily):
    """PDH / PDL / ORH / ORL, raising on anything that would make them wrong."""
    if not isinstance(daily, dict):
        raise RuntimeError(
            "no daily read in the payload - day_trend needs the previous session's "
            "high and low (pd_high / pd_low from daily-market-sentiment)"
        )
    if daily.get("stale"):
        raise RuntimeError(
            f"daily read for trade_date {daily.get('trade_date')} is stale (written "
            f"{daily.get('created_at')}, before today) - its pd_high / pd_low would be "
            f"an older session's, so day_trend refuses to judge on them"
        )
    if daily.get("pd_high") is None or daily.get("pd_low") is None:
        raise RuntimeError(
            f"daily read carries no pd_high / pd_low - keys present: {sorted(daily)}"
        )
    minutes = sorted(_ist_minute(b["ts"]) for b in opening)
    if minutes != [SESSION_OPEN_MINUTE, SESSION_OPEN_MINUTE + 5, SESSION_OPEN_MINUTE + 10]:
        raise RuntimeError(
            f"opening range incomplete: expected the 09:15, 09:20 and 09:25 index "
            f"candles, got IST minutes {minutes}"
        )
    return {
        "pdh": float(daily["pd_high"]),
        "pdl": float(daily["pd_low"]),
        "orh": max(float(b["high"]) for b in opening),
        "orl": min(float(b["low"]) for b in opening),
    }


def off_lines(lv):
    """(up off, down off): PDH (PDL) when it lies inside the opening range, otherwise
    the far side of the range - its low for up, its high for down (option F)."""
    up_off = lv["pdh"] if lv["orl"] <= lv["pdh"] <= lv["orh"] else lv["orl"]
    down_off = lv["pdl"] if lv["orl"] <= lv["pdl"] <= lv["orh"] else lv["orh"]
    return up_off, down_off


def day_trend(session_bars, daily, run_epoch):
    """
    Today's day trend as of the run clock: (up | down | none, levels or None).

    session_bars  today's index 5-minute candles, oldest first (the payload's).
    daily         the daily read in force today (the payload's `daily`).
    run_epoch     the run wall clock, epoch seconds.

    Before the 09:30 candle has closed there is nothing to judge: "none", with
    no levels and no checks.
    """
    closed = sorted((b for b in session_bars if int(b["ts"]) + BAR_SECONDS <= int(run_epoch)),
                    key=lambda b: int(b["ts"]))
    judged = [b for b in closed if _ist_minute(b["ts"]) >= RULE_FROM_MINUTE]
    if not judged:
        return NONE, None
    opening = [b for b in closed if SESSION_OPEN_MINUTE <= _ist_minute(b["ts"]) < RULE_FROM_MINUTE]
    lv = _levels(opening, daily)

    up_on, down_on = lv["orh"], lv["orl"]
    up_off, down_off = off_lines(lv)
    state = NONE
    for bar in judged:
        close = float(bar["close"])
        if state == UP and close < up_off:
            state = NONE
        elif state == DOWN and close > down_off:
            state = NONE
        if state == NONE:
            if close > up_on:
                state = UP
            elif close < down_on:
                state = DOWN
    return state, lv
