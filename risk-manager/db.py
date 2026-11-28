"""
Neon reads for risk-manager - READ-ONLY. It writes no table: its kill-switch closes go through
order-manager, the only writer of algo.position_master; algo.risk_control is written by Amar's INSERTs.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo, schema algo - the connection
string comes only from /algo/neon/connection. pg8000 `format` paramstyle (%s).
"""

import decimal

from config import CONTROL_TABLE, HOLIDAY_TABLE, OPTION_TABLE, POSITION_TABLE

POSITION_COLUMNS = (
    "position_id", "playbook", "signal_key", "session_start", "security_id", "instrument_type", "exchange_segment",
    "trading_symbol", "position_type", "entry_ts", "lot_size", "buy_qty", "sell_qty", "open_avg", "stop_loss",
    "tsl_stop_loss", "t1_target", "target", "risk_pts", "status", "realized_profit", "t1_booked_ts", "reentry_ts",
    "closed_ts", "exit_reason", "last_update",
)


def _plain(v):
    return float(v) if isinstance(v, decimal.Decimal) else v


def read_control(conn, now):
    """The risk_control row in force (the newest with set_at <= now). None in the table RAISES - nothing may run.
    `daily_loss_limit` (index point-lots) is read for the log only; the check uses `daily_loss_limit_rupees`."""
    cur = conn.cursor()
    cur.execute(f"SELECT set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, daily_loss_limit_rupees, "
                f"set_by, reason FROM {CONTROL_TABLE} WHERE set_at <= %s ORDER BY set_at DESC LIMIT 1", [int(now)])
    rows = cur.fetchall()
    if not rows:
        raise RuntimeError(f"{CONTROL_TABLE} has no row in force - no kill-switch state, no limits; nothing may run")
    cols = ("set_at", "kill_switch", "max_open", "max_trades_per_day", "daily_loss_limit", "daily_loss_limit_rupees",
            "set_by", "reason")
    return {c: _plain(v) for c, v in zip(cols, rows[0])}


OPTION_COLUMNS = ("position_id", "option_security_id", "exchange_segment", "trading_symbol", "open_units", "buy_value",
                  "sell_value", "charges_est")


def read_options(conn, position_ids):
    """The paper option rows of these positions (order-manager writes them; read-only here)."""
    ids = sorted({int(i) for i in position_ids})
    if not ids:
        return []
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(OPTION_COLUMNS)} FROM {OPTION_TABLE} WHERE position_id IN "
                f"({', '.join(['%s'] * len(ids))}) ORDER BY position_id", ids)
    return [{c: _plain(v) for c, v in zip(OPTION_COLUMNS, r)} for r in cur.fetchall()]


def read_holidays(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT trade_date FROM {HOLIDAY_TABLE}")
    return {r[0] for r in cur.fetchall()}


def read_positions(conn, instrument, session_start):
    """Every position of the session, every playbook (REFUSED included - they count for nothing but are shown)."""
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(POSITION_COLUMNS)} FROM {POSITION_TABLE} "
                f"WHERE security_id = %s AND instrument_type = %s AND session_start = %s ORDER BY position_id",
                [str(instrument["security_id"]), str(instrument["instrument_type"]), int(session_start)])
    return [{c: _plain(v) for c, v in zip(POSITION_COLUMNS, r)} for r in cur.fetchall()]
