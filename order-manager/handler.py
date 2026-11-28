"""
Order Manager - AWS Lambda function

THE ONE COMMON ORDER MANAGER (Amar 2026-11-22: "common order manager like what we have
in algo_trading app just dont save order directly save the position in position
master"; "Its own Lambda"). The playbooks decide; every step they take - an entry, T1,
an exit, a re-entry, a stop move, a time exit - is handed here as ONE SIGNAL PER
SYNCHRONOUS CALL, and this function carries it out:

  * PAPER MODE (now): no order is saved and none is placed. The step updates the one
    position row in algo.position_master, and the caller's message goes to Telegram.
  * LIVE MODE (the Dhan phase, P5, its own approval): the same entry point will place
    the Dhan order. Only then is an order record kept - as trading-algo's OrderManager
    does for LIVE.

It is the ONLY writer of algo.position_master and the only sender of trade messages,
so a message always equals the record.

ONE TRANSACTION PER CALL: lock the row (SELECT ... FOR UPDATE) -> decide (positions.py,
pure) -> INSERT or UPDATE -> send the message (if the step was applied) -> mark it ->
COMMIT. A refused send raises and rolls back, so the caller's retry applies and sends
it again; a repeated step is a `duplicate` (nothing changes, nothing is sent). The
only duplicate message left is a send that succeeded followed by a commit that failed.

dry_run: true runs everything, statements included, then ROLLS BACK and sends nothing
- the deploy smoke.

FAIL LOUDLY. Anything inconsistent raises, so Lambda records an error, the caller's
synchronous invoke sees FunctionError and raises too, and error-notifier reports both.
Never a {"statusCode": 500} shape.

P1 (2026-11-22): built and deployed, CALLED BY NOTHING. The playbooks start calling it
in P2a (record only - their own messages unchanged), the risk manager in P2b.

PAPER TRADING ON THE OPTION (Amar 2026-11-28, build plan approved; OPTION_PAPER, off by
default). With it on, every APPLIED index step that changes quantity also buys / sells the
paper option 1 strike in the money (option.py) at Dhan's LIVE price - an option-chain read
on a buy, a quote on a sell (dhan.py, data APIs only: no order, no static IP) - and writes
algo.option_paper_position in the SAME transaction, before the message. A Dhan failure
raises and rolls the whole step back (the caller's retry runs it again). The trade message
gets one extra option line. The index record and the contract with the callers are
unchanged.

Modules:
    positions.py  the state machine (pure)
    option.py     the paper option: the contract, the expiry, the arithmetic, the line (pure)
    db.py         the row lock, the lot size, the insert / update (+ the option row)
    dhan.py       Dhan data reads: expiry list, option chain, quote
    params.py     the Dhan token and client id (auth-dhan-broker's /algo/dhan/token)
    notify.py     Telegram
    config.py     the contract version, the table, the switches
"""

import datetime
import logging
import time

from neon_access import connect, ist_datetime, read_neon_connection_string

import db
import option as opt
import positions as pos
from config import (CONTRACT_VERSION, OPTION_DEPTH_ITM, OPTION_PAPER, OPTION_PAPER_FROM, OPTION_SEGMENT, STRIKE_STEP,
                    TELEGRAM_ENABLED)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_EXPIRIES = {}      # (underlying security_id, session date) -> Dhan's expiry list, kept in the warm environment


def make_dhan():
    """A Dhan data client on auth-dhan-broker's token (read from SSM only when an option step needs it)."""
    from dhan import DhanClient
    from params import read_client_id, read_token_record
    record = read_token_record()
    return DhanClient(record["access_token"], read_client_id(record))


QUANTITY_KINDS = opt.OPENING + ("T1",) + opt.CLOSING


