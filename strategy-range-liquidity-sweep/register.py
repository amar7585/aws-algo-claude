"""
The alert register - every level the playbook watched today, and what became of it (Amar 2026-11-14).

RE-DERIVED AND RECORDED, NEVER READ BACK. Amar's choice ("re-derive and record"): each run rebuilds the whole
session's register from the closed candles and the playbook's own derivations (session.derive, with and without the
gate) and writes it to algo.playbook_alerts. Nothing here is read on the next run, so a failed or retried run cannot
leave a wrong state behind - the next run rewrites the day. It decides no trade: it records what the playbook decided.

ONE ROW PER ALERT = one level touched on one side, under one rule set (current / new).
  touch     a candle's wick goes beyond a level the playbook watches and the candle before did not - the same "first
            penetration" rule as sweep.detect
  retest    another fresh penetration of the same level while the alert is still watching (touches + 1)

STATUS
  watching  touched, no entry yet, nothing has ended it
  entered   the playbook took the trade (gate + cell passed) - `outcome` / `steps` say how the trade went
  blocked   the structure qualified but the gate or the cell refused it - followed in shadow, same fields
  missed    no entry, and price reached T2 - the opposite side of the opening range - without us: the full move
  invalid   no entry, and the level was accepted through, the opening range broke for good (the playbook stands
            down for the day), or - new rules - a push was still beyond the level at 15:00
  ended     no entry, and 15:00 came first
  covered   no entry here because a trade on the same side was taken (or blocked) at another level - one idea per side

Both choices for "missed" and the 15:00 push were measured on the 104-session replay before they were made
(2026-11-14): with T1 as the target a T1 sitting on the level marked misses (11-13: 0.75 pt from PDL while PDL broke),
and 34 of 38 new-rule "ended" alerts were pushes still beyond at 15:00 - a break that held.

PURE. A function of the bars and the derivations; no I/O.
"""

import collections

import gate as gate_lib
import manage
import sweep as sweep_lib
from config import ACCEPTANCE_CLOSES
from levels import HIGH, LOW, ist_datetime
from session import ENTRY_WINDOWS, walk_current

WATCHING, ENTERED, BLOCKED, MISSED, INVALID, ENDED, COVERED = (
    "watching", "entered", "blocked", "missed", "invalid", "ended", "covered")
NO_TRADE_ENDS = (MISSED, INVALID, ENDED, COVERED)
CURRENT, NEW = "current", "new"
LOGICS = (CURRENT, NEW)


def _beyond(side, price, level):
    return price > level if side == HIGH else price < level


def _ext(side, bar):
    return bar["high"] if side == HIGH else bar["low"]


def _fresh_touch(bars, n, side, level):
    return _beyond(side, _ext(side, bars[n]), level) and (n == 0 or not _beyond(side, _ext(side, bars[n - 1]), level))


def _hm(bars, i):
    return ist_datetime(bars[i]["ts"]).strftime("%H:%M")


# ---- the trades each derivation produced ---------------------------------------------------------------------------
def _held(bars, n, c):
    """The current rules have no trade management: a trade is held to its stop or T2 (stop first within a candle)."""
    short = c["direction"] == "short"
    for i in range(n + 1, len(bars)):
        b = bars[i]
        if (b["high"] >= c["stop"]) if short else (b["low"] <= c["stop"]):
            return manage.STOP, i, [{"kind": manage.STOP, "index": i, "price": c["stop"], "reason": "the stop traded"}]
        if (b["low"] <= c["t2"]) if short else (b["high"] >= c["t2"]):
            return manage.TARGET, i, [{"kind": manage.TARGET, "index": i, "price": c["t2"], "reason": "T2 traded"}]
    return "open", None, []


def trades(result, bars, logic):
    """[{n, key, event, candidate, outcome, end, steps}] - one per trade the derivation took, in order."""
    out = []
    if logic == CURRENT:
        for n, rec in result["current"]:
            outcome, end, steps = _held(bars, n, rec["candidate"])
            out.append({"n": n, "key": (rec["event"]["pool"]["name"], rec["event"]["side"]), "event": rec["event"],
                        "candidate": rec["candidate"], "outcome": outcome, "end": end, "steps": steps})
    else:
        for idea in result["ideas"]:
            outcome = idea["steps"][-1]["kind"] if idea["end"] is not None else idea["state"]
            out.append({"n": idea["n"], "key": (idea["event"]["pool"]["name"], idea["event"]["side"]),
                        "event": idea["event"], "candidate": idea["candidate"], "outcome": outcome,
                        "end": idea["end"], "steps": idea["steps"]})
    return out


