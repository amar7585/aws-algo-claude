"""
Instrument Master Loader - AWS Lambda function

Downloads Dhan's public scrip master CSV, filters it to the categories in
INSTRUMENT_RULES (all 9 rules, ported from
trading-algo/brokers/implementations/dhan/dhan_broker.py - deliberately not
narrowed to Nifty-only), and upserts the result into algo.instrument_master on
Neon (project: AI Trader APP, database: Algo).

Trigger: EventBridge Scheduler, monthly cron. This loader sits OUTSIDE the
History -> Regime -> Strategy state machine: it is reference data that changes
when the exchange lists or expires contracts, on a different clock from the
trading session.

Modules:
    config.py         this function's tunables
    rules.py          ExchangeSegment, the 9 instrument rules, row predicates
    scrip_master.py   download and parse the CSV
    db.py             the upsert

The Neon connection and the shared connection-string read come from the
neon-access layer.

Conventions:
  - exchange_segment is stored as the raw Dhan segment code
    (ExchangeSegment.<X>.value), never a human-readable string.
  - Every time value written to the DB is epoch seconds.
"""

import json
import logging
import time

from neon_access import connect, read_neon_connection_string

from db import upsert_instrument_master
from scrip_master import download_scrip_master, fetch_and_filter_instruments

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """EventBridge Scheduler entry point. Raises on failure so the invocation
    is recorded as an error and the schedule's retry policy applies."""
    neon_conn_string = read_neon_connection_string()

    started = time.time()
    csv_text = download_scrip_master()
    rows = fetch_and_filter_instruments(csv_text)

    conn = None
    try:
        conn = connect(neon_conn_string)
        row_count = upsert_instrument_master(conn, rows)
    finally:
        if conn is not None:
            conn.close()

    result = {
        "status": "success",
        "rows_upserted": row_count,
        "elapsed_seconds": round(time.time() - started, 2),
    }
    logger.info("instrument_master upsert complete: %s", json.dumps(result))
    return result
