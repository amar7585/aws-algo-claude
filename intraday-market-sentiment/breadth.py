"""
Market breadth - advance/decline for the NIFTY 50 and the NIFTY 500.

WHAT BREADTH IS. How many stocks in an index are up on the day versus down. A
market can rise on a handful of heavyweights while most of its members fall;
breadth is what tells those apart, and the index level alone cannot.

ONE FETCH, TWO UNIVERSES. The NIFTY 50 is a subset of the NIFTY 500, so the
whole roster is quoted once through /marketfeed/ohlc (one request of ~500,
under the 1000-per-call limit) and counted twice - once over the 500, once over
just the 50. The NIFTY 50 breadth therefore costs no extra API call.

ADVANCING VERSUS THE PREVIOUS CLOSE. A stock advances when its live last_price
is above the PREVIOUS trading day's close (ohlc.close), declines when below,
and is unchanged when exactly equal - the classic definition. A stock whose
quote is missing or non-positive (suspended, not yet traded) is dropped, not
counted as unchanged, so the three counts sum to the members that carried a
usable quote, never to the roster.

adv_dec_ratio = advances / declines, left NULL when declines = 0: advances/0 is
undefined and would read as an off-the-scale bull count. The raw counts are
always stored, so the ratio is recoverable - the same discipline the OI deltas
use.

WHERE THE ROSTERS COME FROM. Dhan has no index-membership endpoint, so the 50
and 500 are read from algo.index_constituents, seeded and maintained by hand
(see the schema and the migration). The read carries the exchange_segment, so
the /marketfeed/ohlc request groups by it with no join.

ALWAYS ON, WITH A FALLBACK. Breadth runs every invocation. When the NIFTY 50
roster is missing from the database it falls back to a built-in static list
(config.STATIC_NIFTY50), so the index breadth is produced even against an
unseeded table; the NIFTY 500 has no static list, so with no DB roster its
columns are left null and a warning logged. What still RAISES is a BROKEN FETCH
- an HTTP error, or a non-empty roster that returns no usable quote at all -
because that is "breadth broke", not "breadth not seeded". Amar's call,
2026-09-19.
"""

import logging

from config import (
    BREADTH_BATCH_SIZE,
    BREADTH_INDICES,
    BREADTH_SAMPLED_INDEX,
    STATIC_NIFTY50,
)

logger = logging.getLogger()

# The nine columns this module owns on intraday_fno_data. measure() always
# returns exactly these keys, so write_snapshot's completeness check passes
# whether breadth ran or not.
BREADTH_COLUMNS = tuple(
    f"{prefix}_{field}"
    for prefix, _ in BREADTH_INDICES
    for field in ("advances", "declines", "unchanged", "adv_dec_ratio")
) + ("mkt_sampled",)

# Built-in fallback rosters by index_name, used only when the DB roster is
# empty. Each is a list of (exchange_segment, security_id) - the same shape
# read_constituents returns - so a fallback roster fetches and counts through
# exactly the same path as a DB one. Only the NIFTY 50 has a fallback.
STATIC_ROSTERS = {
    "NIFTY50": [("NSE_EQ", security_id) for security_id, _ in STATIC_NIFTY50],
}


def _direction(quote):
    """
    'adv' | 'dec' | 'unc' for one instrument, or None when it is unusable.

    None means no usable quote - a missing instrument, or a non-positive
    last_price or previous close (suspended or not yet traded). Such a stock is
    excluded from every count rather than being treated as unchanged, which
    would quietly inflate the unchanged tally and shrink the ratio's meaning.
    """
    if not isinstance(quote, dict):
        return None
    last = quote.get("last_price")
    prev_close = (quote.get("ohlc") or {}).get("close")
    try:
        last = float(last)
        prev_close = float(prev_close)
    except (TypeError, ValueError):
        return None
    if last <= 0 or prev_close <= 0:
        return None
    if last > prev_close:
        return "adv"
    if last < prev_close:
        return "dec"
    return "unc"


