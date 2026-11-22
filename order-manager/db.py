"""
Neon access for order-manager - the reads and the writes of algo.position_master.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo, schema
algo. That is the only database this function touches - a second Neon project carries
an `algo` schema with the same table names and incompatible column types, so the
connection string comes only from /algo/neon/connection.

pg8000's DB-API layer uses `format` paramstyle (%s placeholders), autocommit off: the
handler owns the transaction (lock -> decide -> write -> send -> commit). Nothing here
commits.
"""

import datetime
import decimal

from config import INSTRUMENT_TABLE, LOT_SIZE_EXCHANGE_SEGMENT, LOT_SIZE_INSTRUMENT_TYPE, POSITION_TABLE, UNIQUE_CONSTRAINT

KEY_COLUMNS = ("security_id", "instrument_type", "playbook", "entry_ts", "position_type")
INSERT_COLUMNS = (
    "broker_id", "trade_mode", "playbook", "signal_key", "session_start", "security_id", "instrument_type",
    "exchange_segment", "trading_symbol", "underlying_security_id", "underlying_instrument_type",
    "underlying_exchange_segment", "position_type", "product_type", "entry_ts", "lot_size", "buy_qty", "buy_avg",
    "sell_qty", "sell_avg", "open_avg", "stop_loss", "tsl_stop_loss", "t1_target", "target", "risk_pts", "status",
    "refused_reason", "realized_profit", "last_update", "created_at", "updated_at",
)
UPDATABLE = frozenset((
    "buy_qty", "buy_avg", "sell_qty", "sell_avg", "open_avg", "tsl_stop_loss", "status", "realized_profit",
    "t1_booked_ts", "reentry_ts", "closed_ts", "exit_reason", "last_update", "updated_at",
))
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _rows(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def lock_position(cursor, key, table=POSITION_TABLE):
    """The position on the unique key, row-locked for this transaction (None if there is none)."""
    cursor.execute(
        f"SELECT * FROM {table} WHERE " + " AND ".join(f"{c} = %s" for c in KEY_COLUMNS) + " FOR UPDATE",
        list(key))
    rows = _rows(cursor)
    if len(rows) > 1:
        raise RuntimeError(f"{len(rows)} positions on one unique key {key} - the constraint is missing")
    return rows[0] if rows else None


def lot_size_symbols(underlying_symbol, session_start):
    """The near-month future's symbol, then the next month's: <underlying>-<MON><YEAR>-FUT."""
    d = datetime.datetime.fromtimestamp(int(session_start), IST).date()
    nxt = (d.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    return [f"{underlying_symbol}-{MONTHS[m.month - 1]}{m.year}-FUT" for m in (d, nxt)]


def near_month_lot_size(cursor, underlying_symbol, session_start, table=INSTRUMENT_TABLE):
    """The near-month future's lot_units - the session month's contract, else the next month's. Raises if neither."""
    names = lot_size_symbols(underlying_symbol, session_start)
    cursor.execute(
        f"SELECT trading_symbol, lot_units FROM {table} WHERE instrument_type = %s AND exchange_segment = %s "
        f"AND trading_symbol IN (%s, %s)",
        [LOT_SIZE_INSTRUMENT_TYPE, LOT_SIZE_EXCHANGE_SEGMENT] + names)
    found = {r["trading_symbol"]: r["lot_units"] for r in _rows(cursor)}
    for n in names:
        if n in found:
            if not found[n] or int(found[n]) <= 0:
                raise RuntimeError(f"{n} has lot_units {found[n]!r} in {table}")
            return int(found[n]), n
    raise RuntimeError(f"no {names} in {table} - the instrument master is stale; cannot size the position")


def insert_position(cursor, fields, table=POSITION_TABLE, constraint=UNIQUE_CONSTRAINT):
    """INSERT ... ON CONFLICT DO NOTHING -> the new position_id, or None if a concurrent run got there first."""
    row = {"broker_id": "paper", "trade_mode": "PAPER", "product_type": "INTRADAY", **fields}
    missing = [c for c in INSERT_COLUMNS if c not in row]
    if missing:
        raise RuntimeError(f"insert is missing {missing}")
    cursor.execute(
        f"INSERT INTO {table} ({', '.join(INSERT_COLUMNS)}) VALUES ({', '.join(['%s'] * len(INSERT_COLUMNS))}) "
        f"ON CONFLICT ON CONSTRAINT {constraint} DO NOTHING RETURNING position_id",
        [row[c] for c in INSERT_COLUMNS])
    rows = cursor.fetchall()
    return int(rows[0][0]) if rows else None


def update_position(cursor, position_id, fields, table=POSITION_TABLE):
    bad = sorted(set(fields) - UPDATABLE)
    if bad:
        raise RuntimeError(f"update tries to set {bad} - not updatable")
    cols = sorted(fields)
    cursor.execute(
        f"UPDATE {table} SET {', '.join(f'{c} = %s' for c in cols)} WHERE position_id = %s",
        [fields[c] for c in cols] + [int(position_id)])
    if cursor.rowcount != 1:
        raise RuntimeError(f"update of position {position_id} affected {cursor.rowcount} rows, expected 1")


def mark_sent(cursor, position_id, now, table=POSITION_TABLE):
    cursor.execute(f"UPDATE {table} SET message_sent_at = %s WHERE position_id = %s", [int(now), int(position_id)])
    if cursor.rowcount != 1:
        raise RuntimeError(f"marking position {position_id} sent affected {cursor.rowcount} rows, expected 1")


def plain(row):
    """Decimals to floats, for the JSON answer."""
    return {k: float(v) if isinstance(v, decimal.Decimal) else v for k, v in (row or {}).items()}
