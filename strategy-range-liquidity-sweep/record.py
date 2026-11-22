"""
The paper position record (P2a, Amar 2026-11-22) - what order-manager must be told. PURE.

Every trade the playbook ENTERED (the gated ideas - the ones that sent SWEEP SIGNAL) becomes
signals for order-manager, one per step, in order:

    sweep step                          order-manager signal
    the entry (SWEEP SIGNAL)            ENTRY       3 lots at the confirming candle's close (the fill at the signal)
    T1 REACHED                          T1          2 lots at T1 (the range midpoint)
    EXIT                                EXIT        watch: true, at the close
    RE-ENTRY                            RE-ENTRY    3 lots at the close
    WATCH ENDED                         WATCH ENDED
    TARGET REACHED / STOP HIT           TARGET / STOP
    CLOSED AT 15:15                     TIME EXIT   at the 15:10 close

Every signal carries message: null - in P2a the playbook keeps sending its own messages.
Blocked ideas (the register's `blocked` rows) are not positions and are never recorded.

READ, THEN CATCH UP (Amar 2026-11-22). pending() compares the derivation with what
position_master already holds and returns only what is missing: an idea with no row gets its
ENTRY and every step so far; an idea with a row gets the steps from its last applied candle on
(order-manager answers `duplicate` for a step it already has). A stored position this run cannot
explain - entered on a candle the run has seen, but not derived - RAISES: the derivation and the
record disagree. Rows entered after this run's newest candle belong to a newer run (a late tick)
and are left alone.
"""

import manage
from config import LOTS, T1_LOTS_BOOKED

_STEP = {
    manage.T1: ("T1", T1_LOTS_BOOKED),
    manage.EXIT: ("EXIT", None),
    manage.REENTRY: ("RE-ENTRY", LOTS),
    manage.WATCH_ENDED: ("WATCH ENDED", None),
    manage.TARGET: ("TARGET", None),
    manage.STOP: ("STOP", None),
    manage.AUCTION_CLOSE: ("TIME EXIT", None),
}


def _instrument(instrument):
    need = ("security_id", "instrument_type", "exchange_segment", "trading_symbol")
    missing = [k for k in need if not instrument.get(k)]
    if missing:
        raise RuntimeError(f"the payload's instrument is missing {missing} - order-manager needs the raw segment and symbol")
    return {k: str(instrument[k]) for k in need}


def signals(bars, result, instrument, session_start, playbook):
    """{(entry_ts, position_type): [signal, ...]} for every entered idea, oldest step first."""
    inst = _instrument(instrument)
    out = {}
    for idea in result["ideas"]:
        c, e, n = idea["candidate"], idea["event"], idea["n"]
        entry_ts = int(bars[n]["ts"])
        fill = round(float(bars[n]["close"]), 2)
        base = {"playbook": playbook, "signal_key": f"{e['pool']['name']}|{e['side']}|{int(bars[e['sweep_index']]['ts'])}",
                "instrument": inst, "session_start": int(session_start), "entry_ts": entry_ts,
                "direction": c["direction"], "message": None}
        sig = [dict(base, kind="ENTRY", candle_ts=entry_ts, price=fill, lots=LOTS, stop_loss=c["stop"],
                    t1_target=c.get("t1"), target=c["t2"], risk_pts=round(abs(fill - c["stop"]), 2))]
        for s in idea["steps"]:
            if s["kind"] not in _STEP:
                raise RuntimeError(f"no record mapping for sweep step {s['kind']!r}")
            kind, lots = _STEP[s["kind"]]
            x = dict(base, kind=kind, candle_ts=int(bars[s["index"]]["ts"]), price=round(float(s["price"]), 2))
            if lots:
                x["lots"] = lots
            if kind == "EXIT":
                x["watch"] = True
            sig.append(x)
        key = (entry_ts, "LONG" if c["direction"] == "long" else "SHORT")
        if key in out:
            raise RuntimeError(f"two entered ideas share one position key {key}")
        out[key] = sig
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


def pending(derived, stored, newest_ts):
    """
    The signals order-manager does not have yet, in order. Raises on a stored position the run cannot explain.

    A step on a candle AFTER the row's last applied one is missing. A step ON that candle is missing only if
    its effect is not in the row - two steps can share a candle (T1 then EXIT; EXIT then WATCH ENDED), and a run
    that failed between them leaves the second to send. Earlier steps are in by construction. So a quiet tick
    sends nothing.
    """
    by_key = {(int(r["entry_ts"]), r["position_type"]): r for r in stored}
    for key, r in by_key.items():
        if key not in derived and key[0] <= int(newest_ts) and r["status"] != "REFUSED":
            raise RuntimeError(f"position_master holds a sweep position entered at {key[0]} ({key[1]}, {r['status']}) "
                               f"that this run does not derive - the derivation and the record disagree")
    todo = []
    for key in sorted(derived):
        row = by_key.get(key)
        if row is None:
            todo += derived[key]
            continue
        last = int(row["last_update"])
        todo += [s for s in derived[key][1:]
                 if s["candle_ts"] > last or (s["candle_ts"] == last and not applied(row, s))]
    return todo
