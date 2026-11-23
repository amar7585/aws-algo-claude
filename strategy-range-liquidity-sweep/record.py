"""
The position record AND the messages, through order-manager (P2a 2026-11-22, P2b 2026-11-23). PURE.

Every trade the playbook ENTERED (the gated ideas) becomes signals for order-manager, one per step, each
carrying this playbook's own message text, word for word as before (order-manager sends it once, inside the
same transaction as the record):

    sweep step                          order-manager signal
    the entry (SWEEP SIGNAL)            ENTRY       3 lots at the confirming candle's close (the fill at the signal)
    T1 REACHED                          T1          2 lots at T1 (the range midpoint)
    EXIT                                EXIT        watch: true, at the close
    RE-ENTRY                            RE-ENTRY    3 lots at the close
    WATCH ENDED                         WATCH ENDED
    TARGET REACHED / STOP HIT           TARGET / STOP

THE HARD RULES ARE THE RISK MANAGER'S (P2b, Amar 2026-11-22 / 11-23). The derivation keeps its own 15:00
window and its 15:15 step as the alert register's record, but:
  * the 15:15 close is NOT sent from the derivation - only on the risk manager's time-exit instruction
    (forced_closes), at the 15:10 candle's close, with the same text the playbook has always sent;
  * the mode decides entries: entry_exit = as before; exit_only / force_exit = a would-be ENTRY becomes a
    REJECTED signal (its message headed REJECTED with the reason) and a would-be RE-ENTRY ends the watch;
  * force_exit = this playbook's own steps on the newest candle first, then whatever is still open or watching
    closed at that candle's close (TIME EXIT at the 15:10 close; DAILY LOSS EXIT at the newest close).

READ, THEN CATCH UP (Amar 2026-11-22). pending() compares the derivation with position_master and returns only
what is missing; a step from an older candle (a tick that failed for good) is sent late, marked late. A
position that is CLOSED or REFUSED takes no further step - whatever the derivation goes on to say. A stored
position this run cannot explain RAISES; rows from a newer run are left alone.
"""

import manage
import session as session_lib
from config import LAST_LIVE_INDEX_BAR, LOTS, T1_LOTS_BOOKED
from levels import ist_datetime, ist_time

ENTRY_EXIT, EXIT_ONLY, FORCE_EXIT = "entry_exit", "exit_only", "force_exit"
TIME_EXIT, DAILY_LOSS_EXIT = "TIME EXIT", "DAILY LOSS EXIT"

_STEP = {
    manage.T1: ("T1", T1_LOTS_BOOKED),
    manage.EXIT: ("EXIT", None),
    manage.REENTRY: ("RE-ENTRY", LOTS),
    manage.WATCH_ENDED: ("WATCH ENDED", None),
    manage.TARGET: ("TARGET", None),
    manage.STOP: ("STOP", None),
}


def _instrument(instrument):
    need = ("security_id", "instrument_type", "exchange_segment", "trading_symbol")
    missing = [k for k in need if not instrument.get(k)]
    if missing:
        raise RuntimeError(f"the payload's instrument is missing {missing} - order-manager needs the raw segment and symbol")
    return {k: str(instrument[k]) for k in need}


def _auction_step(bars, s):
    """The derivation's own closing-auction step - sent only on the risk manager's instruction."""
    return s["kind"] == manage.AUCTION_CLOSE or (s["kind"] == manage.WATCH_ENDED and ist_time(bars[s["index"]]["ts"]) >= LAST_LIVE_INDEX_BAR)


def _head(c, event):
    return f"{c['direction'].upper()} {event['pool']['name']}"


def signals(bars, result, instrument, session_start, playbook):
    """{(entry_ts, position_type): {"entry", "steps", "auction_text", "texts"}} for every entered idea."""
    inst = _instrument(instrument)
    out = {}
    for idea in result["ideas"]:
        c, e, n = idea["candidate"], idea["event"], idea["n"]
        entry_ts = int(bars[n]["ts"])
        fill = round(float(bars[n]["close"]), 2)
        base = {"playbook": playbook, "signal_key": f"{e['pool']['name']}|{e['side']}|{int(bars[e['sweep_index']]['ts'])}",
                "instrument": inst, "session_start": int(session_start), "entry_ts": entry_ts, "direction": c["direction"]}
        entry_text = session_lib.entry_text(bars, n, e, c)
        entry = dict(base, kind="ENTRY", candle_ts=entry_ts, price=fill, lots=LOTS, stop_loss=c["stop"],
                     t1_target=c.get("t1"), target=c["t2"], risk_pts=round(abs(fill - c["stop"]), 2), message=entry_text)
        steps, auction_text = [], None
        for s in idea["steps"]:
            if _auction_step(bars, s):
                auction_text = session_lib.step_text(bars, idea, s)
                continue
            if s["kind"] not in _STEP:
                raise RuntimeError(f"no record mapping for sweep step {s['kind']!r}")
            kind, lots = _STEP[s["kind"]]
            x = dict(base, kind=kind, candle_ts=int(bars[s["index"]]["ts"]), price=round(float(s["price"]), 2),
                     message=session_lib.step_text(bars, idea, s))
            if lots:
                x["lots"] = lots
            if kind == "EXIT":
                x["watch"] = True
            steps.append(x)
        key = (entry_ts, "LONG" if c["direction"] == "long" else "SHORT")
        if key in out:
            raise RuntimeError(f"two entered ideas share one position key {key}")
        out[key] = {"entry": entry, "steps": steps, "auction_text": auction_text, "head": _head(c, e), "candidate": c,
                    "base": base}
    return out


