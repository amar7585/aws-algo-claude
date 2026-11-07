"""
pattern-recorder (pattern-detector v2) - STRUCTURE: the *where*.

The 15-minute bars, the swings, the named levels and the context attached to
every v2 event. Pure functions over plain bar lists: no database, no clock, no
I/O. The definitions are v2-pattern-definitions.md sections 2, 3 and 6; this
module is the exact form of them, and where the two disagree the document is
the one to argue with.

WHERE IT RUNS. In the pattern-recorder Lambda (handler.py), which
pattern-detector invokes as its last step, and in the offline replay
(replay/replay.py). pattern-detector's own detection does not use it.

ONE SERIES: THE FUTURE. Every shape, level and volume read here is on the
active future's own bars. Measured 2026-11-05: index and future 5-min candles
share a colour on only 80% of bars, and a future bullish engulfing is also one
on the index only 14% of the time, so the shape depends on which series is
read. Index bars are used for one thing only - converting option strikes (and
the index's own levels, recorded for comparison) into future prices.

BARS. Each bar is {ts, open, high, low, close, volume}; ts is epoch seconds of
the bar's START (candle_5min.candle_ts), oldest first, CLOSED bars only - the
caller enforces candle_ts + 300 <= captured_at, as db.read_bars already does.

NO LOOK-AHEAD. Everything an event at bar b uses was knowable when b started:
a swing counts only once the bar that confirms it has closed, the opening range
only from 09:30, an option wall only from the snapshot stamped at or before b.
That is what lets a stateless re-derivation on every run reach the same answer
for an old bar, and what lets a replay over stored bars reproduce live.
"""

import os

BAR_SECONDS = 300
BAR15_SECONDS = 900
IST_OFFSET_SECONDS = 19800
DAY_SECONDS = 86400

# ---- tunables. PROVISIONAL, env-overridable (definitions section 11) --------
# "Near a level", percent of the trigger bar's close. 0.05% is ~12 pts at 23,000,
# about the 25th percentile of a 5-min future bar's range (measured 2026-11-05),
# and the value the near-level counts in the definitions were measured at.
NEAR_PCT = float(os.environ.get("V2_NEAR_PCT", "0.05"))

# Fractal half-width for 15-min swings. k=2 gave 9.0 near-level candles a session
# against 14.7 for 5-min swings (measured 2026-11-05) - swings come from 15-min.
SWING_K = int(os.environ.get("V2_SWING_K", "2"))

# ---- fixed facts about the session, not tunables ----------------------------
SESSION_OPEN_MINUTE = 9 * 60 + 15
ORB_END_MINUTE = 9 * 60 + 30        # opening range = the 09:15 15-min bar
INDEX_FROZEN_MINUTE = 15 * 60 + 15  # NSE closing auction from 2026-08-03
LAST_LIVE_INDEX_MINUTE = 15 * 60 + 10

# Level names. Highs and lows of a pair are kept apart only for the record; a
# level's ROLE (support / resistance) comes from where price is, not its name.
PDH, PDL = "pdh", "pdl"
ORH, ORL = "orh", "orl"
SWING_HIGH, SWING_LOW = "swing_high", "swing_low"
CALL_WALL, PUT_WALL, MAX_PAIN = "call_wall", "put_wall", "max_pain"
INDEX_TWINS = (PDH, PDL, ORH, ORL)

SUPPORT, RESISTANCE = "support", "resistance"


# ---- time ------------------------------------------------------------------

def ist_minute(ts):
    """Minutes since IST midnight."""
    return ((int(ts) + IST_OFFSET_SECONDS) % DAY_SECONDS) // 60


def ist_day(ts):
    """An integer IST day number - equal for every bar of one session."""
    return (int(ts) + IST_OFFSET_SECONDS) // DAY_SECONDS


def ist_day_start(ts):
    """Epoch seconds of IST midnight of the bar's day."""
    return ist_day(ts) * DAY_SECONDS - IST_OFFSET_SECONDS


def clock(ts):
    m = ist_minute(ts)
    return f"{m // 60:02d}:{m % 60:02d}"


def near_points(price):
    return abs(float(price)) * NEAR_PCT / 100.0


def split_sessions(bars):
    """{ist_day: [bars oldest-first]} - one list per trading session."""
    sessions = {}
    for bar in bars:
        sessions.setdefault(ist_day(bar["ts"]), []).append(bar)
    for day in sessions:
        sessions[day].sort(key=lambda b: b["ts"])
    return sessions


# ---- 15-minute bars ----------------------------------------------------------

