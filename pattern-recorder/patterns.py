"""
pattern-recorder (pattern-detector v2) - PATTERNS: the *when*.

The 5-min candle shapes, the five pattern families and the volume flags, on the
active future. The definitions are v2-pattern-definitions.md sections 4, 5 and
7; this module is their exact form. structure.py supplies the *where*.

WHERE IT RUNS - see structure.py: the pattern-recorder Lambda and the replay.

RECOGNITION ONLY. An event says what formed, where, and what volume was doing
when it formed. It carries no entry, stop, target or score: those belong to a
strategy, later (patterns before exits). OI is deliberately NOT read here - at
detection time the trigger bar's own 5-min OI is never yet written (measured
230/230 quarter-hours, 2026-11-05), so OI is joined to events at read time from
the stored rows, which are immutable and keyed by time.

FAMILIES (each is a 5-min trigger at a level, with a direction):
  R1  rejection   - a reversal candle whose extreme is within NEAR of a level;
                    subtype "plain" when no textbook shape formed (B2)
  R2  sweep       - price goes beyond a level by more than NEAR (a wick, or up to
                    RECLAIM_MAX_BARS closes) and closes back
  C1  break       - a close through a level, body in the break direction
  C2  retest      - an R1 or R2 at a level price has already CROSSED this
                    session in the event's direction: same geometry, different
                    history, so it is a continuation, not a reversal. Only the
                    first retest per break is kept (B1, _first_retest_only)
  D   double/triple test - not a family of its own: an R1 or R2 at a level
                    tested before carries chart_pattern = double_bottom, ...

STATELESS. detect_session() is a pure function of the session's closed bars:
an event at bar b depends only on bars up to b and levels known when b
started, so re-deriving the session on every run reproduces earlier events
exactly. Which run ANNOUNCES an event is the caller's freshness filter
(fresh_events), the same rule today's detector uses.
"""

import os
import statistics

import structure as st
from structure import BAR_SECONDS, SUPPORT, RESISTANCE

# The rules this module implements. Every stored event carries it and it is in
# algo.pattern_events' key, so a change to the definitions writes NEW rows beside
# the old ones instead of overwriting them. Bump it with any change that can
# alter which events are found or what they carry.
#   v2.0  2026-11-06  PDH/PDL/ORH/ORL from the future's own bars
#   v2.1  2026-11-09  PDH/PDL/ORH/ORL from the INDEX, priced on the future with
#                     each bar's basis (plan v3). Everything else unchanged.
RULES_VERSION = "v2.1"

# ---- tunables. PROVISIONAL, env-overridable (definitions section 11) --------
# Single-bar shapes need a real range: on a 5-pt bar a "doji" is noise. 0.03% is
# ~7 pts, about the 10th percentile of a 5-min future bar (measured 2026-11-05).
MIN_RANGE_PCT = float(os.environ.get("V2_MIN_RANGE_PCT", "0.03"))
# Longer than this many closes beyond a level and it is a breakdown, not a sweep.
# NOT MEASURED - the replay reports the real spread.
RECLAIM_MAX_BARS = int(os.environ.get("V2_RECLAIM_MAX_BARS", "6"))
VOLUME_LOOKBACK = int(os.environ.get("V2_VOLUME_LOOKBACK", "20"))
# Textbook shape ratios; the single-bar ones are the values measured in section 1.
HAMMER_WICK_TO_BODY = 2.0
HAMMER_MAX_OPPOSITE_WICK = 0.3   # of range
DOJI_MAX_BODY = 0.1              # of range
STAR_FIRST_MIN_BODY = 0.5        # of the first bar's range
STAR_MIDDLE_MAX_BODY = 0.3       # of the first bar's body
# B2 plain rejection: the close must sit in this favourable fraction of the bar's
# own range (0.5 = the upper half for an up rejection). NOT MEASURED.
PLAIN_CLOSE_FRACTION = float(os.environ.get("V2_PLAIN_CLOSE_FRACTION", "0.5"))

