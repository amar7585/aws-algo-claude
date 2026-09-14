"""
Neon access — the trading calendar only.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders), which is why the reads below are written with %s
rather than named parameters.
"""

import datetime
import logging

from config import HOLIDAY_TABLE, NEXT_SESSION_HORIZON_DAYS

logger = logging.getLogger()


def holiday_for(conn, trade_date):
    """The holiday on `trade_date`, or None when it is a normal session.

    A ROW MEANS CLOSED; NO ROW MEANS A NORMAL SESSION. algo.trading_holiday
    stores closures only, so "absent" is both the common answer and the
    permissive one — a calendar nobody reseeded keeps the system trading
    instead of making it go quiet. That asymmetry is the point, and the
    reasoning is in trading-calendar/README.md.

    Returns the description where one is known. Most rows have none: the seed
    source publishes dates without names, so the label is for humans reading
    logs, never for logic.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT description FROM {HOLIDAY_TABLE} WHERE trade_date = %s",
        (trade_date,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return row[0] or "unnamed holiday"


def next_trading_day(conn, after):
    """The next weekday that is not a stored closure, or None.

    For the holiday notice only — nothing decides anything on this. It answers
    the one question a holiday raises ("when do we resume"), and it answers it
    the same way the gate does: a weekday with no row is a session.

    None means the horizon ran out, which in practice means the calendar has
    not been reseeded. The notice says so rather than inventing a date.
    """
    start = after + datetime.timedelta(days=1)
    end = start + datetime.timedelta(days=NEXT_SESSION_HORIZON_DAYS)

    cursor = conn.cursor()
    cursor.execute(
        f"SELECT trade_date FROM {HOLIDAY_TABLE} "
        "WHERE trade_date >= %s AND trade_date <= %s",
        (start, end),
    )
    closed = {row[0] for row in cursor.fetchall()}

    day = start
    while day <= end:
        if day.weekday() < 5 and day not in closed:
            return day
        day += datetime.timedelta(days=1)
    return None