def build_15min(bars):
    """
    The session's 15-min bars, built from its 5-min bars three at a time.

    Built here rather than read from candle_15min so the trigger and the
    structure share ONE source, and a replay sees exactly what live saw. Since
    the loader fix (2026-10-22) candle_15min equals this aggregation on 300 of
    300 future bars; before it, on only 88%.

    The grid is IST 09:15, 09:30, ... ; 19800 is a multiple of 900, so epoch
    buckets are IST buckets. A 15-min bar exists ONLY when all three 5-min bars
    are present: a bucket with a missing bar would carry a false high, low or
    close, and since the inputs are closed bars, a complete bucket is closed.
    """
    by_bucket = {}
    for bar in bars:
        by_bucket.setdefault(bar["ts"] - bar["ts"] % BAR15_SECONDS, []).append(bar)
    out = []
    for bucket in sorted(by_bucket):
        group = sorted(by_bucket[bucket], key=lambda b: b["ts"])
        if [b["ts"] for b in group] != [bucket, bucket + BAR_SECONDS, bucket + 2 * BAR_SECONDS]:
            continue
        out.append({
            "ts": bucket,
            "open": group[0]["open"],
            "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group),
            "close": group[-1]["close"],
            "volume": sum(b["volume"] for b in group),
        })
    return out


# ---- swings ------------------------------------------------------------------

def find_swings(bars15, k=None):
    """
    Confirmed k-fractal swings on ONE session's 15-min bars.

    Bar t is a swing high when its high is ABOVE each of the k bars before it
    and AT OR ABOVE each of the k bars after it (the mirror for lows) - strict
    on the left so a flat top is dated to its first bar. It is KNOWN only once
    bar t+k has closed (known_at); before that it does not exist, which is the
    no-look-ahead rule. The 2k+1 bars must be contiguous: a missing bucket would
    otherwise make two bars 30 minutes apart look like neighbours.
    """
    k = SWING_K if k is None else k
    swings = []
    for t in range(k, len(bars15) - k):
        window = bars15[t - k:t + k + 1]
        if window[-1]["ts"] - window[0]["ts"] != 2 * k * BAR15_SECONDS:
            continue
        left, bar, right = window[:k], window[k], window[k + 1:]
        known_at = window[-1]["ts"] + BAR15_SECONDS
        if all(bar["high"] > b["high"] for b in left) and all(bar["high"] >= b["high"] for b in right):
            swings.append({"kind": "high", "ts": bar["ts"], "price": bar["high"], "known_at": known_at})
        if all(bar["low"] < b["low"] for b in left) and all(bar["low"] <= b["low"] for b in right):
            swings.append({"kind": "low", "ts": bar["ts"], "price": bar["low"], "known_at": known_at})
    return swings


def known_swings(swings, ts):
    """The swings already confirmed when a bar starting at ts opened, oldest first."""
    return [s for s in swings if s["known_at"] <= ts]


def latest_swing(swings, kind, ts):
    """The newest confirmed swing of `kind` ("high"/"low") known by ts, or None."""
    known = [s for s in known_swings(swings, ts) if s["kind"] == kind]
    return known[-1] if known else None