# Trigger window: from the first bar after the opening range to the last stored
# bar. Nothing is cut by time of day; `clock` is recorded instead.
TRIGGER_FROM_MINUTE = 9 * 60 + 30
TRIGGER_TO_MINUTE = 15 * 60 + 25
OPEN_BAR_MINUTE = 9 * 60 + 15    # left out of the volume baseline (V4)

UP, DOWN = "up", "down"
R1, R2, C1, C2 = "R1", "R2", "C1", "C2"


# ---- candle shapes -----------------------------------------------------------

def _anatomy(bar):
    body = abs(bar["close"] - bar["open"])
    rng = bar["high"] - bar["low"]
    upper = bar["high"] - max(bar["open"], bar["close"])
    lower = min(bar["open"], bar["close"]) - bar["low"]
    return body, rng, upper, lower


def _bullish(bar):
    return bar["close"] > bar["open"]


def _bearish(bar):
    return bar["close"] < bar["open"]


def _contiguous(bars, first, last):
    return bars[last]["ts"] - bars[first]["ts"] == (last - first) * BAR_SECONDS


def shapes_at(bars, i, first_allowed=0):
    """
    Every shape that COMPLETES on bar i: [{name, direction, first}], where
    direction is up / down, or None for a doji (its direction comes from the
    level it sits on), and `first` is the index of the pattern's first bar.
    Multi-bar shapes need contiguous bars within the session (first_allowed).
    """
    out = []
    bar = bars[i]
    body, rng, upper, lower = _anatomy(bar)
    single_ok = rng > 0 and rng >= bar["close"] * MIN_RANGE_PCT / 100.0
    if single_ok and body > 0:
        if lower >= HAMMER_WICK_TO_BODY * body and upper <= HAMMER_MAX_OPPOSITE_WICK * rng:
            out.append({"name": "hammer", "direction": UP, "first": i})
        if upper >= HAMMER_WICK_TO_BODY * body and lower <= HAMMER_MAX_OPPOSITE_WICK * rng:
            out.append({"name": "shooting_star", "direction": DOWN, "first": i})
    if single_ok and body <= DOJI_MAX_BODY * rng:
        out.append({"name": "doji", "direction": None, "first": i})

    if i - 1 >= first_allowed and _contiguous(bars, i - 1, i):
        prev = bars[i - 1]
        if (_bearish(prev) and _bullish(bar)
                and bar["open"] <= prev["close"] and bar["close"] >= prev["open"]):
            out.append({"name": "bullish_engulfing", "direction": UP, "first": i - 1})
        if (_bullish(prev) and _bearish(bar)
                and bar["open"] >= prev["close"] and bar["close"] <= prev["open"]):
            out.append({"name": "bearish_engulfing", "direction": DOWN, "first": i - 1})

    if i - 2 >= first_allowed and _contiguous(bars, i - 2, i):
        a, m = bars[i - 2], bars[i - 1]
        a_body, a_rng, _, _ = _anatomy(a)
        m_body = abs(m["close"] - m["open"])
        a_mid = (a["open"] + a["close"]) / 2.0
        star_ok = a_rng > 0 and a_body >= STAR_FIRST_MIN_BODY * a_rng and m_body <= STAR_MIDDLE_MAX_BODY * a_body
        if star_ok and _bearish(a) and _bullish(bar) and bar["close"] > a_mid:
            out.append({"name": "morning_star", "direction": UP, "first": i - 2})
        if star_ok and _bullish(a) and _bearish(bar) and bar["close"] < a_mid:
            out.append({"name": "evening_star", "direction": DOWN, "first": i - 2})
    return out


# ---- volume ------------------------------------------------------------------

def volume_z_series(bars):
    """
    z of each bar's volume against the up-to-VOLUME_LOOKBACK bars before it,
    with 09:15 bars LEFT OUT of every baseline (V4): the open bar is several
    times a normal bar and sat in the 20-bar baseline until ~10:55, roughly
    halving sensitivity before 11:00 (measured 2026-10-30). None where the
    baseline has fewer than two bars or no spread.
    """
    zs = []
    for i, bar in enumerate(bars):
        baseline = []
        j = i - 1
        while j >= 0 and len(baseline) < VOLUME_LOOKBACK:
            if st.ist_minute(bars[j]["ts"]) != OPEN_BAR_MINUTE:
                baseline.append(bars[j]["volume"])
            j -= 1
        if len(baseline) < 2:
            zs.append(None)
            continue
        sd = statistics.pstdev(baseline)
        zs.append(None if sd == 0 else (bar["volume"] - statistics.fmean(baseline)) / sd)
    return zs


