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

Modules:
    positions.py  the state machine (pure)
    db.py         the row lock, the lot size, the insert / update
    notify.py     Telegram
    config.py     the contract version, the table, the switches
"""

import logging
import time

from neon_access import connect, ist_datetime, read_neon_connection_string

import db
import positions as pos
from config import CONTRACT_VERSION, TELEGRAM_ENABLED

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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


def apply(conn, sig, dry_run, now, send, telegram_enabled=TELEGRAM_ENABLED):
    """One signal, one transaction. Returns the answer; raises on anything inconsistent."""
    for attempt in (1, 2):
        cursor = conn.cursor()
        try:
            row = db.lock_position(cursor, pos.key(sig))
            lot_size, lot_from = None, None
            if row is None and sig["kind"] in pos.OPENING:
                lot_size, lot_from = db.near_month_lot_size(cursor, sig["instrument"]["trading_symbol"], sig["session_start"])
            d = pos.decide(row, sig, lot_size, now)
            pid = row["position_id"] if row else None
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
            sent = False
            if d.send and d.result in (pos.APPLIED, pos.REJECTED_STORED):
                if dry_run:
                    logger.info("DRY RUN - would send: %s", sig["message"].replace("\n", " | "))
                elif telegram_enabled:
                    send(sig["message"])
                    db.mark_sent(cursor, pid, now)
                    sent = True
                else:
                    logger.info("WOULD SEND (telegram off): %s", sig["message"].replace("\n", " | "))
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            state = pos.after(row, d)
            bal = pos.balance(state) if state.get("position_type") else 0
            lot = int(state["lot_size"]) if state.get("lot_size") else None
            return db.plain({
                "result": d.result, "kind": sig["kind"], "why": d.why, "position_id": pid, "status": state.get("status"),
                "balance_units": bal, "lots_open": (bal / lot) if lot else None, "lot_size": lot, "lot_size_from": lot_from,
                "realized_profit": state.get("realized_profit"), "sent": sent, "dry_run": dry_run,
            })
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    raise RuntimeError("unreachable")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    started = time.monotonic()
    sig, dry_run = read_event(event)
    conn = connect(read_neon_connection_string())
    try:
        from notify import send_telegram
        out = apply(conn, sig, dry_run, int(time.time()), send_telegram)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    logger.info("%s %s %s %s entry %s, candle %s: %s - %s (position %s, %s, %s units open)%s in %.2fs",
                sig["playbook"], sig["kind"], sig["direction"], sig["instrument"]["trading_symbol"],
                ist_datetime(sig["entry_ts"]).strftime("%Y-%m-%d %H:%M"), ist_datetime(sig["candle_ts"]).strftime("%H:%M"),
                out["result"], out["why"], out["position_id"], out["status"], out["balance_units"],
                " DRY RUN, rolled back" if dry_run else "", time.monotonic() - started)
    return out
