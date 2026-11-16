"""
The session, re-derived candle by candle under BOTH rule sets (2026-11-14). Pure.

Since 2026-11-14 this playbook runs on every 5-min tick. Each run re-derives
the whole session from its closed candles, one candle at a time, using only
what was known AT that candle: the candles up to it, and the 15-min row that
was in force then (regime, day trend, VIX). That makes the re-derivation
the same on every run, which is what lets a stateless function follow a trade
for hours and announce each step once - on the run whose newest candle it is.

Two rule sets run side by side, so each message can say which produced it:

  CURRENT  today's playbook rules (walk_current, unchanged): first-candle
           detection, 2 closes beyond the opening range = the range broke,
           2 attempts per side.
  NEW      today's detection PLUS a 2+ close push judged structurally at its
           close back inside, with one more look on the next candle (Amar
           2026-11-16, sweep.push_sweeps); "range broke" only when such a push
           failed or has not come back; one live idea per side; and the
           exit / watch / re-entry rule after entry (manage.follow).

Both take entries only when the gate passes, price is within the levels and
volatility is not high (regime not volatile-expansion). No bias (Amar
2026-11-14), no trend test (Amar 2026-11-15): "if price is within levels then
why it matters - depends on high volatility." 
"""

import gate as gate_lib
import manage
import sweep as sweep_lib
import trade as trade_lib
from config import (
    ALLOWED_REGIMES,
    MAX_ATTEMPTS_PER_SIDE,
    ROW_VISIBLE_LAG_SECONDS,
)
from levels import HIGH, LOW, build_pools, ist_datetime, stacked_with

ENTRY_WINDOWS = (gate_lib.WINDOW_PRIME, gate_lib.WINDOW_LATE)


# ---------------------------------------------------------------------------
# Today's rules, unchanged (moved here from handler.py)
# ---------------------------------------------------------------------------
def walk_current(bars, pools, orb, vwap, previous_day, window):
    """
    Replay the session's sweeps in order, applying the attempt rules as it goes.

    In order, because the rules are sequential: whether a sweep is a first
    attempt or a re-entry depends on what happened to the earlier one, and
    whether it is allowed at all depends on whether the range has since broken.
    Evaluating them independently would let a session produce three first
    attempts on the same side.
    """
    events = sweep_lib.detect(bars, pools)

    # Where the range itself broke, if it did. Measured on the opening range's
    # own boundaries - those two levels ARE the range - and independently of
    # whether an attempt was ever taken, because the stand-down applies either
    # way.
    broke_at = None
    if orb:
        for level, side in ((orb["high"], HIGH), (orb["low"], LOW)):
            at = sweep_lib.acceptance_at(bars, level, side)
            if at is not None:
                broke_at = at if broke_at is None else min(broke_at, at)

    attempts = {HIGH: [], LOW: []}
    candidates, rejections = [], []

    for event in events:
        if event["reject_reason"]:
            rejections.append((event, event["reject_reason"]))
            continue

        index = event.get("confirm_index", event["sweep_index"])
        if broke_at is not None and index >= broke_at:
            rejections.append((event, _broke_reason(bars, broke_at)))
            continue

        side = event["side"]
        price = event["level"]
        stacked = stacked_with(event["pool"], pools, price)

        # The late window takes a setup only with a stacked pool and a near
        # target.
        if window == gate_lib.WINDOW_LATE and not stacked:
            rejections.append((event, "after 14:30 only a stacked-pool sweep qualifies"))
            continue

        attempt_no = len(attempts[side]) + 1
        if attempt_no > MAX_ATTEMPTS_PER_SIDE:
            rejections.append(
                (event, f"attempt {attempt_no} on the {side} side - the cap is "
                        f"{MAX_ATTEMPTS_PER_SIDE} per side per session, and a second "
                        f"failure closes that side for the day"))
            continue

        if attempt_no == 1:
            candidate, reason = trade_lib.build(event, bars, orb, vwap, stacked, previous_day)
        else:
            previous = attempts[side][-1]
            if previous["outcome"]["outcome"] != sweep_lib.CASE_B:
                rejections.append(
                    (event, f"the first attempt on this side ended "
                            f"{previous['outcome']['outcome']}, and a re-entry is "
                            f"allowed only after Case B - an extended sweep with no "
                            f"acceptance"))
                continue
            candidate, reason = trade_lib.re_entry(
                event, bars, orb, vwap, stacked, previous["candidate"]["t2"], previous_day)

        if candidate is None:
            rejections.append((event, reason))
            continue

        if window == gate_lib.WINDOW_LATE and candidate["t1"] is None:
            rejections.append((event, "after 14:30 a near target is required and there is no T1"))
            continue

        result = sweep_lib.outcome(bars, event, candidate)
        record = {
            "event": event,
            "candidate": candidate,
            "stacked": stacked,
            "outcome": result,
            "attempt": attempt_no,
            # The first attempt on THIS side, for the re-entry report's
            # running cost. Not the first of the session.
            "first_on_side": attempts[side][0] if attempts[side] else None,
        }
        attempts[side].append(record)
        candidates.append(record)

    return candidates, rejections, broke_at