def rvol_tod(volume, history):
    """Volume over the median of the same clock bar in earlier sessions, or None."""
    history = [v for v in (history or []) if v is not None]
    if not history:
        return None
    median = statistics.median(history)
    return None if median <= 0 else volume / median


# ---- levels in force at a bar ------------------------------------------------

class _Level:
    """
    One level as the detector sees it at a bar. Fixed levels have one price;
    an option wall is an index STRIKE whose future price moves with the basis,
    so price_at(j) converts it with bar j's own basis.
    """

    def __init__(self, name, known_from_index, origin_ts, price=None, strike=None, basis_fn=None):
        self.name = name
        self.known_from_index = known_from_index
        self.origin_ts = origin_ts  # where its tests start counting (structure.prior_tests)
        self._price = price
        self.strike = strike
        self._basis_fn = basis_fn

    def price_at(self, j):
        if self.strike is None:
            return self._price
        basis = self._basis_fn(j)
        return None if basis is None else self.strike + basis


class _Session:
    """The bars and the level machinery for one session's detection."""

    def __init__(self, session_bars, prev_bars, index_bars, prev_index_bars, fno_rows):
        self.bars = list(prev_bars) + list(session_bars)
        self.s0 = len(prev_bars)
        self.session_bars = list(session_bars)
        self.bars15 = st.build_15min(self.session_bars)
        self.swings = st.find_swings(self.bars15)
        self.index_day = st.day_levels(prev_index_bars, index_bars) if index_bars else []
        self.fno_rows = sorted(fno_rows or [], key=lambda r: r["snapshot_ts"])
        self.fut_close = {b["ts"]: b["close"] for b in self.bars}
        self.idx_close = {b["ts"]: b["close"] for b in list(prev_index_bars or []) + list(index_bars or [])}
        self.z = volume_z_series(self.bars)

    def basis(self, j):
        return st.basis_at(self.bars[j]["ts"], self.fut_close, self.idx_close)

    def first_index_at_or_after(self, ts):
        for j in range(self.s0, len(self.bars)):
            if self.bars[j]["ts"] >= ts:
                return j
        return len(self.bars)

    def history_start(self, known_from_ts):
        """
        The first bar whose CLOSE may be compared with a level that becomes
        usable at known_from_ts: the bar just before the first usable bar. Its
        close is already known when the first usable bar opens, so a 09:30 close
        above the opening range is a break of it, and a 09:15 gap through PDH
        (against the previous session's last close) is a break of that.
        """
        return max(self.first_index_at_or_after(known_from_ts) - 1, 0)

    def levels_at(self, i):
        """
        Every level in force when bar i opened.

        v2.1: PDH/PDL/ORH/ORL are the INDEX's (the previous index session's
        high/low, the index's 09:15-09:25 range) - the levels a trader marks on
        the chart - priced on the future as index level + that bar's basis, the
        same way an option wall is. v2.0 took them from the future's own bars,
        which put the 2026-11-06 PDH 61 points above the index's (the previous
        day's basis was ~62, that day's ~25). Without index bars there are no
        such levels; the live handler refuses to run that way.
        """
        ts = self.bars[i]["ts"]
        session_open = self.session_bars[0]["ts"]
        levels = []
        for lvl in self.index_day:
            if lvl["known_from"] is None or lvl["known_from"] > ts:
                continue
            levels.append(_Level(lvl["name"], self.history_start(lvl["known_from"]),
                                 session_open, strike=lvl["price"], basis_fn=self.basis))
        for kind, name in (("high", st.SWING_HIGH), ("low", st.SWING_LOW)):
            swing = st.latest_swing(self.swings, kind, ts)
            if swing is not None:
                levels.append(_Level(name, self.history_start(swing["known_at"]),
                                     swing["ts"], price=swing["price"]))
        row = st.wall_row_at(self.fno_rows, ts)
        if row is not None:
            for name, strike in st.wall_strikes(row).items():
                since = st.wall_known_from(self.fno_rows, name, strike, ts)
                levels.append(_Level(name, self.history_start(since), since,
                                     strike=strike, basis_fn=self.basis))
        return levels

    def last_crossing(self, level, upto):
        """The most recent close-through of `level` at a bar <= upto: (direction, j) or None."""
        start = max(level.known_from_index + 1, 1)
        found = None
        for j in range(start, upto + 1):
            p0, p1 = level.price_at(j - 1), level.price_at(j)
            if p0 is None or p1 is None:
                continue
            c0, c1 = self.bars[j - 1]["close"], self.bars[j]["close"]
            if c0 <= p0 and c1 > p1:
                found = (UP, j)
            elif c0 >= p0 and c1 < p1:
                found = (DOWN, j)
        return found


