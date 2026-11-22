"""
The position state machine - PURE: (the stored row or None, one signal) -> what to do.

ONE RULE: a step is applied only if it moves the position forward. That is what makes
every call safe to repeat - an async retry, a late older run, two alerts on one candle
- without an order table (Amar 2026-11-22: "just dont save order directly save the
position in position master"):

    kind               applies when                                change
    ENTRY              no row on the unique key                    insert OPEN, lots x lot_size units at price
    REJECTED           no row on the unique key                    insert REFUSED, no quantity, the reason
    T1                 OPEN, no T1 yet, units < balance            close `lots` x lot_size at price
    EXIT (watch)       OPEN                                        close the balance -> WATCHING (the sweep)
    EXIT / STOP / TARGET / TRAIL / TIME EXIT / DAILY LOSS EXIT / KILL SWITCH EXIT
                       OPEN                                        close the balance -> CLOSED
                       WATCHING (not EXIT)                         the watch ends -> CLOSED, no quantity
    WATCH ENDED        WATCHING                                    -> CLOSED, no quantity
    RE-ENTRY           WATCHING, no re-entry yet                   lots x lot_size units at price -> OPEN
    STOP MOVE          OPEN, the stop moves the trade's way        tsl_stop_loss

  duplicate        the same step again (same candle), an opening signal for a position that
                   exists (a retry, or a second alert on the same candle), or a step older than
                   the last one applied (a late run) - nothing changes, nothing is sent
  ignored          any step for a REFUSED position - its later steps are never sent
  anything else    RAISES (fail loudly): the caller's derivation and the record disagree

Quantities are UNITS (lots x lot_size). Realised profit = index points x units, exact:
prices are carried as Decimal, and open_avg is the price of the lots still open (an
entry and a re-entry each set it; T1 and the closes do not change it).
"""

import decimal
import re

IST_OFFSET_SECONDS = 19800

ENTRY, REJECTED, T1, EXIT, WATCH_ENDED, REENTRY = "ENTRY", "REJECTED", "T1", "EXIT", "WATCH ENDED", "RE-ENTRY"
STOP, TARGET, TRAIL, STOP_MOVE = "STOP", "TARGET", "TRAIL", "STOP MOVE"
TIME_EXIT, DAILY_LOSS_EXIT, KILL_SWITCH_EXIT = "TIME EXIT", "DAILY LOSS EXIT", "KILL SWITCH EXIT"
OPENING = (ENTRY, REJECTED)
CLOSING = (EXIT, STOP, TARGET, TRAIL, TIME_EXIT, DAILY_LOSS_EXIT, KILL_SWITCH_EXIT)
KINDS = OPENING + (T1, WATCH_ENDED, REENTRY, STOP_MOVE) + CLOSING

APPLIED, DUPLICATE, REJECTED_STORED, IGNORED = "applied", "duplicate", "rejected_stored", "ignored"
OPEN, WATCHING, CLOSED, REFUSED = "OPEN", "WATCHING", "CLOSED", "REFUSED"
LONG, SHORT = "LONG", "SHORT"

_SEGMENT = re.compile(r"^[A-Z]+_[A-Z]+$")   # a raw Dhan segment code: IDX_I, NSE_FNO, BSE_FNO, NSE_EQ ...


class Decision:
    def __init__(self, result, op=None, fields=None, send=False, why=""):
        self.result, self.op, self.fields, self.send, self.why = result, op, fields or {}, send, why


def D(x):
    """A price as an exact Decimal (via str, so 25175.42 stays 25175.42)."""
    return None if x is None else decimal.Decimal(str(x))