def option_step(cursor, sig, row, lot_size, now, dhan):
    """
    The paper option's side of an APPLIED index step (Amar 2026-11-28): reads Dhan, writes nothing.
    Returns {"op": "insert" | "update", "fields", "line", "answer"} or None (nothing for the option to do).
    RAISES on anything inconsistent or on any Dhan failure - the caller rolls the whole step back (O4: retry).
    """
    pid = row["position_id"] if row else None
    opt_row = db.lock_option(cursor, pid) if pid is not None else None
    if row is not None and opt_row is None:
        if int(row["entry_ts"]) < OPTION_PAPER_FROM:
            logger.info("position %s was entered before OPTION_PAPER_FROM (%s) - it has no option to follow", pid, OPTION_PAPER_FROM)
            return None
        raise RuntimeError(f"position {pid} has no option row although it was entered after the cut-over - its option entry was lost")
    act = opt.action(sig, opt_row, lot_size)
    if act is None:
        return None
    side, units = act
    inst = sig["instrument"]
    if side == opt.BUY:
        if opt_row is not None and int(opt_row["open_units"]) != 0:
            raise RuntimeError(f"a {sig['kind']} buys the option of position {pid} while {opt_row['open_units']} units are still open")
        day = datetime.datetime.fromtimestamp(int(sig["session_start"]), IST).date()
        key = (str(inst["security_id"]), day)
        if key not in _EXPIRIES:
            _EXPIRIES[key] = dhan.expiry_list(inst["security_id"], inst["exchange_segment"])
        expiry = opt.pick_expiry(_EXPIRIES[key], day)
        typ = opt.option_type(sig["direction"])
        strike = opt.strike_for(sig["price"], sig["direction"], OPTION_DEPTH_ITM, STRIKE_STEP)
        chain = dhan.option_chain(inst["security_id"], inst["exchange_segment"], expiry.isoformat())
        sid, px = opt.find_leg(chain, strike, typ)
        contract = {"option_security_id": sid, "exchange_segment": OPTION_SEGMENT, "strike": strike,
                    "trading_symbol": opt.symbol(inst["trading_symbol"], expiry, strike, typ), "expiry_ts": opt.expiry_ts(expiry)}
    else:
        index_units = pos.balance(row)
        if sig["kind"] != "T1" and int(opt_row["open_units"]) != int(index_units):
            raise RuntimeError(f"position {pid}: the option holds {opt_row['open_units']} units, the index {index_units}")
        contract = {k: opt_row[k] for k in ("option_security_id", "exchange_segment", "strike", "trading_symbol", "expiry_ts")}
        contract["strike"] = float(contract["strike"])
        px = dhan.last_prices(contract["exchange_segment"], [contract["option_security_id"]])[str(contract["option_security_id"])]
        typ = opt_row["option_type"]
    fills = list(opt_row["fills"]) if opt_row else []
    sale = opt.sale_rupees(fills, units, px) if side == opt.SELL else None
    fills.append({"kind": sig["kind"], "candle_ts": int(sig["candle_ts"]), "read_at": int(now), "side": side, "units": units,
                  "price": px, "charges": str(opt.charges(px, units, side)), **contract})
    fields = {**contract, **opt.totals(fills), "fills": fills, "updated_at": int(now)}
    if opt_row is None:
        fields.update(underlying_security_id=str(inst["security_id"]), option_type=typ, depth_itm=OPTION_DEPTH_ITM,
                      lot_size=int(lot_size), created_at=int(now))
    answer = {"side": side, "units": units, "price": px, "symbol": contract["trading_symbol"],
              "sale_rupees": float(sale) if sale is not None else None, "open_units": fields["open_units"]}
    return {"op": "insert" if opt_row is None else "update", "fields": fields,
            "line": opt.line(side, contract["trading_symbol"], units, px, sale), "answer": answer}


def read_event(event):
    if not isinstance(event, dict):
        raise ValueError(f"expected the order-manager payload object, got {type(event).__name__}")
    version = event.get("contract_version")
    if version != CONTRACT_VERSION:
        raise ValueError(f"contract_version {version!r} but this function is written against v{CONTRACT_VERSION} - "
                         f"the caller's signal shape cannot be trusted. Keys present: {sorted(event)}")
    dry_run = event.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError(f"`dry_run` must be true or false, got {dry_run!r}")
    return pos.validate(event.get("signal")), dry_run