# ---- the families ------------------------------------------------------------

def _beyond(direction, price, level_price):
    """Is price on the far side of the level for a move in `direction`?"""
    return price > level_price if direction == UP else price < level_price


def _crossing_candidate(sess, i, level, near):
    """
    A close through `level` on bar i, classified (definitions section 5):

    If price was on the other side only briefly (at most RECLAIM_MAX_BARS closes)
    and had come from this side, it is a RECLAIM - C2 dip_reclaim when the level
    was crossed this way earlier this session (a retest), otherwise R2
    close_below when the dip went deeper than NEAR (a sweep). Anything else is a
    C1 break, provided the bar's body runs the break's way.
    """
    if i - 1 < level.known_from_index:
        return None
    p_prev, p_now = level.price_at(i - 1), level.price_at(i)
    if p_prev is None or p_now is None:
        return None
    bars = sess.bars
    c_prev, c_now = bars[i - 1]["close"], bars[i]["close"]
    if c_prev <= p_prev and c_now > p_now:
        direction = UP
    elif c_prev >= p_prev and c_now < p_now:
        direction = DOWN
    else:
        return None

    # Walk back over the closes on the far (pre-move) side of the level.
    j = i - 1
    while j >= level.known_from_index:
        pj = level.price_at(j)
        if pj is None:
            break
        if _beyond(direction, bars[j]["close"], pj):
            break
        j -= 1
    run_length = (i - 1) - j
    came_from_this_side = (
        j >= level.known_from_index
        and level.price_at(j) is not None
        and _beyond(direction, bars[j]["close"], level.price_at(j))
    )

    if came_from_this_side and run_length <= RECLAIM_MAX_BARS:
        run = bars[j + 1:i + 1]
        if direction == UP:
            extreme = min(b["low"] for b in run)
            depth = p_now - extreme
        else:
            extreme = max(b["high"] for b in run)
            depth = extreme - p_now
        earlier = sess.last_crossing(level, j)
        base = {"direction": direction, "level": level, "first": j + 1, "extreme": extreme,
                "bars_beyond": run_length, "depth": depth}
        if earlier is not None and earlier[0] == direction:
            return {**base, "family": C2, "subtype": "dip_reclaim", "break_index": earlier[1]}
        if depth > near:
            return {**base, "family": R2, "subtype": "close_beyond"}
        return None

    if (direction == UP and _bullish(bars[i])) or (direction == DOWN and _bearish(bars[i])):
        return {"direction": direction, "level": level, "first": i, "extreme": c_now,
                "family": C1, "subtype": None}
    return None