def session_of(ts):
    """IST midnight of the session a candle START belongs to."""
    ts = int(ts)
    return ((ts + IST_OFFSET_SECONDS) // 86400) * 86400 - IST_OFFSET_SECONDS


def position_type(sig):
    return LONG if sig["direction"] == "long" else SHORT


def key(sig):
    i = sig["instrument"]
    return (str(i["security_id"]), str(i["instrument_type"]), sig["playbook"], int(sig["entry_ts"]), position_type(sig))


# ---------------------------------------------------------------------------
# Validation - the shape of one signal (raises with the field named)
# ---------------------------------------------------------------------------
def _need(sig, *fields):
    missing = [f for f in fields if sig.get(f) is None]
    if missing:
        raise ValueError(f"{sig.get('kind')} signal is missing {missing}")


def validate(sig):
    if not isinstance(sig, dict):
        raise ValueError(f"`signal` must be an object, got {type(sig).__name__}")
    kind = sig.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown signal kind {kind!r} - one of {list(KINDS)}")
    _need(sig, "playbook", "signal_key", "instrument", "session_start", "entry_ts", "direction", "candle_ts")
    if sig["direction"] not in ("long", "short"):
        raise ValueError(f"direction must be long or short, got {sig['direction']!r}")
    for name in ("instrument", "underlying"):
        inst = sig.get(name)
        if inst is None and name == "underlying":
            continue
        if not isinstance(inst, dict):
            raise ValueError(f"`{name}` must be an object")
        miss = [f for f in ("security_id", "instrument_type", "exchange_segment", "trading_symbol") if not inst.get(f)]
        if miss:
            raise ValueError(f"`{name}` is missing {miss}")
        if not _SEGMENT.match(str(inst["exchange_segment"])):
            raise ValueError(f"`{name}.exchange_segment` {inst['exchange_segment']!r} is not a raw Dhan segment code")
    for f in ("session_start", "entry_ts", "candle_ts"):
        if not isinstance(sig[f], int) or isinstance(sig[f], bool):
            raise ValueError(f"`{f}` must be epoch seconds (int), got {sig[f]!r}")
    if sig["session_start"] != session_of(sig["entry_ts"]):
        raise ValueError(f"session_start {sig['session_start']} is not IST midnight of entry_ts {sig['entry_ts']}")
    if sig["candle_ts"] < sig["entry_ts"]:
        raise ValueError("candle_ts is before entry_ts")
    if kind in OPENING and sig["candle_ts"] != sig["entry_ts"]:
        raise ValueError(f"{kind} must be on its entry candle (candle_ts == entry_ts)")
    if kind in (ENTRY, T1, REENTRY):
        _need(sig, "price", "lots")
    if kind in CLOSING:
        _need(sig, "price")
    if kind == REJECTED:
        _need(sig, "refused_reason")
    if kind == STOP_MOVE:
        _need(sig, "stop_loss")
    if sig.get("lots") is not None and (not isinstance(sig["lots"], int) or isinstance(sig["lots"], bool) or sig["lots"] <= 0):
        raise ValueError(f"`lots` must be a positive int, got {sig['lots']!r}")
    if sig.get("price") is not None and D(sig["price"]) <= 0:
        raise ValueError(f"`price` must be positive, got {sig['price']!r}")
    if sig.get("message") is not None and not isinstance(sig["message"], str):
        raise ValueError("`message` must be text or null")
    return sig


# ---------------------------------------------------------------------------
# Arithmetic on a row (a dict of the stored columns, numerics as Decimal)
# ---------------------------------------------------------------------------
def balance(row):
    return (row["buy_qty"] - row["sell_qty"]) if row["position_type"] == LONG else (row["sell_qty"] - row["buy_qty"])


def _sign(pt):
    return 1 if pt == LONG else -1


def _add(row, side, units, price, out):
    """Units onto one side (buy / sell), its cumulative average re-weighted."""
    q, a = f"{side}_qty", f"{side}_avg"
    old_q = out.get(q, row[q]); old_a = out.get(a, row[a])
    new_q = old_q + units
    new_a = price if not old_q else ((D(old_a) * old_q + price * units) / new_q).quantize(decimal.Decimal("0.000001"))
    out[q], out[a] = new_q, new_a


def _close(row, units, price, out):
    """Close `units` at `price`: the opposite side's quantity, and the realised profit (points x units)."""
    pt = row["position_type"]
    _add(row, "sell" if pt == LONG else "buy", units, price, out)
    out["realized_profit"] = D(row["realized_profit"]) + _sign(pt) * (price - D(row["open_avg"])) * units


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def _insert(sig, lot_size, now):
    i = sig["instrument"]; u = sig.get("underlying") or i
    return {
        "playbook": sig["playbook"], "signal_key": str(sig["signal_key"]), "session_start": sig["session_start"],
        "security_id": str(i["security_id"]), "instrument_type": str(i["instrument_type"]),
        "exchange_segment": str(i["exchange_segment"]), "trading_symbol": str(i["trading_symbol"]),
        "underlying_security_id": str(u["security_id"]), "underlying_instrument_type": str(u["instrument_type"]),
        "underlying_exchange_segment": str(u["exchange_segment"]),
        "position_type": position_type(sig), "entry_ts": sig["entry_ts"], "lot_size": int(lot_size),
        "buy_qty": 0, "buy_avg": None, "sell_qty": 0, "sell_avg": None, "open_avg": None,
        "stop_loss": D(sig.get("stop_loss")), "tsl_stop_loss": None, "t1_target": D(sig.get("t1_target")),
        "target": D(sig.get("target")), "risk_pts": D(sig.get("risk_pts")),
        "refused_reason": None, "realized_profit": decimal.Decimal(0), "last_update": sig["candle_ts"],
        "created_at": now, "updated_at": now,
    }


def decide(row, sig, lot_size, now):
    kind, ts = sig["kind"], sig["candle_ts"]
    price = D(sig.get("price"))
    wants_send = sig.get("message") is not None

    # ---- no position yet: only an opening signal may create one ---------------------------
    if row is None:
        if kind not in OPENING:
            raise RuntimeError(f"{kind} for a position that does not exist ({key(sig)}) - its ENTRY was never recorded")
        if not lot_size or int(lot_size) <= 0:
            raise RuntimeError(f"no lot size for {sig['instrument']['trading_symbol']} - cannot size the position")
        f = _insert(sig, lot_size, now)
        if kind == REJECTED:
            f.update(status=REFUSED, refused_reason=str(sig["refused_reason"]))
            return Decision(REJECTED_STORED, "insert", f, wants_send, "a refused signal, stored")
        units = sig["lots"] * int(lot_size)
        f.update(status=OPEN, open_avg=price)
        _add(f, "buy" if f["position_type"] == LONG else "sell", units, price, f)
        return Decision(APPLIED, "insert", f, wants_send, f"opened {sig['lots']} lots ({units} units) at {price}")

    # ---- a position exists ------------------------------------------------------------------
    if kind in OPENING:
        return Decision(DUPLICATE, why="the position already exists (a retry, or a second alert on the same candle)")
    st = row["status"]
    if st == REFUSED:
        return Decision(IGNORED, why="a step for a REFUSED position - never sent")
    if ts < int(row["last_update"]):
        return Decision(DUPLICATE, why=f"older than the last step applied ({row['last_update']}) - a late run")
    f = {"last_update": ts, "updated_at": now}
    bal = balance(row)
    lot = int(row["lot_size"])

    if kind == T1:
        if row["t1_booked_ts"] is not None:
            if int(row["t1_booked_ts"]) == ts:
                return Decision(DUPLICATE, why="T1 already booked on this candle")
            raise RuntimeError(f"a second T1 at {ts} - T1 was booked at {row['t1_booked_ts']}")
        if st != OPEN:
            raise RuntimeError(f"T1 for a {st} position")
        units = sig["lots"] * lot
        if units >= bal:
            raise RuntimeError(f"T1 of {units} units would close the whole balance ({bal}) - T1 never ends a trade")
        _close(row, units, price, f)
        f["t1_booked_ts"] = ts
        return Decision(APPLIED, "update", f, wants_send, f"T1: {sig['lots']} lots ({units} units) at {price}")

    if kind == REENTRY:
        if row["reentry_ts"] is not None:
            if int(row["reentry_ts"]) == ts:
                return Decision(DUPLICATE, why="the re-entry is already applied")
            raise RuntimeError(f"a second re-entry at {ts} - the first was at {row['reentry_ts']}")
        if st != WATCHING:
            raise RuntimeError(f"RE-ENTRY for a {st} position")
        units = sig["lots"] * lot
        _add(row, "buy" if row["position_type"] == LONG else "sell", units, price, f)
        f.update(open_avg=price, reentry_ts=ts, status=OPEN)
        return Decision(APPLIED, "update", f, wants_send, f"re-entered {sig['lots']} lots ({units} units) at {price}")

    if kind == STOP_MOVE:
        new = D(sig["stop_loss"])
        cur = D(row["tsl_stop_loss"] if row["tsl_stop_loss"] is not None else row["stop_loss"])
        if cur is not None and new == cur:
            return Decision(DUPLICATE, why="the stop is already there")
        if st != OPEN:
            raise RuntimeError(f"STOP MOVE for a {st} position")
        if cur is not None and ((new < cur) if row["position_type"] == LONG else (new > cur)):
            raise RuntimeError(f"STOP MOVE from {cur} to {new} goes against the trade - a stop only moves its way")
        f["tsl_stop_loss"] = new
        return Decision(APPLIED, "update", f, wants_send, f"stop moved to {new}")

    if kind == WATCH_ENDED:
        if st == WATCHING:
            f.update(status=CLOSED, closed_ts=ts, exit_reason=WATCH_ENDED)
            return Decision(APPLIED, "update", f, wants_send, "the watch ended")
        if st == CLOSED and row["exit_reason"] == WATCH_ENDED and row["closed_ts"] is not None and int(row["closed_ts"]) == ts:
            return Decision(DUPLICATE, why="the watch already ended on this candle")
        raise RuntimeError(f"WATCH ENDED for a {st} position")

    # ---- the closes ----------------------------------------------------------------------------
    watch = bool(sig.get("watch"))
    if watch and kind != EXIT:
        raise ValueError("`watch` applies to an EXIT only")
    if st == OPEN:
        _close(row, bal, price, f)
        if watch:
            f.update(status=WATCHING, exit_reason=EXIT)
            return Decision(APPLIED, "update", f, wants_send, f"exit {bal} units at {price}, now watching")
        f.update(status=CLOSED, closed_ts=ts, exit_reason=kind)
        return Decision(APPLIED, "update", f, wants_send, f"{kind}: {bal} units at {price}, closed")
    if st == WATCHING:
        if kind == EXIT:
            if row["exit_reason"] == EXIT and int(row["last_update"]) == ts:
                return Decision(DUPLICATE, why="the exit is already applied on this candle")
            raise RuntimeError("a second EXIT while already out and watching")
        f.update(status=CLOSED, closed_ts=ts, exit_reason=kind)
        return Decision(APPLIED, "update", f, wants_send, f"{kind} while out - the watch ends, nothing to close")
    if st == CLOSED and row["exit_reason"] == kind and row["closed_ts"] is not None and int(row["closed_ts"]) == ts:
        return Decision(DUPLICATE, why=f"already closed by {kind} on this candle")
    raise RuntimeError(f"{kind} for a {st} position (closed by {row['exit_reason']} at {row['closed_ts']})")


def after(row, d):
    """The row as it stands once the decision is applied (for the answer to the caller)."""
    out = dict(row or {})
    out.update(d.fields)
    return out
