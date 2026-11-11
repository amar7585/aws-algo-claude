"""
The day trend: has today become a trend day yet, and which way.

WHY IT EXISTS (plan v3, agreed with Amar 2026-11-07/08). The classifier decides
the market type, and pattern-recorder alerts only in the trend's direction on a
trend day. regime / structure / bias could not say "trend day" in time: on
2026-11-06 - a clean up day that broke PDH and held it for an hour - regime read
volatile-expansion all day, bias went range-bound 10:45-11:30 (exactly the
hold), and structure reached "trending" only at 14:15. Those three are
unchanged; this is a separate answer stored beside them.

THE RULE (T1), on the index's CLOSED 5-minute candles from the 09:30 candle on:

    up    on   a close above BOTH PDH and the opening-range high
          off  a close below BOTH of them (below the lower of the two) -
               or, on a GAP day, a close below the opening-range low
    down  on   a close below BOTH PDL and the opening-range low
          off  a close above BOTH of them (above the higher of the two) -
               or, on a GAP day, a close above the opening-range high

As one line: up off = max(min(PDH, ORH), ORL); down off = min(max(PDL, ORL), ORH).

A close that switches one trend off can switch the other on in the same candle.
Between "on" and "off" the state holds - a pullback that stays above the lower
of PDH / ORH keeps an up day up, which is what lets the 2026-11-06 PDH hold be
read as a with-trend long.

GAP DAYS (Amar, 2026-11-11, option C). When the whole opening range sits above
PDH (ORL > PDH), the lower of PDH / ORH is PDH, far below price: on 2026-11-11
the day stayed "up" through a 330-point fall that never reached PDH. On such a
day the trend turns off when price closes back below the opening range - it is
falling into the gap. Mirror for a range wholly below PDL. On every other day
the line is exactly the lower (higher) of the two, as before. strategy-trend-
pullback uses the same line as its stop, so "trend off" means one thing in both.
Replay (80 sessions to 2026-11-11): 19 gap days; the line changes the trend on 3
(07-24, 09-09, 11-11), where price closed back into the gap.

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

Measured on the 2026-11-08 replay (90 sessions): it is on at 1,002 of 2,160
classifier runs and never turns on in 25 sessions; on 2026-11-06 it turned up
with the 09:45 candle, so the 10:00 run is the first to show it.
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
    """(up off, down off): the lower of PDH / ORH (higher of PDL / ORL), or the far
    side of the opening range when the whole range sits beyond PDH (PDL) - a gap day."""
    up_off = max(min(lv["pdh"], lv["orh"]), lv["orl"])
    down_off = min(max(lv["pdl"], lv["orl"]), lv["orh"])
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

    up_on, down_on = max(lv["pdh"], lv["orh"]), min(lv["pdl"], lv["orl"])
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