def structure_15m(swings, ts):
    """
    The 15-min trend read from the last two confirmed swing highs and lows:
    HH+HL = up, LH+LL = down, anything else mixed, fewer than two of either
    none. Our own read on the future - the classifier's `structure` is on other
    bars and is deliberately not reused.
    """
    known = known_swings(swings, ts)
    highs = [s["price"] for s in known if s["kind"] == "high"][-2:]
    lows = [s["price"] for s in known if s["kind"] == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "none"
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return "up"
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return "down"
    return "mixed"


# ---- levels ------------------------------------------------------------------

def day_levels(prev_session_bars, session_bars):
    """
    The fixed future-native levels of a session: PDH/PDL and the opening range.

    PDH/PDL are the previous session's high/low of the SAME CONTRACT - the
    caller passes that contract's bars. Futures trade to 15:40 but nothing
    stores bars after the 15:25 bar, so the last ten minutes are not in them.
    The opening range is the 09:15, 09:20 and 09:25 bars; it exists only when
    all three do, and only from 09:30.

    Returns [{name, price, known_from}] where known_from is the earliest bar
    START that may use the level. For PDH/PDL that is the session's open; the
    crossing history for them may also look at the previous session's last
    close (a gap through PDH at the open is a break).
    """
    levels = []
    if prev_session_bars:
        start = session_bars[0]["ts"] if session_bars else None
        levels.append({"name": PDH, "price": max(b["high"] for b in prev_session_bars), "known_from": start})
        levels.append({"name": PDL, "price": min(b["low"] for b in prev_session_bars), "known_from": start})
    orb = [b for b in session_bars if SESSION_OPEN_MINUTE <= ist_minute(b["ts"]) < ORB_END_MINUTE]
    if len(orb) == 3:
        known_from = orb[0]["ts"] + BAR15_SECONDS
        levels.append({"name": ORH, "price": max(b["high"] for b in orb), "known_from": known_from})
        levels.append({"name": ORL, "price": min(b["low"] for b in orb), "known_from": known_from})
    return levels


def basis_at(ts, fut_close_by_ts, index_close_by_ts):
    """
    Future minus index at the bar starting ts, or None.

    From 15:15 the index is frozen by the closing auction while the future
    trades on, so the difference there is not a basis; the 15:10 bar's is used
    instead. Measured 2026-11-05: the basis moves 35-54 pts in a session and
    sd 7 pts bar to bar, so a strike converted with a stale basis lands wrong.
    """
    if ist_minute(ts) >= INDEX_FROZEN_MINUTE:
        ts = ist_day_start(ts) + LAST_LIVE_INDEX_MINUTE * 60
    fut = fut_close_by_ts.get(ts)
    idx = index_close_by_ts.get(ts)
    if fut is None or idx is None:
        return None
    return float(fut) - float(idx)


def wall_row_at(fno_rows, ts):
    """The latest fno snapshot stamped at or before ts (rows oldest-first), or None."""
    row = None
    for candidate in fno_rows:
        if candidate["snapshot_ts"] <= ts:
            row = candidate
        else:
            break
    return row


def wall_strikes(fno_row):
    """{name: strike} for the option walls and max pain on one fno row."""
    out = {}
    for name, key in ((CALL_WALL, "near_max_oi_call"), (PUT_WALL, "near_max_oi_put"),
                      (MAX_PAIN, "near_max_pain")):
        value = fno_row.get(key) if fno_row else None
        if value is not None:
            out[name] = float(value)
    return out


def wall_known_from(fno_rows, name, strike, ts):
    """
    The earliest snapshot_ts in the unbroken run of rows, ending at the row in
    force at ts, that carry this same strike for this wall. A wall that moved
    from 23300 to 23350 at 11:00 is a NEW level at 11:00 - crossings of the old
    strike are not its history.
    """
    key = {CALL_WALL: "near_max_oi_call", PUT_WALL: "near_max_oi_put", MAX_PAIN: "near_max_pain"}[name]
    rows = [r for r in fno_rows if r["snapshot_ts"] <= ts]
    start = None
    for row in reversed(rows):
        value = row.get(key)
        if value is None or float(value) != strike:
            break
        start = row["snapshot_ts"]
    return start


def role(level_price, prev_close):
    """Support when the previous bar closed above the level, resistance below."""
    if prev_close > level_price:
        return SUPPORT
    if prev_close < level_price:
        return RESISTANCE
    return None


# ---- tests of a level ----------------------------------------------------------

def tested(bar15, level_price, near, direction):
    """
    Did this 15-min bar TEST the level from the side an event in `direction`
    reverses off? For an up event (the level is support): its low came within
    `near` of the level, or went further below and the bar still closed above
    it - a sweep is a test. Merely trading through the level is not a test.
    """
    if direction == "up":
        low = bar15["low"]
        return low <= level_price + near and (low >= level_price - near or bar15["close"] > level_price)
    high = bar15["high"]
    return high >= level_price - near and (high <= level_price + near or bar15["close"] < level_price)


def prior_tests(session_bars15, level_price, trigger_ts, near, direction, since_ts):
    """
    How many times this level was TESTED earlier this session, before the test
    the event itself belongs to.

    A test is a run of consecutive 15-min bars that each test() the level. Only
    bars from the level's ORIGIN (since_ts: a swing's own bar, 09:15 for the
    opening range and PDH/PDL, the first snapshot carrying a wall's strike) and
    closed before the trigger bar opened count - touches before a level existed
    are not part of its pattern. The run that ends at the bucket immediately
    before the trigger's own bucket is the CURRENT test, not a prior one (a sweep
    at 10:10 reclaimed at 10:20 is one test), so it is not counted.

    The bar that DEFINES a level is its first test: a swing low's own bar, the
    09:15 bar for the opening low. So a later rejection at a swing low reads
    prior_tests = 1, which is what labels it a double bottom.
    """
    trigger_bucket = trigger_ts - trigger_ts % BAR15_SECONDS
    closed = [b for b in session_bars15
              if b["ts"] >= since_ts and b["ts"] + BAR15_SECONDS <= trigger_ts]
    runs = []  # [first_ts, last_ts]
    for bar in closed:
        if tested(bar, level_price, near, direction):
            if runs and bar["ts"] - runs[-1][1] == BAR15_SECONDS:
                runs[-1][1] = bar["ts"]
            else:
                runs.append([bar["ts"], bar["ts"]])
    if runs and runs[-1][1] == trigger_bucket - BAR15_SECONDS:
        runs.pop()
    return len(runs)


def chart_pattern(direction, tests_before):
    """The double / triple label for a reversal at an already-tested level."""
    if tests_before <= 0:
        return None
    side = "bottom" if direction == "up" else "top"
    if tests_before == 1:
        return f"double_{side}"
    if tests_before == 2:
        return f"triple_{side}"
    return f"multi_test_{side}"
