"""
The 09:35 MORNING BRIEF (Amar, 2026-11-22) - every line a pure function of its inputs.

    build(inputs) -> (text, missing)

No Dhan, no database: the handler fetches, this composes. That is what lets the
whole brief be rebuilt offline for every stored session.

WHAT AMAR DECIDED (2026-11-22). The 09:35 message IS the morning brief, sent by
this function; the option chain and VIX are read live at 09:35 (not stored);
VIX is judged against its own last 20 sessions, with no fixed band; no
round-number levels; and the brief ALWAYS goes out - a section whose input is
missing is replaced by a line saying so, never dropped silently.

THE SECTIONS, in order:
  GAP          the gap from the previous close, the first 15-min candle (colour,
               how much of the gap it took back, whether it filled it), the
               15-min range against ADR, and where price stands
  OPENING READ the gap's direction, who held the first 15 min, the daily read's
               regime / structure / swing; and what turns the day's trend on.
               It is NOT a trend call: day_trend (market-classifier, rule G)
               first exists at the 09:45 run.
  LEVELS       PDH / PDC / PDL; the "open" daily highs and lows (no later
               session has traded through them - every stored session, no
               fixed lookback); the daily read's swing; the nearest unfilled
               daily FVG either side
  PAIN POINTS  today's chain: max pain, call / put wall with the OI at each,
               ATM, PCR; yesterday's close beside it; the OI change; the
               straddle's expected move; the weekly expiry
  VIX          now vs the previous close, ranked against the last N sessions'
               daily moves (UNUSUAL when bigger than all of them); the
               VIX-implied move from the daily read
ASCII only, like every message this repo sends.

THE INPUTS (a dict; any value may be an Exception - its section then reads
"missing - <reason>"):
  today        datetime.date
  clock        "HH:MM" - the run's IST clock
  daily        NIFTY daily candles, oldest first, COMPLETED sessions only
  vix_daily    INDIA VIX daily candles, oldest first
  opening      today's 09:15 15-min candle {open, high, low, close}, or None
  sentiment    the daily read just written (db.SENTIMENT_FIELDS)
  chain        {"summary": chain.summarise(...), "call_oi", "put_oi",
                "expiry": date, "spot"}
  prev_snap    db.read_prev_session_snapshot(...) or None
  vix_now      {"value", "clock"} - the latest closed VIX 5-min candle
  adr_sessions, vix_rank_sessions   ints (config)
"""

import statistics

WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Missing(Exception):
    """An input the section needs is absent - its line says so."""


def _need(inputs, key):
    value = inputs.get(key)
    if isinstance(value, BaseException):
        raise Missing(f"{key}: {value}")
    if value is None:
        raise Missing(f"no {key}")
    return value


def n2(x):
    return f"{x:,.2f}"


def n0(x):
    return f"{x:,.0f}"


def oi_m(x):
    return "-" if x is None else f"{x / 1e6:.1f}M"


def day_label(d):
    return f"{WEEKDAY[d.weekday()]} {d:%d %b}"


# ---- the pieces ---------------------------------------------------------------------
def adr(daily, sessions):
    """Mean high - low of the last `sessions` completed sessions."""
    last = daily[-sessions:]
    if len(last) < sessions:
        raise Missing(f"ADR{sessions} needs {sessions} daily candles, {len(last)} stored")
    return statistics.fmean(b["high"] - b["low"] for b in last)


def open_levels(daily):
    """
    Daily highs no LATER session has traded above, and lows none has traded
    below - over every stored session. A level is defined by price, not by a
    count of days: it stays open until the market takes it.
    """
    highs, lows = [], []
    run_high, run_low = float("-inf"), float("inf")
    for b in reversed(daily):
        if b["high"] > run_high:
            highs.append(b)
        if b["low"] < run_low:
            lows.append(b)
        run_high, run_low = max(run_high, b["high"]), min(run_low, b["low"])
    return highs, lows


