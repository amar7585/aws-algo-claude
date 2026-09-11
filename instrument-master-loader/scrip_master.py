"""
Fetching and parsing Dhan's public scrip master CSV.

Parsed with the stdlib csv module rather than pandas, and fetched over plain
HTTP rather than through the dhanhq SDK - both of which would drag
pandas/numpy into the deployment package.
"""

import csv
import io
import logging
import urllib.request

from config import DOWNLOAD_TIMEOUT_SECONDS, SCRIP_MASTER_URL
from rules import NORMALISED_COLUMNS, keep_row, match_rule, parse_lot_units

logger = logging.getLogger()

def download_scrip_master(url=SCRIP_MASTER_URL):
    """Fetch Dhan's public scrip master CSV. It needs no credentials, so this
    is a plain GET rather than a dhanhq.fetch_security_list() call."""
    logger.info("Downloading scrip master from %s", url)
    # An explicit User-Agent: the CDN serves the default python-urllib one
    # today, but browsers' UA is the better-trodden path through it.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
        if resp.status != 200:
            raise RuntimeError("scrip master GET returned HTTP %s" % resp.status)
        payload = resp.read()
    logger.info("Downloaded %d bytes", len(payload))
    return payload.decode("utf-8-sig", errors="replace")


def fetch_and_filter_instruments(csv_text):
    """Apply the same filter/rename pipeline as the original
    load_instrument_master(), yielding tuples of
    (security_id, trading_symbol, exchange_segment, instrument_type, lot_units).
    """
    reader = csv.DictReader(io.StringIO(csv_text))

    missing = [c for c in NORMALISED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise RuntimeError("scrip master is missing expected columns: %s" % missing)

    rows = []
    seen = set()
    for raw in reader:
        # Cheap two-field exchange gate first: it drops ~37% of the file, and
        # normalising before it would pay the copy on every row instead of on
        # the survivors.
        if not keep_row(
            {
                "SEM_EXM_EXCH_ID": (raw.get("SEM_EXM_EXCH_ID") or "").strip().upper(),
                "SEM_TRADING_SYMBOL": (raw.get("SEM_TRADING_SYMBOL") or "")
                .strip()
                .upper(),
            }
        ):
            continue

        row = dict(raw)
        for col in NORMALISED_COLUMNS:
            value = row.get(col)
            row[col] = "" if value is None else str(value).strip().upper()

        exchange_segment = match_rule(row)
        if exchange_segment is None:
            continue

        security_id = str(raw.get("SEM_SMST_SECURITY_ID", "") or "").strip()
        if not security_id:
            continue

        instrument_type = row["SEM_INSTRUMENT_NAME"]

        # (security_id, instrument_type) is the primary key; a batch carrying
        # the same key twice makes Postgres abort the whole statement.
        key = (security_id, instrument_type)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            (
                security_id,
                row["SEM_TRADING_SYMBOL"],
                exchange_segment,
                instrument_type,
                parse_lot_units(row["SEM_LOT_UNITS"]),
            )
        )

    logger.info("Filtered scrip master down to %d instruments", len(rows))
    return rows
