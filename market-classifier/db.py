"""
Neon access for market-classifier: write the judgement row.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo - the same rule every function here follows, and the reason the
connection string comes only from /algo/neon/connection. A second Neon project
carries an `algo` schema with the same table names and incompatible column
types, so resolving it from anywhere else is the easiest serious mistake here.

connect() lives in the neon-access layer. pg8000's DB-API uses `format`
paramstyle (%s placeholders). THE COLUMN LIST IS THE SOURCE OF TRUTH - one
ordered tuple drives the INSERT list, the placeholders and the DO UPDATE SET, so
the three cannot drift and silently write a value under the wrong column.
"""

import logging

logger = logging.getLogger()

SENTIMENTS_TABLE = "algo.intraday_sentiments"

SENTIMENTS_KEY = ("security_id", "instrument_type", "snapshot_ts")
SENTIMENTS_COLUMNS = SENTIMENTS_KEY + (
    "captured_at", "prev_snapshot_ts",
    "bias", "structure", "regime", "volatility", "score", "max_score",
    "confidence", "buildup",
    "swing_direction", "swing_high", "swing_low", "structure_determined",
    "range_used", "session_elapsed", "volatility_expanding",
    "created_at",
)


def _upsert_sql(table, columns, key):
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in key)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
        f"({', '.join(['%s'] * len(columns))}) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}"
    )


SENTIMENTS_UPSERT = _upsert_sql(SENTIMENTS_TABLE, SENTIMENTS_COLUMNS, SENTIMENTS_KEY)


def write_sentiment(conn, row):
    """
    Upsert one judgement row, keyed on (security_id, instrument_type, snapshot_ts).

    Upsert rather than insert so a re-run - a retried invoke, a replayed snapshot
    - rewrites the identical row instead of failing on the primary key. Raises on
    a missing column rather than writing a partial row.
    """
    missing = [c for c in SENTIMENTS_COLUMNS if c not in row]
    if missing:
        raise RuntimeError(
            f"sentiment row is missing {missing} - refusing to write a partial row"
        )
    cursor = conn.cursor()
    try:
        cursor.execute(SENTIMENTS_UPSERT, [row[c] for c in SENTIMENTS_COLUMNS])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