def unfilled_fvgs(daily):
    """
    Daily fair-value gaps (three candles) that no later session has traded
    fully through. Bullish: a day's low above the high two days earlier - the
    zone between them, support below price. Bearish mirrored.
    """
    out = []
    for i in range(2, len(daily)):
        a, c = daily[i - 2], daily[i]
        if c["low"] > a["high"]:
            kind, lo, hi = "bullish", a["high"], c["low"]
        elif c["high"] < a["low"]:
            kind, lo, hi = "bearish", c["high"], a["low"]
        else:
            continue
        later = daily[i + 1:]
        filled = (any(x["low"] <= lo for x in later) if kind == "bullish"
                  else any(x["high"] >= hi for x in later))
        if not filled:
            out.append({"ts": c["ts"], "kind": kind, "lo": lo, "hi": hi})
    return out


def vix_rank(vix_daily, today_start, now_value, sessions):
    """(previous close, today's % move, how many of the last `sessions` daily
    moves today's beats in size)."""
    done = [b for b in vix_daily if b["ts"] < today_start]
    if len(done) < sessions + 1:
        raise Missing(f"VIX rank needs {sessions + 1} daily closes, {len(done)} stored")
    prev = done[-1]["close"]
    move = (now_value / prev - 1) * 100
    closes = [b["close"] for b in done[-(sessions + 1):]]
    moves = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    beats = sum(1 for m in moves if abs(move) > m)
    return prev, move, beats


# ---- the sections --------------------------------------------------------------------
def section_gap(x):
    daily, o15 = _need(x, "daily"), x.get("opening")
    if isinstance(o15, BaseException):
        raise Missing(f"opening: {o15}")
    if not o15:
        raise Missing("no 09:15 15-min candle - did the market open?")
    pdc, pdh, pdl = daily[-1]["close"], daily[-1]["high"], daily[-1]["low"]
    gap = o15["open"] - pdc
    word = "GAP UP" if gap > 0 else "GAP DOWN" if gap < 0 else "FLAT OPEN"
    body = o15["close"] - o15["open"]
    colour = "green" if body > 0 else "red" if body < 0 else "doji"
    lines = [f"{word} {gap:+,.2f} ({gap / pdc * 100:+.2f}%): prev close {n2(pdc)} -> open {n2(o15['open'])}"]
    if gap:
        filled = (o15["high"] >= pdc) if gap < 0 else (o15["low"] <= pdc)
        back = (o15["close"] - o15["open"]) / -gap * 100
        if filled:
            state = f"gap FILLED ({'high' if gap < 0 else 'low'} {n2(o15['high'] if gap < 0 else o15['low'])})"
        elif back >= 0:
            state = f"{back:.0f}% of the gap taken back, {abs(pdc - o15['close']):,.0f} pts still to the prev close"
        else:
            state = f"gap EXTENDED {abs(o15['close'] - o15['open']):,.0f} pts"
    else:
        state = "no gap"
    lines.append(f"  first 15 min: {colour} candle {n2(o15['open'])} -> {n2(o15['close'])} ({body:+,.0f}), {state}")
    rng = o15["high"] - o15["low"]
    try:
        vs_adr = f" = {rng / adr(daily, x['adr_sessions']) * 100:.0f}% of ADR{x['adr_sessions']}"
    except Missing:
        vs_adr = ""
    where = ("above PDH" if o15["close"] > pdh else "below PDL" if o15["close"] < pdl
             else "inside the previous day")
    lines.append(f"  15-min range {n2(o15['low'])} - {n2(o15['high'])} ({rng:,.0f} pts{vs_adr}) | price {where}")
    chain = x.get("chain")
    if isinstance(chain, dict) and chain.get("spot"):
        lines.append(f"  now {n2(chain['spot'])} ({x['clock']})")
    return lines


