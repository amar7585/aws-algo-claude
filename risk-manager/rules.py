"""
The risk manager's decision - PURE: (the control row, today's positions, the newest candles) -> the run block.

Amar 2026-11-22 / 11-23. The hard rules live here, for every playbook; the playbooks keep their own
entry and exit strategies (stop, target, exit conditions) and close their own positions:

  kill switch    on for the session it was set in, else the next session (set outside session hours);
                 every open or watching position is closed here, and no playbook runs
  15:00          no new trade on a candle closing at or after 15:00 (exit only)
  time exit      from the 15:10 candle (the 15:16 tick): every holder gets force_exit("TIME EXIT") and
                 closes at that candle's close AFTER its own stop / target on it
  limits         max open, max trades per day, daily loss; NULL = off. The daily loss is in OPTION RUPEES since
                 2026-11-28 (Amar: "Option rupees", "₹20,000"): the paper option record's cash + open options at
                 their live marks, against daily_loss_limit_rupees - it replaced the point-lot check. A breach =
                 force_exit("DAILY LOSS EXIT") and no new trade for the rest of the day
  modes          entry_exit | exit_only (with why) | force_exit (with the reason; no entries either)

A TICK playbook (the sweep) gets a mode on every tick. An ALERT playbook (trend) gets a mode on its alert,
and on a tick only while it holds a position - then never entry_exit (it enters only on alerts), and with
its position rows, so it needs no database.
"""

import datetime
import decimal

from config import NO_NEW_TRADE_FROM_CLOSE, RUN_VERSION, SESSION_CLOSE, TICK_SOURCE, TIME_EXIT_CANDLE

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OPEN, WATCHING, CLOSED, REFUSED = "OPEN", "WATCHING", "CLOSED", "REFUSED"
TIME_EXIT, DAILY_LOSS_EXIT, KILL_SWITCH_EXIT = "TIME EXIT", "DAILY LOSS EXIT", "KILL SWITCH EXIT"
ENTRY_EXIT, EXIT_ONLY, FORCE_EXIT = "entry_exit", "exit_only", "force_exit"


def _t(ts):
    return datetime.datetime.fromtimestamp(int(ts), IST)


def _f(x):
    return float(x) if isinstance(x, decimal.Decimal) else x


def previous_session_close(session_start, holidays):
    """15:30 IST of the trading day before the session (weekends and `holidays` skipped)."""
    d = _t(session_start).date() - datetime.timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d -= datetime.timedelta(days=1)
    return int(datetime.datetime.combine(d, SESSION_CLOSE, IST).timestamp())


def kill_switch_on(control, prev_close):
    """On for this session: the row in force says on, and it was set after the previous session's close."""
    return bool(control["kill_switch"]) and int(control["set_at"]) > int(prev_close)


def balance(p):
    return (p["buy_qty"] - p["sell_qty"]) if p["position_type"] == "LONG" else (p["sell_qty"] - p["buy_qty"])


def option_rupees(options, marks):
    """The day's paper-option result in rupees: cash so far (received - paid - estimated charges) + every open option
    at its live mark. A missing mark for an open option RAISES (a limit must never be judged on a guess)."""
    total = 0.0
    for o in options:
        total += float(o["sell_value"]) - float(o["buy_value"]) - float(o["charges_est"])
        if int(o["open_units"]) > 0:
            px = (marks or {}).get(str(o["option_security_id"]))
            if px is None:
                raise RuntimeError(f"no live mark for the open option {o['trading_symbol']} ({o['option_security_id']})")
            total += int(o["open_units"]) * float(px)
    return total


def assess(positions, bars, control, options=(), marks=None):
    """The day's state at the newest closed candle, and what it allows."""
    newest = bars[-1]
    close = float(newest["close"])
    live = [p for p in positions if p["status"] in (OPEN, WATCHING)]
    open_ = [p for p in positions if p["status"] == OPEN]
    trades = [p for p in positions if p["status"] != REFUSED]
    realised = sum(float(p["realized_profit"]) / int(p["lot_size"]) for p in trades)
    marked = sum(balance(p) * (1 if p["position_type"] == "LONG" else -1) * (close - float(p["open_avg"])) / int(p["lot_size"])
                 for p in open_)
    pnl = realised + marked                                    # index points x lots - for the log only since 2026-11-28
    # THE DAILY-LOSS LIMIT IS IN OPTION RUPEES (Amar 2026-11-28: "Option rupees", "₹20,000"). It replaced the point-lot
    # check: the day's paper-option result (cash + open options at their live marks) against daily_loss_limit_rupees.
    rupees = option_rupees(options, marks)
    limit = control.get("daily_loss_limit_rupees")
    limit = None if limit is None else float(limit)
    loss_hit = limit is not None and (rupees <= -limit or any(p.get("exit_reason") == DAILY_LOSS_EXIT for p in positions))
    candle_close = _t(int(newest["ts"]) + 300).time()
    time_exit = _t(int(newest["ts"])).time() >= TIME_EXIT_CANDLE
    why = []
    if candle_close >= NO_NEW_TRADE_FROM_CLOSE:
        why.append("no new trade after 15:00")
    mo, mt = control.get("max_open"), control.get("max_trades_per_day")
    if mo is not None and len(open_) >= int(mo):
        why.append(f"max open positions {len(open_)}/{int(mo)}")
    if mt is not None and len(trades) >= int(mt):
        why.append(f"max trades per day {len(trades)}/{int(mt)}")
    if loss_hit:
        why.append(f"daily loss limit ₹{limit:,.0f} hit (option ₹{rupees:+,.0f})")
    force = TIME_EXIT if time_exit else (DAILY_LOSS_EXIT if loss_hit else None)
    return {"newest_ts": int(newest["ts"]), "close": close, "open": len(open_), "live": len(live), "trades": len(trades),
            "pnl_point_lots": round(pnl, 2), "option_rupees": round(rupees, 2), "loss_hit": loss_hit, "force": force,
            "why": why}


