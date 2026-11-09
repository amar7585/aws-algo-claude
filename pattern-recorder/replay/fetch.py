"""Read-only pull of the tables the v2 replay needs, from Neon AI Trader APP / Algo.

    python -I pattern-recorder/replay/fetch.py [driver_dir] [out_dir]

driver_dir defaults to replay/driver (the repo's own neon-db-driver layer zip,
unpacked - see README.md), out_dir to replay/data. Both are git-ignored.

The connection string comes from the NEON_CONNECTION_STRING environment variable and
is never printed. The session is set READ ONLY before any query, and the target is
verified (database Algo, instrument_master.security_id is text) before reading - the
other Neon project has the same table names with incompatible columns (CLAUDE.md).
"""
import decimal
import json
import os
import pathlib
import ssl
import sys
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
driver_dir = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "driver")
out_dir = sys.argv[2] if len(sys.argv) > 2 else str(HERE / "data")
os.makedirs(out_dir, exist_ok=True)
sys.path.insert(0, os.path.join(driver_dir, "python"))
import pg8000.dbapi  # noqa: E402

conn_string = os.environ.get("NEON_CONNECTION_STRING")
if not conn_string:
    raise SystemExit("NEON_CONNECTION_STRING is not set (aws ssm read failed?)")
p = urllib.parse.urlparse(conn_string)
conn = pg8000.dbapi.connect(
    user=urllib.parse.unquote(p.username or ""), password=urllib.parse.unquote(p.password or ""),
    host=p.hostname, port=p.port or 5432, database=(p.path or "/").lstrip("/"),
    ssl_context=ssl.create_default_context(),
)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
cur.execute("SHOW transaction_read_only")
assert cur.fetchone()[0] == "on", "session is not read-only"
cur.execute("SELECT current_database()")
db = cur.fetchone()[0]
cur.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='algo' "
            "AND table_name='instrument_master' AND column_name='security_id'")
sid_type = cur.fetchone()[0]
assert db == "Algo" and sid_type == "text", f"wrong target: db={db} security_id={sid_type}"
print(f"target OK: database {db}, instrument_master.security_id {sid_type}, read-only on")


def dump(name, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = []
    for r in cur.fetchall():
        rows.append({c: (float(v) if isinstance(v, decimal.Decimal) else v) for c, v in zip(cols, r)})
    with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(rows, f)
    print(f"{name}: {len(rows)} rows")


dump("fut_5m", "SELECT security_id, candle_ts, open, high, low, close, volume FROM algo.candle_5min "
               "WHERE instrument_type = 'FUTIDX' ORDER BY security_id, candle_ts")
dump("idx_5m", "SELECT candle_ts, open, high, low, close, volume FROM algo.candle_5min "
               "WHERE security_id = '13' AND instrument_type = 'INDEX' ORDER BY candle_ts")
dump("fno", "SELECT * FROM algo.intraday_fno_data WHERE security_id = '13' ORDER BY snapshot_ts")
dump("sent", "SELECT * FROM algo.intraday_sentiments WHERE security_id = '13' ORDER BY snapshot_ts")
dump("fno5", "SELECT * FROM algo.fno_5m_snapshot WHERE security_id = '13' ORDER BY snapshot_ts")
dump("obs", "SELECT * FROM algo.session_observations ORDER BY event_ts")
dump("daily", "SELECT * FROM algo.daily_market_sentiment WHERE security_id = '13' ORDER BY trade_date")
conn.close()