def apply(conn, sig, dry_run, now, send, telegram_enabled=TELEGRAM_ENABLED, option_paper=OPTION_PAPER,
          dhan_factory=make_dhan):
    """One signal, one transaction. Returns the answer; raises on anything inconsistent."""
    dhan = None
    for attempt in (1, 2):
        cursor = conn.cursor()
        try:
            row = db.lock_position(cursor, pos.key(sig))
            lot_size, lot_from = None, None
            if row is None and sig["kind"] in pos.OPENING:
                lot_size, lot_from = db.near_month_lot_size(cursor, sig["instrument"]["trading_symbol"], sig["session_start"])
            d = pos.decide(row, sig, lot_size, now)
            pid = row["position_id"] if row else None
            # THE PAPER OPTION (2026-11-28): decided on the same row, priced from Dhan BEFORE anything is written, so a
            # Dhan failure rolls the whole step back and the caller's retry runs it again (O4).
            o = None
            if option_paper and d.result == pos.APPLIED and sig["kind"] in QUANTITY_KINDS:
                dhan = dhan or dhan_factory()
                o = option_step(cursor, sig, row, d.fields.get("lot_size") if row is None else row["lot_size"], now, dhan)
            if d.op == "insert":
                pid = db.insert_position(cursor, d.fields)
                if pid is None:
                    # A concurrent run inserted the same position between our lock and our insert. Start over on
                    # the stored row - it is now a duplicate. Twice in a row means something else is wrong.
                    conn.rollback()
                    if attempt == 2:
                        raise RuntimeError(f"the insert of {pos.key(sig)} conflicted twice")
                    logger.info("insert conflicted with a concurrent run - deciding again on the stored row")
                    continue
            elif d.op == "update":
                db.update_position(cursor, pid, d.fields)
            if o is not None:
                if o["op"] == "insert":
                    db.insert_option(cursor, dict(o["fields"], position_id=pid))
                else:
                    db.update_option(cursor, pid, {k: v for k, v in o["fields"].items() if k in db.OPTION_UPDATABLE})
            text = sig.get("message")
            if text is not None and o is not None:
                text = f"{text}\n{o['line']}"
            sent = False
            if d.send and d.result in (pos.APPLIED, pos.REJECTED_STORED):
                if dry_run:
                    logger.info("DRY RUN - would send: %s", text.replace("\n", " | "))
                elif telegram_enabled:
                    send(text)
                    db.mark_sent(cursor, pid, now)
                    sent = True
                else:
                    logger.info("WOULD SEND (telegram off): %s", text.replace("\n", " | "))
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            state = pos.after(row, d)
            bal = pos.balance(state) if state.get("position_type") else 0
            lot = int(state["lot_size"]) if state.get("lot_size") else None
            out = db.plain({
                "result": d.result, "kind": sig["kind"], "why": d.why, "position_id": pid, "status": state.get("status"),
                "balance_units": bal, "lots_open": (bal / lot) if lot else None, "lot_size": lot, "lot_size_from": lot_from,
                "realized_profit": state.get("realized_profit"), "sent": sent, "dry_run": dry_run,
            })
            if option_paper:            # off = the answer exactly as before
                out["option"] = o["answer"] if o else None
            return out
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    raise RuntimeError("unreachable")


# ONE CONNECTION KEPT ACROSS WARM INVOCATIONS (P2b, Amar 2026-11-23). A fresh connection is a TLS handshake
# from Mumbai to Neon in Singapore - ~1.4 s of the ~1.5 s a warm call took at the P1 deploy - and from P2b this
# function SENDS the trade messages, so that delay reached the phone. The connection lives in the execution
# environment; each call first checks it with SELECT 1 (and ends that read) and reconnects if Neon dropped it
# (autosuspend, a network blip). A call that fails drops the connection, so the next one starts clean.
_CONN = {"conn": None}


def connection(connector=None):
    """(conn, reused). `connector` is for tests; Lambda uses neon_access.connect."""
    c = _CONN["conn"]
    if c is not None:
        try:
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            c.rollback()
            return c, True
        except Exception:  # noqa: BLE001 - a dead connection is replaced, never trusted
            drop()
    c = (connector or (lambda: connect(read_neon_connection_string())))()
    _CONN["conn"] = c
    return c, False


def drop():
    c, _CONN["conn"] = _CONN["conn"], None
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    sig, dry_run = read_event(event)
    conn, reused = connection()
    try:
        from notify import send_telegram
        out = apply(conn, sig, dry_run, int(time.time()), send_telegram)
    except Exception:
        drop()   # the connection's state is unknown after a failure - the next call reconnects
        raise
    o = out.get("option")
    logger.info("%s %s %s %s entry %s, candle %s: %s - %s (position %s, %s, %s units open)%s%s in %.2fs (%s connection)",
                sig["playbook"], sig["kind"], sig["direction"], sig["instrument"]["trading_symbol"],
                ist_datetime(sig["entry_ts"]).strftime("%Y-%m-%d %H:%M"), ist_datetime(sig["candle_ts"]).strftime("%H:%M"),
                out["result"], out["why"], out["position_id"], out["status"], out["balance_units"],
                f"; option {o['side']} {o['symbol']} x{o['units']} @ {o['price']:.2f} ({o['open_units']} open)" if o else "",
                " DRY RUN, rolled back" if dry_run else "", time.monotonic() - started, "reused" if reused else "new")
    return out
