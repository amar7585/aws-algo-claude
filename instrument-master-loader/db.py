"""
Writing algo.instrument_master.

Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
Algo, schema algo. connect() comes from the neon-access layer.
"""

import logging
import time

from config import UPSERT_BATCH_SIZE

logger = logging.getLogger()

UPSERT_SQL = """
    INSERT INTO algo.instrument_master
        (security_id, trading_symbol, exchange_segment, instrument_type,
         lot_units, updated_at)
    VALUES {values}
    ON CONFLICT (security_id, instrument_type) DO UPDATE SET
        trading_symbol   = EXCLUDED.trading_symbol,
        exchange_segment = EXCLUDED.exchange_segment,
        lot_units        = EXCLUDED.lot_units,
        updated_at       = EXCLUDED.updated_at
"""


def upsert_instrument_master(conn, rows, batch_size=UPSERT_BATCH_SIZE):
    """Insert/update rows into algo.instrument_master, keyed on
    (security_id, instrument_type). updated_at is epoch seconds.

    The whole load commits once: a partial instrument master is worse than a
    stale one, since downstream lookups would silently miss contracts.
    """
    if not rows:
        return 0

    now_epoch = int(time.time())
    written = 0

    cur = conn.cursor()
    try:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            values = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(batch))
            params = []
            for row in batch:
                params.extend(row)
                params.append(now_epoch)
            cur.execute(UPSERT_SQL.format(values=values), params)
            written += len(batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()

    return written