def section_opening_read(x):
    o15 = x.get("opening")
    if isinstance(o15, BaseException) or not o15:
        raise Missing("no 09:15 15-min candle")
    s = _need(x, "sentiment")
    daily = _need(x, "daily")
    gap = o15["open"] - daily[-1]["close"]
    word = "gap up" if gap > 0 else "gap down" if gap < 0 else "flat open"
    held = ("being bought" if o15["close"] > o15["open"] else "being sold" if o15["close"] < o15["open"]
            else "balanced")
    return [
        f"OPENING READ  {word}, {held} in the first 15 min | daily regime {s['regime']}, "
        f"structure {s['structure']}, swing {s['swing_direction']}"
        + (", volatility expanding" if s.get("volatility_expanding") else ""),
        f"  trend for the day: none yet - a 5-min CLOSE above {n2(o15['high'])} reads up, "
        f"below {n2(o15['low'])} down (first 15-min read at the 09:45 run)",
    ]


def section_levels(x):
    daily = _need(x, "daily")
    o15 = x.get("opening")
    o15 = o15 if isinstance(o15, dict) else None
    ref = o15["close"] if o15 else daily[-1]["close"]
    pdh, pdc, pdl = daily[-1]["high"], daily[-1]["close"], daily[-1]["low"]
    ist_date = x["ist_date"]
    lines = [f"LEVELS (from {'the 15-min close' if o15 else 'the prev close'} {n2(ref)})",
             f"  PDH {n2(pdh)} ({pdh - ref:+,.0f}) | PDC {n2(pdc)} ({pdc - ref:+,.0f}) | PDL {n2(pdl)} ({pdl - ref:+,.0f})"]
    highs, lows = open_levels(daily)
    # Open as of yesterday's close; the first 15 min may already have taken some.
    top = o15["high"] if o15 else float("-inf")
    bottom = o15["low"] if o15 else float("inf")
    above = sorted((b for b in highs if b["high"] > max(top, ref)), key=lambda b: b["high"])[:3]
    below = sorted((b for b in lows if b["low"] < min(bottom, ref)), key=lambda b: -b["low"])[:3]
    taken_h = [b for b in highs if b["high"] <= top]
    taken_l = [b for b in lows if b["low"] >= bottom]
    fmt = lambda b, k: f"{n2(b[k])} ({ist_date(b['ts']):%d %b})"  # noqa: E731
    lines.append("  open highs above: " + (", ".join(fmt(b, "high") for b in above)
                                           or "none - above every stored daily high"))
    lines.append("  open lows below:  " + (", ".join(fmt(b, "low") for b in below)
                                           or "none - below every stored daily low"))
    if taken_h or taken_l:
        lines.append("  taken in the first 15 min: " + ", ".join(
            [f"high {fmt(b, 'high')}" for b in taken_h] + [f"low {fmt(b, 'low')}" for b in taken_l]))
    s = x.get("sentiment")
    if isinstance(s, dict) and s.get("swing_high") is not None:
        lines.append(f"  swing high {n2(s['swing_high'])} | swing low {n2(s['swing_low'])} [daily read]")
    else:
        lines.append("  swing: missing - no daily read")
    gaps = unfilled_fvgs(daily)
    up = [g for g in gaps if g["lo"] > ref]
    down = [g for g in gaps if g["hi"] < ref]
    near_up = min(up, key=lambda g: g["lo"] - ref) if up else None
    near_dn = min(down, key=lambda g: ref - g["hi"]) if down else None
    part = []
    if near_up:
        part.append(f"above {near_up['kind']} {n2(near_up['lo'])}-{n2(near_up['hi'])} ({ist_date(near_up['ts']):%d %b})")
    if near_dn:
        part.append(f"below {near_dn['kind']} {n2(near_dn['lo'])}-{n2(near_dn['hi'])} ({ist_date(near_dn['ts']):%d %b})")
    lines.append(f"  unfilled daily FVGs: {len(gaps)}" + (" - nearest " + "; ".join(part) if part else ""))
    return lines


