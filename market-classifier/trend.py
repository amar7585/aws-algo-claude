"""
The day trend: is price trading OUTSIDE today's opening range, and which side.
Inside the range is no trend (Amar, 2026-11-19, rule G).

WHY IT EXISTS (plan v3, agreed with Amar 2026-11-07/08). The classifier decides
the market type, and pattern-recorder alerts only in the trend's direction on a
trend day. regime / structure / bias could not say "trend day" in time: on
2026-11-06 - a clean up day that broke PDH and held it for an hour - regime read
volatile-expansion all day, bias went range-bound 10:45-11:30 (exactly the
hold), and structure reached "trending" only at 14:15. Those three are
unchanged; this is a separate answer stored beside them.

THE RULE (rule G, Amar 2026-11-19), on the index's CLOSED 5-minute candles
from the 09:30 candle on:

    up    on   a close above the opening-range high
          off  a close back below it - back inside the range
    down  on   a close below the opening-range low
          off  a close back above it - back inside the range

As one line: up off = ORH, down off = ORL. PDH / PDL no longer decide the
trend; they stay levels for pattern-recorder's alerts and the sweep playbook.

A close below the range low switches an up day off and a down day on in the
same candle. Inside the range there is no trend: the no-trend rules (the sweep
playbook, pattern-recorder's sweeps and rejections) are the ones that apply.
In Amar's words: "in no trend why we need to take trending trades ... why you
are forcefully taking trades if the price is inside 15min candle range".

HOW IT GOT HERE. T1 (2026-11-08) turned up on a close above BOTH PDH and the
opening-range high and off below the lower of the two; option C (2026-11-11)
moved the off line to the range low on a GAP day (the whole range above PDH),
after 2026-11-11 stayed "up" through a 330-point fall that never reached PDH.
Option F (2026-11-19) drops PDH from the ON test: on 2026-11-19 PDH sat 135
points above the range high, the range high broke at 09:45, and T1 turned up
only on the 14:00 row. When PDH is above the range, F turns on at the range high
and gives the trade the whole range - off at the range low. When PDH lies inside
or below the range, F is exactly option C. F was live 2026-11-19 17:24 ->
rule G the same evening, before any session ran on it: it held the trend "on"
with price back inside the range (85% of classifier rows on; 82 trend trades
entered inside the range over 86 sessions; 10 trend trades on the no-trend day
2026-11-13), which is what Amar rejected. strategy-trend-pullback uses the same
line as its stop, so "trend off" means one thing in both.

Full-ruleset replay (pattern-recorder/replay/out/validation-2026-11-19/, from the
deployed zips; trend chain + sweep playbook): trend side, 86 clean sessions -
rule G 165 trades, +1556 points, profit factor 1.90, max drawdown -336 (F:
273 / +3472 / 1.86 / -1340; C: 99 / +1963 / 2.09 / -976); losses on days that
did not end as trends -498 (F -1496, C -1516). Trend + sweep together, 34
sessions 09-30 -> 11-19: +1148, PF 2.11, drawdown -210 (F +1467 / 1.69 / -1252).
Known cost: a dip that closes back inside the range turns the trend off, so a
retest that reclaims the range high is judged on the no-trend branch (2026-11-19
10:30, +140 under F, not taken under G).

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

THE DAILY READ is still required and still raises when missing or stale, although
the rule no longer uses PDH / PDL: it is checked here so a broken daily feed
fails at the classifier, before alerts and playbooks run on it.
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
    """(up off, down off): the opening range itself - a close back inside it is no
    trend (rule G, 2026-11-19)."""
    return lv["orh"], lv["orl"]


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
