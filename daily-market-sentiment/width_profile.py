"""
The morning width profile: what a 5-minute candle's width NORMALLY is at each
clock slot, over the previous 20 sessions. Pure - no I/O.

Built once each morning for that day's session and stored in
algo.intraday_width_profile. intraday-market-sentiment reads it on every
15-minute run and judges its last six closed candles against it; the ratio is
what market-classifier's volatility test reads (agreed with Amar 2026-11-12,
plan choice A2: this function builds it at 09:35, before the first intraday
run at 09:45).

WIDTH IS (high - low) / close x 100, per candle - a percentage, so a normal
measured at 24,000 still means the same thing at 26,000.

PER CLOCK SLOT, BECAUSE THE DAY IS NOT FLAT. Over 103 sessions (2026-06-15 ..
11-11) the median 09:15 candle was 4.7x a midday one, 09:20-09:35 1.6-1.9x and
15:00 1.5x. A single average would call every morning volatile - the failure
this replaces.

THE LAST 20 WEEKDAY SESSIONS STRICTLY BEFORE THE SESSION. Today's own candles
are never in it, so a session is never measured against itself. A weekend
special session (the 2026-11-08 Muhurat Sunday) is not a normal session and is
left out. 20 rather than "every session stored": a normal taken from all
sessions since June sat below this autumn's market, so the median snapshot read
1.45x it; from the last 20 it reads 1.2x (measured 2026-11-12).

FAILS LOUDLY. Fewer than 20 sessions in the fetched window, or a slot that too
few of them have a candle at, raises: a short profile would make the intraday
ratio describe a different window than the one it claims to.
"""

import statistics

from neon_access import ist_datetime, ist_midnight_epoch

SLOT_SECONDS = 300
FIRST_SLOT = 9 * 3600 + 15 * 60     # 09:15, seconds after IST midnight
LAST_SLOT = 15 * 3600 + 25 * 60     # 15:25 - the last 5-min bucket of the session
SLOT_OFFSETS = tuple(range(FIRST_SLOT, LAST_SLOT + 1, SLOT_SECONDS))   # 75 slots


def width_pct(candle):
    """One candle's width: (high - low) / close x 100."""
    return (float(candle["high"]) - float(candle["low"])) / float(candle["close"]) * 100


def widths_by_session(candles, before_day):
    """
    {session date: {slot offset: width_pct}} for weekday sessions strictly
    before `before_day`. A candle is placed by its offset from its own day's
    IST midnight; anything off the 09:15 .. 15:25 five-minute grid (Dhan's stray
    post-close candles) is not a slot and is dropped.
    """
    sessions = {}
    for candle in candles:
        day = ist_datetime(candle["ts"]).date()
        if day >= before_day or day.weekday() >= 5:
            continue
        offset = int(candle["ts"]) - ist_midnight_epoch(day)
        if offset in SLOT_OFFSETS:
            sessions.setdefault(day, {})[offset] = width_pct(candle)
    return sessions


def build(candles, session_day, *, sessions=20, min_samples=15):
    """
    The profile for `session_day` from 5-minute `candles` covering the weeks
    before it. Returns (rows, used) - rows is one dict per slot, used is the
    list of session dates the medians were taken over (oldest first).

    Each row: slot_ts (the slot ON session_day, = that day's candle_ts),
    normal_width_pct (the median over the sessions that have that slot), and
    samples (how many did).
    """
    by_day = widths_by_session(candles, session_day)
    used = sorted(by_day)[-sessions:]
    if len(used) < sessions:
        raise RuntimeError(
            f"width profile for {session_day}: only {len(used)} weekday sessions "
            f"before it in the fetched candles, need {sessions} - widen "
            f"PROFILE_LOOKBACK_DAYS or check the fetch"
        )
    midnight = ist_midnight_epoch(session_day)
    rows = []
    thin = []
    for offset in SLOT_OFFSETS:
        values = [by_day[day][offset] for day in used if offset in by_day[day]]
        if len(values) < min_samples:
            thin.append((offset, len(values)))
            continue
        rows.append({
            "slot_ts": midnight + offset,
            "normal_width_pct": statistics.median(values),
            "samples": len(values),
        })
    if thin:
        raise RuntimeError(
            f"width profile for {session_day}: {len(thin)} slot(s) have fewer than "
            f"{min_samples} of {sessions} sessions with a candle - first "
            f"{ist_datetime(midnight + thin[0][0]):%H:%M} with {thin[0][1]}"
        )
    return rows, used