def section_pain(x):
    c = _need(x, "chain")
    s, today = c["summary"], x["today"]
    expiry = c["expiry"]
    head = f"PAIN POINTS (weekly expiry {day_label(expiry)}" + (", EXPIRY TODAY" if expiry == today else "") + ")"
    lines = [head,
             f"  max pain {n0(s['max_pain'])} | call wall {n0(s['max_oi_call'])} (OI {oi_m(c.get('call_oi'))}) | "
             f"put wall {n0(s['max_oi_put'])} (OI {oi_m(c.get('put_oi'))}) | ATM {n0(s['atm_strike'])} | "
             f"PCR {s['pcr_oi']:.2f}"]
    p = x.get("prev_snap")
    if isinstance(p, dict) and p:
        same = p.get("near_expiry_ts") is not None and x["ist_date"](p["near_expiry_ts"]) == expiry
        lines.append(f"  yesterday's close{'' if same else ' (previous expiry)'}: max pain {n0(p['near_max_pain'])} | "
                     f"call wall {n0(p['near_max_oi_call'])} | put wall {n0(p['near_max_oi_put'])} | "
                     f"PCR {p['near_pcr_oi']:.2f}")
        if same and p.get("near_ce_oi_total") and p.get("near_pe_oi_total"):
            lines.append(f"  CE OI {(s['ce_oi_total'] / p['near_ce_oi_total'] - 1) * 100:+.1f}% | "
                         f"PE OI {(s['pe_oi_total'] / p['near_pe_oi_total'] - 1) * 100:+.1f}% since yesterday's close")
        elif not same:
            lines.append("  OI change: n/a - yesterday's snapshot is the previous expiry")
    else:
        reason = p if isinstance(p, BaseException) else "none stored"
        lines.append(f"  yesterday's close: missing - {reason}")
    if s.get("straddle") and c.get("spot"):
        lines.append(f"  straddle {n0(s['straddle'])} -> expected move {n2(c['spot'] - s['straddle'])} .. "
                     f"{n2(c['spot'] + s['straddle'])}")
    return lines


def section_vix(x):
    v = _need(x, "vix_now")
    vix_daily = _need(x, "vix_daily")
    prev, move, beats = vix_rank(vix_daily, x["today_start"], v["value"], x["vix_rank_sessions"])
    n = x["vix_rank_sessions"]
    lines = [f"VIX {v['value']:.2f} at {v['clock']} ({move:+.1f}% vs prev close {prev:.2f}) - bigger than "
             f"{beats} of the last {n} daily moves" + (" - UNUSUAL" if beats == n else "")]
    s = x.get("sentiment")
    if isinstance(s, dict) and s.get("expected_move") is not None:
        lines.append(f"  VIX-implied move +/-{n0(s['expected_move'])} -> {n2(s['lower_volatility'])} .. "
                     f"{n2(s['upper_volatility'])}")
    return lines


def section_adr(x):
    daily = _need(x, "daily")
    n = x["adr_sessions"]
    return [f"ADR{n} {adr(daily, n):,.0f} pts ({n} sessions to {x['ist_date'](daily[-1]['ts']):%d %b})"]


SECTIONS = (("GAP", section_gap), ("OPENING READ", section_opening_read), ("LEVELS", section_levels),
            ("PAIN POINTS", section_pain), ("VIX", section_vix), ("ADR", section_adr))


def build(inputs):
    """(text, missing): every section, each in its own guard - a missing input
    becomes a "missing" line and an entry in `missing`, never an exception."""
    today = inputs["today"]
    lines = [f"NIFTY MORNING BRIEF - {day_label(today)} {today:%Y}, {inputs['clock']}", ""]
    missing = []
    for name, section in SECTIONS:
        try:
            lines += section(inputs)
        except Missing as exc:
            lines.append(f"{name}: missing - {exc}")
            missing.append((name, str(exc)))
        except Exception as exc:  # a bug must not cost the brief - it is reported, not swallowed
            lines.append(f"{name}: missing - {type(exc).__name__}: {exc}")
            missing.append((name, f"{type(exc).__name__}: {exc}"))
        if name in ("GAP", "OPENING READ", "LEVELS", "PAIN POINTS"):
            lines.append("")
    lines += ["", "Index points. Not a trade recommendation."]
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    ascii_text = text.encode("ascii", "replace").decode("ascii")  # every message here is ASCII
    if ascii_text != text:
        missing.append(("TEXT", "non-ASCII characters replaced"))
    return ascii_text, missing