def _broke_reason(bars, broke_at):
    return (f"the range broke at {ist_datetime(bars[broke_at]['ts']).strftime('%H:%M')} - "
            f"a level was accepted through, so neither side is a range trade any more")


# ---------------------------------------------------------------------------
# What was in force at each candle
# ---------------------------------------------------------------------------
def row_in_force(rows, candle_close):
    """The newest 15-min row written by the candle's close + the recorder's lag."""
    visible = [r for r in rows if int(r["created_at"]) <= candle_close + ROW_VISIBLE_LAG_SECONDS]
    return visible[-1] if visible else None


def within_levels(close, orb, daily):
    """(ok, why) - is this close inside the levels: between min(PDL, ORL) and max(PDH, ORH)?

    Amar 2026-11-15: levels = PDH/PDL and the 15-min high/low; a market beyond
    ALL of them on one side has broken out (a high-volatility day) - no sweep.
    Measured on all 21 gate-off sweep trades over 104 sessions: the 3 it keeps
    out were all losers against the day trend; all 9 winners were inside.
    An unknown level fails: a missing input must not pass.
    """
    if not orb or not orb.get("complete") or not daily or daily.get("pd_high") is None or daily.get("pd_low") is None:
        return False, "levels unknown (opening range or the daily read missing)"
    hi = max(float(daily["pd_high"]), orb["high"])
    lo = min(float(daily["pd_low"]), orb["low"])
    if lo <= close <= hi:
        return True, ""
    return False, f"price {close:.2f} beyond all levels ({lo:.2f} - {hi:.2f})"


def context_at(bars, n, rows, daily, atr14):
    """Everything the entry and re-entry decisions need at candle n."""
    so_far = bars[: n + 1]
    as_of = bars[n]["ts"] + 300
    pools, orb = build_pools(so_far, previous_day=daily, snapshot=None)
    window = gate_lib.scan_window(as_of)
    row = row_in_force(rows, as_of)
    ctx = {"n": n, "as_of": as_of, "pools": pools, "orb": orb, "window": window, "row": row,
           "failures": [], "details": {}, "cell_failures": [], "entry": False, "reentry": False}
    if row is None:
        ctx["cell_failures"] = ["no 15-min row in force yet"]
        return ctx
    snap = dict(row)
    snap.update(snapshot_ts=as_of, day_high=max(b["high"] for b in so_far),
                day_low=min(b["low"] for b in so_far), spot=so_far[-1]["close"])
    passed, failures, details = gate_lib.evaluate(snap, snap, atr14, orb, as_of)
    cell = []
    if row.get("regime") not in ALLOWED_REGIMES:
        cell.append(f"regime {row.get('regime')}")
    within, why = within_levels(so_far[-1]["close"], orb, daily)
    if not within:
        cell.append(why)
    ctx.update(failures=failures, details=details, cell_failures=cell,
               entry=passed and not cell, reentry=not cell and window in ENTRY_WINDOWS)
    return ctx


# ---------------------------------------------------------------------------
# The whole session, both rule sets
# ---------------------------------------------------------------------------
def _gate_open(ctx):
    """The same candle with the gate and the cell forced open - the scan window still applies."""
    c = dict(ctx)
    c["row"] = c["row"] or {}
    allowed = c["window"] in ENTRY_WINDOWS
    c["entry"], c["reentry"] = allowed, allowed
    return c