# ---- what ends an alert with no entry ------------------------------------------------------------------------------
def _accepted_from(bars, level, side, start):
    """CURRENT rules: the close completing ACCEPTANCE_CLOSES consecutive closes beyond, at/after start."""
    run = 0
    for i in range(start, len(bars)):
        run = run + 1 if _beyond(side, bars[i]["close"], level) else 0
        if run >= ACCEPTANCE_CLOSES:
            return i
    return None


def _push_broke_from(bars, pool, start):
    """NEW rules: (index, why) of the first push at this level resolved as a break, at/after start - the index is the
    candle that decided it (the next candle when the one-more-look rule ran, sweep.push_sweeps)."""
    for r in sweep_lib.push_sweeps(bars, pool):
        if r["kind"] == sweep_lib.PUSH_BROKE and r["back_inside"] >= start:
            return r.get("decided_at", r["back_inside"]), r["why"]
    return None, None


def _still_beyond(bars, level, side, n):
    """NEW rules: the last ACCEPTANCE_CLOSES+ closes up to n are all beyond - a push that has not come back."""
    run = 0
    for i in range(n, -1, -1):
        if not _beyond(side, bars[i]["close"], level):
            break
        run += 1
    return run >= ACCEPTANCE_CLOSES


def range_stand_down(bars, orb, logic):
    """(index, reason) where the opening range broke for good - after it the playbook takes no range trade."""
    if not orb:
        return None, None
    best = (None, None)
    for level, side, name in ((orb["high"], HIGH, "opening-range high"), (orb["low"], LOW, "opening-range low")):
        if logic == CURRENT:
            at = sweep_lib.acceptance_at(bars, level, side)
            why = f"the range broke at the {name} ({ACCEPTANCE_CLOSES} closes beyond)"
        else:
            at, why0 = _push_broke_from(bars, {"name": "_range", "price": level, "side": side, "priority": 0}, 0)
            why = f"the range broke at the {name} ({why0})" if at is not None else None
        if at is not None and (best[0] is None or at < best[0]):
            best = (at, why)
    return best


def missed_target(side, level, ctx):
    """T2 of a trade faded from this level: the opposite side of the opening range (trade.build's T2), when it lies
    on the trade's side of the level."""
    orb = ctx["orb"]
    if not orb or not orb.get("complete"):
        return None
    opp = orb["low"] if side == HIGH else orb["high"]
    return round(opp, 2) if ((opp < level) if side == HIGH else (opp > level)) else None


def _gate_reason(ctx):
    why = list(ctx.get("cell_failures") or []) + [str(f) for f in (ctx.get("failures") or [])]
    return "; ".join(why) or f"window: {ctx.get('window')}"


# ---- why each touch did or did not become an entry, in the playbook's own words ---------------------------------------
def _fresh_start(bars, side, level, k):
    while k > 0 and _beyond(side, _ext(side, bars[k - 1]), level):
        k -= 1
    return k


def touch_notes(bars, contexts, gated, ungated, logic, daily):
    """{(pool name, side, touch index): [(decided at index or None, text)]}."""
    out = collections.defaultdict(list)
    pools = [p for p in contexts[-1]["pools"] if not p["name"].startswith("session")]
    g_keys = {(t["key"], t["event"]["sweep_index"]) for t in trades(gated, bars, logic)}
    u_keys = {(t["key"], t["event"]["sweep_index"]) for t in trades(ungated, bars, logic)}

    def confirmed(e):
        c = e["confirm_index"]
        ctx = contexts[c]
        key = (e["pool"]["name"], e["side"])
        head = f"swept and confirmed at {_hm(bars, c)}"
        if ctx["window"] not in ENTRY_WINDOWS:
            return f"{head}, but {ctx['window']}"
        if (key, e["sweep_index"]) in g_keys:
            return f"{head} -> ENTRY"
        if (key, e["sweep_index"]) in u_keys:
            return f"{head}, refused by the gate/cell: {_gate_reason(ctx)}"
        # a structural refusal - the same with or without the gate
        if logic == CURRENT:
            row = ctx["row"] or {}
            _, rej, _ = walk_current(bars[: c + 1], ctx["pools"], ctx["orb"], row.get("vwap"), daily, ctx["window"])
        else:
            rej = ungated["rejected"].get(c, [])
        why = [r for ev, r in rej if ev["pool"]["name"] == key[0] and ev["sweep_index"] == e["sweep_index"]]
        return f"{head}, not taken: {why[0]}" if why else f"{head}, not taken"

    for e in sweep_lib.detect(bars, pools):
        k = (e["pool"]["name"], e["side"], e["sweep_index"])
        if e["reject_reason"]:
            out[k].append((e["sweep_index"], f"wick {e['penetration_pct']:.2f}% through: {e['reject_reason']}"))
        else:
            out[k].append((e["sweep_index"], confirmed(e)))
    if logic == NEW:
        for pool in pools:
            for r in sweep_lib.push_sweeps(bars, pool):
                if "push_start" not in r:
                    k0 = r["accepted_at"] - ACCEPTANCE_CLOSES + 1
                    out[(pool["name"], pool["side"], _fresh_start(bars, pool["side"], pool["price"], k0))].append(
                        (None, "push still beyond the level"))
                    continue
                t = _fresh_start(bars, pool["side"], pool["price"], r["push_start"])
                head = (f"push of {r['closes']} closes beyond ({r['penetration_pct']:.2f}% at its furthest), "
                        f"back inside at {_hm(bars, r['back_inside'])}")
                key = (pool["name"], pool["side"], t)
                if r["kind"] == sweep_lib.PUSH_SWEEP:
                    past = (f", past the whole push at {_hm(bars, r['second_look'])} (the next candle); "
                            if r.get("second_look") is not None else " past the whole push; ")
                    out[key].append((r["event"]["confirm_index"], head + past + confirmed(r["event"])))
                elif r["kind"] == sweep_lib.PUSH_BROKE:
                    out[key].append((r.get("decided_at", r["back_inside"]), f"{head}: {r['why']} - a break, not a sweep"))
                elif r.get("awaiting") == "next candle":
                    out[key].append((None, f"{head}: not past the whole push - one more look on the next candle"))
                else:
                    out[key].append((None, f"{head}: awaiting the confirmation candle"))
    return out