def _wick_candidate(sess, i, level, near):
    """
    A one-bar sweep: bar i trades beyond the level by more than NEAR and closes
    back, having come from this side. R2 wick - or C2 wick_reclaim when the level
    was crossed this way earlier this session.
    """
    if i - 1 < level.known_from_index:
        return None
    p_prev, p_now = level.price_at(i - 1), level.price_at(i)
    if p_prev is None or p_now is None:
        return None
    bar, c_prev = sess.bars[i], sess.bars[i - 1]["close"]
    if c_prev > p_prev and bar["low"] < p_now - near and bar["close"] > p_now:
        direction, extreme, depth = UP, bar["low"], p_now - bar["low"]
    elif c_prev < p_prev and bar["high"] > p_now + near and bar["close"] < p_now:
        direction, extreme, depth = DOWN, bar["high"], bar["high"] - p_now
    else:
        return None
    base = {"direction": direction, "level": level, "first": i, "extreme": extreme,
            "bars_beyond": 0, "depth": depth}
    earlier = sess.last_crossing(level, i - 1)
    if earlier is not None and earlier[0] == direction:
        return {**base, "family": C2, "subtype": "wick_reclaim", "break_index": earlier[1]}
    return {**base, "family": R2, "subtype": "wick"}


def _rejection_candidates(sess, i, levels, near):
    """
    R1: a reversal shape completing on bar i whose extreme is within NEAR of a
    level on the right side (support for up, resistance for down) with the
    trigger closing on the right side of it. A doji takes its direction from the
    level. At a level crossed this way earlier this session it is C2 touch_hold.
    """
    out = []
    bars = sess.bars
    for shape in shapes_at(bars, i, first_allowed=sess.s0):
        first = shape["first"]
        directions = [shape["direction"]] if shape["direction"] else [UP, DOWN]
        for direction in directions:
            span = bars[first:i + 1]
            extreme = min(b["low"] for b in span) if direction == UP else max(b["high"] for b in span)
            for level in levels:
                # The level is in force at the trigger (levels_at); the bar before
                # the pattern only has to exist, to say which side price came from.
                if first - 1 < 0:
                    continue
                p_before, p_now = level.price_at(first - 1), level.price_at(i)
                if p_before is None or p_now is None:
                    continue
                wanted = SUPPORT if direction == UP else RESISTANCE
                if st.role(p_before, bars[first - 1]["close"]) != wanted:
                    continue
                if abs(extreme - p_now) > near or not _beyond(direction, bars[i]["close"], p_now):
                    continue
                cand = {"direction": direction, "level": level, "first": first, "extreme": extreme,
                        "candles": [shape["name"]], "family": R1, "subtype": None}
                earlier = sess.last_crossing(level, first - 1)
                if earlier is not None and earlier[0] == direction:
                    cand.update(family=C2, subtype="touch_hold", break_index=earlier[1])
                out.append(cand)
    covered = {c["direction"] for c in out}
    out.extend(_plain_candidates(sess, i, levels, near, covered))
    return out


def _plain_candidates(sess, i, levels, near, covered):
    """
    B2 (accepted 2026-11-05): a PLAIN rejection - bar i's extreme within NEAR of
    a level on the right side, closing back across it in the favourable
    PLAIN_CLOSE_FRACTION of its own range, where NO textbook shape qualified at
    this bar in that direction (`covered`). R1 subtype "plain"; at a level
    crossed this way earlier this session, C2 touch_hold with candles ["plain"].

    Why: the 2026-09-18 13:45 turn at the 23300 wall touched it within 1.3 pts
    on the future and reversed, but no bar was a textbook shape, so R1 missed
    it. Kept as its own subtype so its numbers can be compared with the
    textbook shapes rather than silently widening R1.
    """
    bars = sess.bars
    bar = bars[i]
    body, rng, _, _ = _anatomy(bar)
    if i - 1 < 0 or rng <= 0 or rng < bar["close"] * MIN_RANGE_PCT / 100.0:
        return []
    out = []
    for direction in (UP, DOWN):
        if direction in covered:
            continue
        if direction == UP:
            extreme = bar["low"]
            closes_well = bar["close"] >= bar["low"] + PLAIN_CLOSE_FRACTION * rng
        else:
            extreme = bar["high"]
            closes_well = bar["close"] <= bar["high"] - PLAIN_CLOSE_FRACTION * rng
        if not closes_well:
            continue
        wanted = SUPPORT if direction == UP else RESISTANCE
        for level in levels:
            p_before, p_now = level.price_at(i - 1), level.price_at(i)
            if p_before is None or p_now is None:
                continue
            if st.role(p_before, bars[i - 1]["close"]) != wanted:
                continue
            if abs(extreme - p_now) > near or not _beyond(direction, bar["close"], p_now):
                continue
            cand = {"direction": direction, "level": level, "first": i, "extreme": extreme,
                    "candles": ["plain"], "family": R1, "subtype": "plain"}
            earlier = sess.last_crossing(level, i - 1)
            if earlier is not None and earlier[0] == direction:
                cand.update(family=C2, subtype="touch_hold", break_index=earlier[1])
            out.append(cand)
    return out