def _on(x, ts):
    return x is not None and int(x) == int(ts)


def applied(row, s):
    """Is this step - on the row's last applied candle - already in the row? (Its effect, as order-manager records it.)"""
    k, ts = s["kind"], s["candle_ts"]
    if k == "T1":
        return _on(row["t1_booked_ts"], ts)
    if k == "RE-ENTRY":
        return _on(row["reentry_ts"], ts)
    if k == "EXIT":
        return row["status"] in ("WATCHING", "CLOSED")
    return row["status"] == "CLOSED" and _on(row["closed_ts"], ts)


def _late(s, newest_ts):
    if s.get("message") and s["candle_ts"] < int(newest_ts):
        s = dict(s, message=s["message"] + f"\n(late: this step was on the {ist_datetime(s['candle_ts']).strftime('%H:%M')} candle)")
    return s


def pending(derived, stored, newest_ts, mode=ENTRY_EXIT, why=""):
    """
    The signals order-manager does not have yet, in order. Raises on a stored position the run cannot explain.

    A step on a candle AFTER the row's last applied one is missing; a step ON that candle only if its effect is not
    in the row (two steps can share a candle). A CLOSED or REFUSED position takes nothing more. Outside entry_exit:
    an idea with no row is sent as REJECTED, and a RE-ENTRY ends the watch instead (and the idea's later steps go).
    """
    by_key = {(int(r["entry_ts"]), r["position_type"]): r for r in stored}
    for key, r in by_key.items():
        if key not in derived and key[0] <= int(newest_ts) and r["status"] != "REFUSED":
            raise RuntimeError(f"position_master holds a sweep position entered at {key[0]} ({key[1]}, {r['status']}) "
                               f"that this run does not derive - the derivation and the record disagree")
    todo = []
    for key in sorted(derived):
        d = derived[key]
        row = by_key.get(key)
        if row is None:
            if mode != ENTRY_EXIT:
                todo.append(_late(dict(d["entry"], kind="REJECTED", refused_reason=why or mode,
                                       message=f"SWEEP SIGNAL REJECTED - {why or mode}\n{d['entry']['message']}"), newest_ts))
                continue
            steps = [d["entry"]] + d["steps"]
        elif row["status"] in ("CLOSED", "REFUSED"):
            continue
        else:
            last = int(row["last_update"])
            steps = [s for s in d["steps"] if s["candle_ts"] > last or (s["candle_ts"] == last and not applied(row, s))]
        for s in steps:
            if mode != ENTRY_EXIT and s["kind"] == "RE-ENTRY":
                stamp = ist_datetime(s["candle_ts"] + 300).strftime("%Y-%m-%d %H:%M")
                todo.append(_late(dict(d["base"], kind="WATCH ENDED", candle_ts=s["candle_ts"], price=s["price"],
                                       message=f"SWEEP WATCH ENDED {d['head']} - {stamp}\nre-entry refused by the risk manager: "
                                               f"{why or mode} - the watch ends\nfrom: sweep playbook"), newest_ts))
                break
            todo.append(_late(s, newest_ts))
    return todo


def forced_closes(derived, status, bars, force, why=""):
    """
    force_exit: a close for every position still OPEN or WATCHING after this run's own steps.

    TIME EXIT closes at the 15:10 candle's close (the 15:15 price - Amar 2026-11-20), with the text the playbook has
    always sent for it; DAILY LOSS EXIT at the newest close. `status` = {key: status after this run's steps}.
    """
    if force == TIME_EXIT:
        bar = next((b for b in bars if ist_time(b["ts"]) >= LAST_LIVE_INDEX_BAR), bars[-1])
    else:
        bar = bars[-1]
    px, ts = round(float(bar["close"]), 2), int(bar["ts"])
    out = []
    for key in sorted(derived):
        if status.get(key) not in ("OPEN", "WATCHING"):
            continue
        d = derived[key]
        c = d["candidate"]
        stamp = ist_datetime(ts + 300).strftime("%Y-%m-%d %H:%M")
        if force == TIME_EXIT and d["auction_text"]:
            text = d["auction_text"]
        elif force == TIME_EXIT:
            text = (f"SWEEP {manage.AUCTION_CLOSE} {d['head']} - {stamp}\nthe closing auction freezes the index at 15:15 - "
                    f"out at {px:.2f} (entry {c['entry']}, stop {c['stop']}, T2 {c['t2']})\nfrom: sweep playbook")
        else:
            text = (f"SWEEP DAILY LOSS EXIT {d['head']} - {stamp}\nthe risk manager's daily loss limit: {why or force} - "
                    f"out at {px:.2f} (entry {c['entry']}, stop {c['stop']}, T2 {c['t2']})\nfrom: sweep playbook")
        out.append(dict(d["base"], kind=force, candle_ts=ts, price=px, message=text))
    return out
