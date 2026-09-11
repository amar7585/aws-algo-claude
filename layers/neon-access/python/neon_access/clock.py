"""
Epoch and IST handling.

Every time value stored by this system is epoch seconds (bigint). There are no
timestamptz columns and no timezone arithmetic in SQL; conversion to IST
happens at display, never in storage. Getting this wrong does not raise - it
writes a plausible number five and a half hours away from the truth - so there
is one implementation and it checks itself.
"""

import datetime
import time

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
IST_OFFSET_SECONDS = 19800


def ist_midnight_epoch(day):
    """
    Epoch seconds for IST midnight of `day`, verified before returning.

    The check is the point: (epoch + 19800) % 86400 must be 0. A day-grain
    value that is not exactly midnight IST means something upstream built it
    by hand, and this refuses rather than storing it.
    """
    epoch = int(datetime.datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())
    remainder = (epoch + IST_OFFSET_SECONDS) % 86400
    if remainder != 0:
        raise ValueError(
            f"epoch {epoch} for {day} is not IST midnight (remainder {remainder})"
        )
    return epoch


def ist_datetime(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), IST)


def today_ist():
    return datetime.datetime.now(IST).date()


def now_epoch():
    return int(time.time())