# ---- assembly ----------------------------------------------------------------

def _distance(cand, i, sess):
    """Signed points from the level: the extreme for R1, the trigger close otherwise."""
    p = cand["level"].price_at(i)
    ref = cand["extreme"] if cand["family"] == R1 or cand.get("subtype") == "touch_hold" else sess.bars[i]["close"]
    return ref - p


def detect_session(session_bars, prev_bars=(), index_bars=(), prev_index_bars=(),
                   fno_rows=(), rvol_history=None):
    """
    Every v2 event in ONE session, oldest first.

    session_bars     this session's CLOSED 5-min bars of the active future
    prev_bars        the previous session's bars of the SAME contract: PDH/PDL,
                     the volume baseline across the open, the prior close
    index_bars,
    prev_index_bars  the index's bars for the same two sessions - only to convert
                     option strikes and record the index's own levels. Optional.
    fno_rows         this session's intraday_fno_data rows (option walls). Optional.
    rvol_history     {ist_minute: [that clock bar's volume in earlier sessions]}.
                     Optional; rvol_tod is None without it.
    """
    sess = _Session(session_bars, prev_bars, index_bars, prev_index_bars, fno_rows)
    return _first_retest_only(_detect_all(sess, rvol_history))


def _level_key(event):
    """A level's identity: its name and price (a wall: its strike, which the price moves around)."""
    return (event["level"],
            event["level_strike"] if event["level_strike"] is not None else event["level_price"])


def _first_retest_only(events):
    """
    B1 (accepted 2026-11-05): ONE retest per break.

    The first C2 at a level in a direction is kept. Further C2s there, that way,
    are dropped until the level is broken (C1) or swept and reclaimed (R2) in
    that direction again. Every reclaim in a chop is itself a crossing, so
    keying on "the last crossing" would let each one start a new break; keying
    on the last C1/R2 is what keeps it to one per break. Measured before B1: 624
    of 1,454 C2s were repeat retests of the same break, and they behaved like
    the first ones, so nothing measured is lost.

    Still stateless: whether an event is kept depends only on earlier events,
    which depend only on earlier bars.
    """
    open_for_retest = {}
    kept = []
    for event in events:  # oldest first
        key = (_level_key(event), event["direction"])
        if event["family"] in (C1, R2):
            open_for_retest[key] = True
        elif event["family"] == C2:
            if not open_for_retest.get(key, True):
                continue
            open_for_retest[key] = False
        kept.append(event)
    return kept


def _detect_all(sess, rvol_history):
    """Every candidate event of the session, oldest first, before the B1 filter."""
    bars = sess.bars
    events = []
    for i in range(sess.s0, len(bars)):
        minute = st.ist_minute(bars[i]["ts"])
        if not TRIGGER_FROM_MINUTE <= minute <= TRIGGER_TO_MINUTE or i == 0:
            continue
        near = st.near_points(bars[i]["close"])
        levels = sess.levels_at(i)
        cands = _rejection_candidates(sess, i, levels, near)
        for level in levels:
            for finder in (_crossing_candidate, _wick_candidate):
                cand = finder(sess, i, level, near)
                if cand is not None:
                    cands.append(cand)

        groups = {}
        for cand in cands:
            key = (cand["family"], cand.get("subtype"), cand["direction"])
            groups.setdefault(key, []).append(cand)
        for (family, subtype, direction), group in groups.items():
            group.sort(key=lambda c: abs(_distance(c, i, sess)))
            events.append(_event(sess, i, family, subtype, direction, group, levels, near,
                                 rvol_history))
    return events