def _fetch_quotes(client, members):
    """
    One quote per (exchange_segment, security_id), batched to the API limit.

    `members` is the deduplicated union of every configured roster. The request
    groups ids by segment and is chunked so no single call exceeds
    BREADTH_BATCH_SIZE instruments. Returns {(segment, security_id_str): quote}.
    """
    ordered = list(members)
    quotes = {}
    for start in range(0, len(ordered), BREADTH_BATCH_SIZE):
        chunk = ordered[start : start + BREADTH_BATCH_SIZE]
        by_segment = {}
        for segment, security_id in chunk:
            by_segment.setdefault(segment, []).append(int(security_id))
        data = client.market_ohlc(by_segment)
        for segment, id_map in data.items():
            if not isinstance(id_map, dict):
                continue
            for security_id_str, quote in id_map.items():
                quotes[(segment, str(security_id_str))] = quote
    return quotes


def _count(members, quotes):
    """Advance/decline counts for one roster against the fetched quotes."""
    advances = declines = unchanged = 0
    for segment, security_id in members:
        direction = _direction(quotes.get((segment, str(security_id))))
        if direction == "adv":
            advances += 1
        elif direction == "dec":
            declines += 1
        elif direction == "unc":
            unchanged += 1
    counted = advances + declines + unchanged
    ratio = round(advances / declines, 4) if declines else None
    return {
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "adv_dec_ratio": ratio,
        "counted": counted,
        "roster": len(members),
    }


def measure(client, conn, read_constituents):
    """
    The nine breadth columns for this snapshot.

    `read_constituents(conn, index_names)` returns {index_name: [(segment,
    security_id), ...]}. It is passed in rather than imported so this module
    stays testable without a database.

    Each index reads its roster from the database; an index with a built-in
    fallback (the NIFTY 50) uses it when the DB roster is empty. An index left
    with no roster has its columns null and is skipped. Returns every key in
    BREADTH_COLUMNS. Raises only on a broken fetch - see the module docstring.
    """
    columns = {c: None for c in BREADTH_COLUMNS}
    index_names = [name for _, name in BREADTH_INDICES]
    rosters = read_constituents(conn, index_names)

    effective = {}
    for name in index_names:
        members = rosters.get(name) or []
        if not members and name in STATIC_ROSTERS:
            members = list(STATIC_ROSTERS[name])
            logger.warning(
                "breadth %s: no DB roster - using the built-in static list "
                "(%d members)", name, len(members),
            )
        effective[name] = members

    union = set()
    for members in effective.values():
        union.update(members)
    if not union:
        logger.warning("breadth: no roster for any index - breadth columns left null")
        return columns
    quotes = _fetch_quotes(client, union)

    for prefix, name in BREADTH_INDICES:
        members = effective[name]
        if not members:
            logger.warning(
                "breadth %s: no DB roster and no static fallback - %s_* left null",
                name, prefix,
            )
            continue
        result = _count(members, quotes)
        if result["counted"] == 0:
            raise RuntimeError(
                f"breadth for {name}: not one of {result['roster']} members "
                f"returned a usable quote - the fetch is broken, not the market"
            )
        if result["counted"] < result["roster"]:
            logger.warning(
                "breadth %s: %d of %d members had no usable quote",
                name, result["roster"] - result["counted"], result["roster"],
            )
        columns[f"{prefix}_advances"] = result["advances"]
        columns[f"{prefix}_declines"] = result["declines"]
        columns[f"{prefix}_unchanged"] = result["unchanged"]
        columns[f"{prefix}_adv_dec_ratio"] = result["adv_dec_ratio"]
        if name == BREADTH_SAMPLED_INDEX:
            columns["mkt_sampled"] = result["roster"]
        logger.info(
            "breadth %s: %d adv / %d dec / %d unch, ratio %s (%d of %d quoted)",
            name, result["advances"], result["declines"], result["unchanged"],
            result["adv_dec_ratio"], result["counted"], result["roster"],
        )
    return columns