# ---- the register ----------------------------------------------------------------------------------------------------
def build(bars, gated, ungated, logic, daily=None):
    """All alert rows for one rule set, oldest first. `gated` is session.derive's result as the playbook runs it;
    `ungated` the same with the gate and cell forced open (window kept) - where `blocked` trades come from."""
    contexts = gated["contexts"]
    orb = contexts[-1]["orb"] if contexts else None
    at = collections.defaultdict(list)
    g = trades(gated, bars, logic)
    gk = {(t["n"], t["key"]) for t in g}
    for t in g:
        at[t["n"]].append((ENTERED, t))
    for t in trades(ungated, bars, logic):
        if (t["n"], t["key"]) not in gk:
            at[t["n"]].append((BLOCKED, t))
    stand_down, stand_down_why = range_stand_down(bars, orb, logic)

    rows, open_, latest = [], {}, {}

    def new_alert(key, level, n, note=None):
        a = {"logic": logic, "level_name": key[0], "side": key[1], "level": round(level, 2), "alert_n": n,
             "touches": 1, "touch_ns": [n], "status": WATCHING, "status_n": None, "reason": None, "note": note,
             "target": missed_target(key[1], level, contexts[n]), "trade": None}
        if logic == CURRENT:
            a["_inv"] = (_accepted_from(bars, level, key[1], n), f"accepted through ({ACCEPTANCE_CLOSES} closes beyond)")
        else:
            i, why = _push_broke_from(bars, {"name": key[0], "price": level, "side": key[1], "priority": 0}, n)
            a["_inv"] = (i, f"accepted through ({why})")
        rows.append(a)
        open_[key] = a
        latest[key] = a
        return a

    for n in range(len(bars)):
        bar, ctx = bars[n], contexts[n]
        # 1. trades confirmed on this candle (entered first, then blocked)
        for kind, t in sorted(at[n], key=lambda x: x[0] != ENTERED):
            key = t["key"]
            a = open_.get(key)
            if a is None or a["status"] != WATCHING:
                prev = latest.get(key)
                # a no-trade end on the same excursion gives way to the trade
                if prev is not None and prev["status"] in NO_TRADE_ENDS and prev["status_n"] >= t["event"]["sweep_index"] - 1:
                    a = prev
                    a["note"] = f"was {prev['status']} at {_hm(bars, prev['status_n'])} on the same excursion"
                    open_[key] = a
                else:
                    a = new_alert(key, t["event"]["level"], t["event"]["sweep_index"], note="opened by the trade")
            a.update(status=kind, status_n=n, trade=t, reason=None if kind == ENTERED else _gate_reason(ctx))
            for k2, a2 in list(open_.items()):
                if k2 != key and k2[1] == key[1] and a2["status"] == WATCHING:
                    a2.update(status=COVERED, status_n=n, reason=f"{kind} at {key[0]}")
                    open_.pop(k2)
        # 2. watching alerts: a retest, then what ends them
        for key, a in list(open_.items()):
            if a["status"] != WATCHING:
                continue
            side, level = key[1], a["level"]
            if n > a["alert_n"] and _fresh_touch(bars, n, side, level):
                a["touches"] += 1
                a["touch_ns"].append(n)
            if a["target"] is None:
                a["target"] = missed_target(side, level, ctx)
            inv_n, inv_why = a["_inv"]
            if inv_n is not None and inv_n == n:
                a.update(status=INVALID, status_n=n, reason=inv_why)
            elif stand_down is not None and n >= stand_down:
                a.update(status=INVALID, status_n=n, reason=stand_down_why)
            elif a["target"] is not None and n > a["alert_n"] and (
                    bar["low"] <= a["target"] if side == HIGH else bar["high"] >= a["target"]):
                a.update(status=MISSED, status_n=n, reason=f"T2 {a['target']} (opposite side of the opening range) "
                                                           f"traded with no entry")
            elif ctx["window"] == gate_lib.WINDOW_CLOSED:
                if logic == NEW and _still_beyond(bars, level, side, n):
                    a.update(status=INVALID, status_n=n, reason="the push was still beyond the level at 15:00 - "
                                                                "the break held")
                else:
                    a.update(status=ENDED, status_n=n, reason="15:00 passed with no entry")
            if a["status"] != WATCHING:
                open_.pop(key)
        # 3. trades: the alert closes when its trade ends
        for key, a in list(open_.items()):
            if a["status"] in (ENTERED, BLOCKED) and a["trade"]["end"] is not None and n >= a["trade"]["end"]:
                open_.pop(key)
        # 4. fresh touches open new alerts - not once the scan window has closed, and not once the range has broken
        #    for good (the playbook has stood down for the day)
        if ctx["window"] != gate_lib.WINDOW_CLOSED and (stand_down is None or n < stand_down):
            for pool in ctx["pools"]:
                key = (pool["name"], pool["side"])
                if key not in open_ and _fresh_touch(bars, n, pool["side"], pool["price"]):
                    new_alert(key, pool["price"], n)

    notes = touch_notes(bars, contexts, gated, ungated, logic, daily)
    for a in rows:
        a.pop("_inv", None)

        def alive(res, a=a):  # only what was decided while this alert was alive
            if res is None:
                return a["status"] in (WATCHING, ENDED)
            return a["status_n"] is None or res <= a["status_n"]
        a["touch_log"] = [{"n": t, "notes": [txt for res, txt in notes.get((a["level_name"], a["side"], t), []) if alive(res)]}
                          for t in a.pop("touch_ns")]
    return rows