def _event(sess, i, family, subtype, direction, group, levels, near, rvol_history):
    bars = sess.bars
    bar = bars[i]
    primary = group[0]
    level = primary["level"]
    level_price = level.price_at(i)
    close = bar["close"]
    first = min(c["first"] for c in group if c["level"] is level)
    distance = _distance(primary, i, sess)

    others = sorted({c["level"].name for c in group if c["level"] is not level}
                    | {lv.name for lv in levels
                       if lv is not level and lv.price_at(i) is not None
                       and abs(lv.price_at(i) - level_price) <= near})

    candles = sorted({name for c in group if c["level"] is level for name in c.get("candles", [])})
    if not candles:
        candles = sorted({s["name"] for s in shapes_at(bars, i, first_allowed=sess.s0)
                          if s["direction"] in (direction, None)})

    tests = None
    pattern = None
    if family in (R1, R2, C2):
        tests = st.prior_tests(sess.bars15, level_price, bar["ts"], near, direction,
                               level.origin_ts)
        if family in (R1, R2):
            pattern = st.chart_pattern(direction, tests)

    crossing = sess.last_crossing(level, first - 1)
    zs = [sess.z[j] for j in range(first, i + 1) if sess.z[j] is not None]

    event = {
        "family": family,
        "subtype": subtype,
        "direction": direction,
        "trigger_ts": bar["ts"],
        "first_ts": bars[first]["ts"],
        "clock": st.clock(bar["ts"]),
        "trigger_close": close,
        "extreme": primary["extreme"],
        "level": level.name,
        "level_price": round(level_price, 2),
        "level_strike": level.strike,
        "distance_pts": round(distance, 2),
        "distance_pct": round(distance / close * 100, 4),
        "other_levels": others,
        "candles": candles,
        "prior_tests": tests,
        "chart_pattern": pattern,
        "structure_15m": st.structure_15m(sess.swings, bar["ts"]),
        "prior_break": ({"direction": crossing[0], "ts": bars[crossing[1]]["ts"]}
                        if crossing else None),
        "vol_z": None if sess.z[i] is None else round(sess.z[i], 2),
        "vol_z_max": round(max(zs), 2) if zs else None,
        "rvol_tod": None,
        "retest_vol_ratio": None,
        "sweep_depth_pct": None,
        "bars_beyond": primary.get("bars_beyond"),
        "break_strength": None,
        "index_twin_offset_pct": None,
    }
    hist = (rvol_history or {}).get(st.ist_minute(bar["ts"]))
    r = rvol_tod(bar["volume"], hist)
    event["rvol_tod"] = None if r is None else round(r, 2)
    if primary.get("depth") is not None and family in (R2, C2):
        event["sweep_depth_pct"] = round(primary["depth"] / close * 100, 4)
    if family == C1:
        body, rng, _, _ = _anatomy(bar)
        event["break_strength"] = round(body / rng, 2) if rng > 0 else None
    if subtype == "dip_reclaim" and primary.get("break_index") is not None:
        dip = bars[primary["first"]:i]
        brk = bars[primary["break_index"]]["volume"]
        if dip and brk > 0:
            event["retest_vol_ratio"] = round(statistics.fmean(b["volume"] for b in dip) / brk, 2)
    if level.name in st.INDEX_TWINS and sess.index_day:
        twin = next((lv for lv in sess.index_day if lv["name"] == level.name), None)
        basis = sess.basis(i)
        if twin is not None and basis is not None:
            event["index_twin_offset_pct"] = round((twin["price"] + basis - level_price) / close * 100, 4)
    return event


def fresh_events(events, closed_after, closed_by):
    """
    The events a run ANNOUNCES: those whose trigger bar closed after the
    previous run's captured_at and by this run's - each bar is fresh exactly
    once, so a stateless re-derivation announces each event once. The same
    rule as detect.bar_closed_within.
    """
    return [e for e in events if closed_after < e["trigger_ts"] + BAR_SECONDS <= closed_by]
