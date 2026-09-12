"""
Choosing the two expiries a snapshot describes.

Nothing about either is hardcoded - both come from Dhan's live expiry list, so
they roll themselves the way the futures month does in intraday-data-loader.

    nearest   the first expiry on or after today
    monthly   the first MONTHLY expiry strictly after the nearest

A monthly expiry is the LAST expiry in its calendar month. It is found by
grouping, never by counting weeks: measured 2026-09-12, the live list runs
2026-09-15, 09-22, 09-29, 10-06, 10-13, 10-27, 11-23, 12-29 - October's 10-20
is simply absent, so "the fourth Tuesday" and "the fifth entry" are both wrong
while "the last one in October" is right.

WHY "STRICTLY AFTER THE NEAREST" IS THE WHOLE RULE. In the last week of a
month the nearest expiry IS that month's monthly, and the two would name the
same contract - one row carrying the same numbers twice under two prefixes.
Taking the first monthly after the nearest rolls to the next month exactly
then, and does nothing at all on every other day:

    nearest 2026-09-15  ->  monthly 2026-09-29   (different months' worth apart)
    nearest 2026-09-29  ->  monthly 2026-10-27   (collision, rolled)

near_expiry_ts and mth_expiry_ts are therefore never equal, which the schema
relies on.
"""

import datetime
import logging

logger = logging.getLogger()


def _dates(raw):
    parsed = sorted({datetime.date.fromisoformat(str(d)[:10]) for d in raw})
    if not parsed:
        raise RuntimeError(f"expiry list carried no parseable dates: {raw!r}")
    return parsed


def monthlies(dates):
    """The last expiry in each calendar month, ascending.

    The final month in the list is the one place this can be wrong: if Dhan
    truncates the list mid-month, that month's last ENTRY is not its monthly
    expiry. It does not bite in practice because the monthly chosen here is
    always within a month or two of today, where the list is dense - but a
    caller reaching for the far end of the list should not trust it.
    """
    last = {}
    for day in dates:
        last[(day.year, day.month)] = day
    return sorted(last.values())


def select(raw, today):
    """(nearest, monthly) as dates. See the module docstring."""
    dates = _dates(raw)
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        raise RuntimeError(
            f"every expiry Dhan returned is before {today}: {raw!r} - the "
            f"expiry list is stale and no chain can be resolved"
        )
    nearest = upcoming[0]

    later = [m for m in monthlies(dates) if m > nearest]
    if not later:
        raise RuntimeError(
            f"no monthly expiry after {nearest} in {raw!r} - the expiry list "
            f"does not reach far enough to resolve the monthly chain"
        )
    monthly = later[0]

    if monthly.month != nearest.month:
        logger.info(
            "nearest %s is its month's last expiry - monthly rolled to %s",
            nearest, monthly,
        )
    logger.info("expiries: nearest %s, monthly %s", nearest, monthly)
    return nearest, monthly


def futures_month(nearest):
    """
    The current futures contract's month.

    The nearest OPTION expiry's month names the current futures contract, and
    it rolls itself: once September's monthly expiry has passed, the nearest
    expiry is already in October. Same rule intraday-data-loader uses, and it
    takes the NEAREST expiry rather than the monthly one chosen above - the
    two disagree in the last week of a month, and it is the nearest that is
    right here.
    """
    return nearest.month, nearest.year