def records(bars, gated, ungated, daily=None):
    """The register for both rule sets, shaped for algo.playbook_alerts (epoch seconds; candle STARTS)."""
    out, seen = [], set()
    for logic in LOGICS:
        for a in build(bars, gated, ungated, logic, daily):
            ident = (a["logic"], a["side"], a["level_name"], int(bars[a["alert_n"]]["ts"]))
            if ident in seen:
                # Fail loud: two rows with one identity would make the upsert fail obscurely - name it here.
                raise RuntimeError(f"register: two alerts share identity {ident} - the alert walk is inconsistent")
            seen.add(ident)
            t = a["trade"]
            c = t["candidate"] if t else None
            out.append({
                "logic": a["logic"],
                "level_name": a["level_name"],
                "side": a["side"],
                "level_price": a["level"],
                "alert_ts": int(bars[a["alert_n"]]["ts"]),
                "touches": a["touches"],
                "touch_log": [{"ts": int(bars[x["n"]]["ts"]), "notes": x["notes"]} for x in a["touch_log"]],
                "status": a["status"],
                "status_ts": int(bars[a["status_n"]]["ts"]) if a["status_n"] is not None else None,
                "reason": (a["reason"] or "") + (f" ({a['note']})" if a["note"] else "") or None,
                "target_price": a["target"],
                "direction": c["direction"] if c else None,
                "entry_ts": int(bars[t["n"]]["ts"]) if t else None,
                "entry_price": c["entry"] if c else None,
                "signal_close": round(bars[t["n"]]["close"], 2) if t else None,
                "stop_price": c["stop"] if c else None,
                "t1_price": c["t1"] if c else None,
                "t2_price": c["t2"] if c else None,
                "risk_reward": c["risk_reward"] if c else None,
                "grade": c.get("grade") if c else None,
                "outcome": t["outcome"] if t else None,
                "steps": [{"kind": s["kind"], "ts": int(bars[s["index"]]["ts"]), "price": round(float(s["price"]), 2),
                           "reason": s.get("reason")} for s in t["steps"]] if t else [],
            })
    return out