def derive(bars, rows, daily, atr14, gated=True, contexts=None):
    """
    Returns {"contexts", "current", "ideas", "rejected"}:
      current   [(n, record)] - today's rules' entries, each on its confirming candle
      ideas     the new rules' trades: {n, event, candidate, stacked, steps, state, end}
      rejected  {n: [(event, reason)]} - the new rules' refusals, for the log

    gated=False runs the same rules with the gate and the cell forced open, for the
    alert register's `blocked` rows (register.py) - nothing sends or trades on it.
    `contexts` reuses a gated run's per-candle contexts instead of rebuilding them.
    """
    if contexts is None:
        contexts = [context_at(bars, n, rows, daily, atr14) for n in range(len(bars))]
    if not gated:
        contexts = [_gate_open(c) for c in contexts]
    current, ideas, rejected = [], [], {}

    def reentry_allowed(i):
        c = contexts[i]
        if c["reentry"]:
            return True, ""
        why = c["cell_failures"] or ([c["window"]] if c["window"] not in ENTRY_WINDOWS else [])
        return False, "; ".join(why) or "not allowed"

    for n, ctx in enumerate(contexts):
        if not ctx["entry"] or ctx["window"] not in ENTRY_WINDOWS:
            continue
        so_far, pools, orb, row = bars[: n + 1], ctx["pools"], ctx["orb"], ctx["row"]
        vwap = row.get("vwap")

        # ---- current rules --------------------------------------------------
        recs, _, _ = walk_current(so_far, pools, orb, vwap, daily, ctx["window"])
        for rec in recs:
            if rec["event"].get("confirm_index") == n and rec["outcome"]["outcome"] == sweep_lib.OPEN:
                current.append((n, rec))

        # ---- new rules --------------------------------------------------------
        events = [e for e in sweep_lib.detect(so_far, pools) if not e["reject_reason"] and e.get("confirm_index") == n]
        for pool in pools:
            events += [r["event"] for r in sweep_lib.push_sweeps(so_far, pool)
                       if r["kind"] == sweep_lib.PUSH_SWEEP and r["event"]["confirm_index"] == n]
        events.sort(key=lambda e: e["pool"]["priority"])
        broke = None
        if orb:
            marks = [sweep_lib.range_broke_at(so_far, orb["high"], HIGH), sweep_lib.range_broke_at(so_far, orb["low"], LOW)]
            marks = [m for m in marks if m is not None]
            broke = min(marks) if marks else None
        for event in events:
            side = event["side"]
            if broke is not None and n >= broke:
                rejected.setdefault(n, []).append((event, _broke_reason(so_far, broke)))
                continue
            live = [i for i in ideas if i["event"]["side"] == side and (i["end"] is None or i["end"] >= n)]
            if live:
                rejected.setdefault(n, []).append(
                    (event, f"a {side}-side idea is still live (from {ist_datetime(bars[live[-1]['n']]['ts'] + 300).strftime('%H:%M')})"))
                continue
            stacked = stacked_with(event["pool"], pools, event["level"])
            if ctx["window"] == gate_lib.WINDOW_LATE and not stacked:
                rejected.setdefault(n, []).append((event, "after 14:30 only a stacked-pool sweep qualifies"))
                continue
            candidate, reason = trade_lib.build(event, so_far, orb, vwap, stacked, daily)
            if candidate is None:
                rejected.setdefault(n, []).append((event, reason))
                continue
            if ctx["window"] == gate_lib.WINDOW_LATE and candidate["t1"] is None:
                rejected.setdefault(n, []).append((event, "after 14:30 a near target is required and there is no T1"))
                continue
            steps, state, end = manage.follow(bars, event, candidate, reentry_allowed)
            ideas.append({"n": n, "event": event, "candidate": candidate, "stacked": stacked,
                          "steps": steps, "state": state, "end": end})
    return {"contexts": contexts, "current": current, "ideas": ideas, "rejected": rejected}


# ---------------------------------------------------------------------------
# The messages for the candles this run announces
# ---------------------------------------------------------------------------
BOTH, CURRENT_ONLY, NEW_ONLY = "current + new logic", "current logic only", "new logic"