def _rows(positions, playbook, newest_ts):
    """This playbook's open / watching positions AS OF the tick's newest candle. A late or retried older tick
    (Lambda's async retry, an out-of-order delivery) reads today's rows now, and may see a position entered on a
    later candle - one its candles cannot follow, so it is left out (the newer tick follows it)."""
    keep = ("position_id", "playbook", "signal_key", "session_start", "security_id", "instrument_type", "exchange_segment",
            "trading_symbol", "position_type", "entry_ts", "lot_size", "buy_qty", "sell_qty", "open_avg", "stop_loss",
            "tsl_stop_loss", "t1_target", "target", "risk_pts", "status", "realized_profit", "t1_booked_ts", "reentry_ts",
            "closed_ts", "exit_reason", "last_update")
    return [{k: _f(p.get(k)) for k in keep} for p in positions
            if p["playbook"] == playbook and p["status"] in (OPEN, WATCHING) and int(p["entry_ts"]) <= int(newest_ts)]


def build_run(source, a, positions, tick_playbooks, alert_playbooks):
    """The run block: {"version", "kill_switch", "assessment", "playbooks": {name: {"mode", "force", "why", "positions"}}}."""
    if a["force"]:
        base = {"mode": FORCE_EXIT, "force": a["force"], "why": "; ".join(a["why"]) or a["force"]}
    elif a["why"]:
        base = {"mode": EXIT_ONLY, "force": None, "why": "; ".join(a["why"])}
    else:
        base = {"mode": ENTRY_EXIT, "force": None, "why": ""}
    playbooks = {}
    newest = a["newest_ts"]
    if source == TICK_SOURCE:
        for fn in tick_playbooks:
            playbooks[fn] = dict(base, positions=_rows(positions, fn, newest))
        for fn in alert_playbooks:
            mine = _rows(positions, fn, newest)
            if mine:   # an alert playbook follows its positions on a tick; it never enters on one
                m = base if base["mode"] == FORCE_EXIT else {"mode": EXIT_ONLY, "force": None, "why": base["why"] or "exit mode"}
                playbooks[fn] = dict(m, positions=mine)
    else:
        for fn in alert_playbooks:
            playbooks[fn] = dict(base, positions=_rows(positions, fn, newest))
    return {"version": RUN_VERSION, "kill_switch": False, "assessment": a, "playbooks": playbooks}


def kill_switch_closes(positions, bars, playbook_names=None):
    """The KILL SWITCH EXIT signals for every open or watching position, at the newest closed candle's close."""
    newest = bars[-1]
    close = round(float(newest["close"]), 2)
    out = []
    for p in positions:
        if p["status"] not in (OPEN, WATCHING):
            continue
        direction = "long" if p["position_type"] == "LONG" else "short"
        text = (f"KILL SWITCH - {p['playbook']} {direction.upper()} closed at {close:.2f} "
                f"({_t(int(newest['ts']) + 300).strftime('%Y-%m-%d %H:%M')})\n"
                f"entry {float(p['open_avg']):.2f}, {balance(p)} units; the database kill switch is on - no playbook runs "
                f"for the rest of the session\nfrom: risk manager")
        out.append({"kind": KILL_SWITCH_EXIT, "playbook": p["playbook"], "signal_key": p["signal_key"],
                    "instrument": {"security_id": p["security_id"], "instrument_type": p["instrument_type"],
                                   "exchange_segment": p["exchange_segment"], "trading_symbol": p["trading_symbol"]},
                    "session_start": int(p["session_start"]), "entry_ts": int(p["entry_ts"]), "direction": direction,
                    "candle_ts": max(int(newest["ts"]), int(p["last_update"])), "price": close, "message": text})
    return out