def _key(event):
    return (event["side"], event["pool"]["name"], event["sweep_time"])


def _same_trade(a, b):
    return all(a[k] == b[k] for k in ("direction", "entry", "stop", "t2"))


def messages(result, bars, fresh_after, newest_ts):
    """
    [(tag, kind, text)] for every entry and trade step on a candle stamped in
    (fresh_after, newest_ts] - normally just the newest candle.
    """
    fresh = lambda n: fresh_after < int(bars[n]["ts"]) <= newest_ts
    out = []
    cur = {_key(rec["event"]): (n, rec) for n, rec in result["current"] if fresh(n)}
    new = {_key(i["event"]): i for i in result["ideas"] if fresh(i["n"])}
    for key in sorted(set(cur) | set(new), key=lambda k: (k[2], k[1])):
        if key in cur and key in new and _same_trade(cur[key][1]["candidate"], new[key]["candidate"]):
            out.append((BOTH, manage.ENTRY, entry_text(bars, new[key]["n"], new[key]["event"], new[key]["candidate"], BOTH)))
            continue
        if key in cur:
            n, rec = cur[key]
            out.append((CURRENT_ONLY, manage.ENTRY, entry_text(bars, n, rec["event"], rec["candidate"], CURRENT_ONLY)))
        if key in new:
            i = new[key]
            out.append((NEW_ONLY, manage.ENTRY, entry_text(bars, i["n"], i["event"], i["candidate"], NEW_ONLY)))
    for idea in result["ideas"]:
        for step in idea["steps"]:
            if fresh(step["index"]):
                out.append((NEW_ONLY, step["kind"], step_text(bars, idea, step)))
    return out


def _stamp(bars, n):
    return ist_datetime(bars[n]["ts"] + 300).strftime("%Y-%m-%d %H:%M")


def _at_signal(bars, n, c):
    """The price when the signal fires - the confirming candle's close - and the risk-reward there.

    The playbook's entry is the close of the candle back inside, one candle
    earlier; the message can only arrive after the confirming candle closes, so
    a market order fills near THIS price (Amar 2026-11-15: show it). The register
    stores it as signal_close.
    """
    market = round(float(bars[n]["close"]), 2)
    short = c["direction"] == "short"
    risk = (c["stop"] - market) if short else (market - c["stop"])
    reward = (market - c["t2"]) if short else (c["t2"] - market)
    rr = reward / risk if risk > 0 and reward > 0 else 0.0
    return market, rr


def entry_text(bars, n, event, c, tag):
    market, rr = _at_signal(bars, n, c)
    return (f"SWEEP SIGNAL {c['direction'].upper()} {event['pool']['name']} - {_stamp(bars, n)}\n"
            f"sweep {event['sweep_time']}, entry {c['entry']} stop {c['stop']}\n"
            f"at the signal {market} ({ist_datetime(bars[n]['ts']).strftime('%H:%M')} candle close) - "
            f"enter at market, RR there 1:{rr:.2f}\n"
            f"T1 {c['t1']} T2 {c['t2']} RR {c['risk_reward']} ({c['grade']})\n"
            f"from: sweep playbook, {tag}")


def step_text(bars, idea, step):
    c, event = idea["candidate"], idea["event"]
    head = f"SWEEP {step['kind']} {c['direction'].upper()} {event['pool']['name']} - {_stamp(bars, step['index'])}"
    if step["kind"] == manage.EXIT:
        way = "below" if c["direction"] == "short" else "above"
        body = (f"{step['reason']} at {step['price']:.2f} (entry {c['entry']}) - out, now watching\n"
                f"re-entry on a close {way} {step['watch_for']:.2f} while the stop {c['stop']} holds")
    elif step["kind"] == manage.REENTRY:
        body = f"{step['reason']} - entry {step['price']:.2f} stop {c['stop']} T2 {c['t2']}"
    else:
        body = f"{step['reason']} at {step['price']:.2f} (entry {c['entry']}, stop {c['stop']}, T2 {c['t2']})"
    return f"{head}\n{body}\nfrom: sweep playbook, {NEW_ONLY}"
