"""V3: what the LIVE recorder wrote == an independent re-derivation of the day. Read-only.

    NEON_CONNECTION_STRING="$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /algo/neon/connection \
        --with-decryption --query Parameter.Value --output text --region ap-south-1)" \
        python -I pattern-recorder/replay/table_check.py [YYYY-MM-DD]

For the day (default today), takes the newest stored 15-min snapshot and re-derives
the session exactly as the recorder would at that run: the same db reads, inputs.py
and patterns.py, on a READ ONLY session. Then compares with algo.pattern_events for
the day:

  * the same set of events (rules_version, trigger bar, family, subtype, direction);
  * every detection field equal;
  * first_seen_at of each row is the captured_at of a stored run that could read its
    trigger bar closed - and the FIRST such run once the hand-off was on.

Run during the session it checks the morning so far; after the close, the whole day.
"""
import datetime
import os
import ssl
import sys
import types
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FUNC = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "driver", "python"))
import pg8000.dbapi  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
na = types.ModuleType("neon_access")
na.ist_datetime = lambda e: datetime.datetime.fromtimestamp(int(e), IST)  # noqa: E731
sys.modules["neon_access"] = na
sys.path.insert(0, FUNC)
import db              # noqa: E402
import inputs          # noqa: E402
import patterns as pt  # noqa: E402
import structure as st  # noqa: E402
from config import HISTORY_DAYS  # noqa: E402

CONN_STR = os.environ.get("NEON_CONNECTION_STRING") or sys.exit("NEON_CONNECTION_STRING is not set")
p = urllib.parse.urlparse(CONN_STR)
conn = pg8000.dbapi.connect(user=urllib.parse.unquote(p.username or ""), password=urllib.parse.unquote(p.password or ""),
                            host=p.hostname, port=p.port or 5432, database=(p.path or "/").lstrip("/"),
                            ssl_context=ssl.create_default_context())
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
cur.execute("SELECT current_database()")
assert cur.fetchone()[0] == "Algo"

day = (datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.datetime.now(IST).date())
day_start = int(datetime.datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())
cur.execute("SELECT snapshot_ts, captured_at, fut_security_id FROM algo.intraday_fno_data "
            "WHERE security_id = '13' AND instrument_type = 'INDEX' AND snapshot_ts >= %s AND snapshot_ts < %s "
            "ORDER BY snapshot_ts", (day_start, day_start + 86400))
runs = cur.fetchall()
if not runs:
    sys.exit(f"no stored snapshots on {day}")
snap, cap, contract = int(runs[-1][0]), int(runs[-1][1]), str(runs[-1][2])

since = st.ist_day_start(snap) - HISTORY_DAYS * st.DAY_SECONDS
fno_rows = db.read_fno_rows(conn, "13", "INDEX", since, snap)
contracts = {str(r["fut_security_id"]) for r in fno_rows if r.get("fut_security_id")} | {contract}
store = inputs.Store(db.read_future_bars(conn, sorted(contracts), since, cap),
                     db.read_index_bars(conn, "13", "INDEX", since, cap), fno_rows)
session = store.session_inputs(st.ist_day(snap), contract=contract, closed_by=cap, snapshot_ts=snap)
want = {}
for e in inputs.detect(session, pt.detect_session):
    r = db.event_row(e, "13", "INDEX", contract, pt.RULES_VERSION, 0, 0, 0)
    want[tuple(r[k] for k in db.KEY)] = r

cur.execute(f"SELECT {', '.join(db.COLUMNS)} FROM algo.pattern_events WHERE security_id = '13' "
            f"AND instrument_type = 'INDEX' AND rules_version = %s AND trigger_ts >= %s AND trigger_ts < %s",
            (pt.RULES_VERSION, day_start, day_start + 86400))
have = {}
for vals in cur.fetchall():
    r = dict(zip(db.COLUMNS, vals))
    have[tuple(r[k] for k in db.KEY)] = r
conn.close()


def norm(col, v):
    if v is None:
        return None
    if col in db.ARRAY_COLUMNS:
        return list(v) if isinstance(v, list) else [x for x in v.strip("{}").split(",") if x]
    if isinstance(v, (int, float)) or hasattr(v, "as_tuple"):
        return float(v)
    return v


fields = [c for c in db.COLUMNS if c not in ("first_seen_snapshot_ts", "first_seen_at", "created_at", "updated_at")]
fails = []
if set(have) != set(want):
    fails.append(f"event sets differ: only-table {sorted(k[3:] for k in set(have) - set(want))}, "
                 f"only-rederived {sorted(k[3:] for k in set(want) - set(have))}")
for k in sorted(set(have) & set(want)):
    diff = [c for c in fields if norm(c, have[k][c]) != norm(c, want[k][c])]
    if diff:
        fails.append(f"{st.clock(k[3])} {k[4]}/{k[5]} {k[6]}: {[(c, have[k][c], want[k][c]) for c in diff]}")
caps = sorted(int(r[1]) for r in runs)
for k, r in have.items():
    closes = k[3] + st.BAR_SECONDS
    fs = int(r["first_seen_at"])
    if fs not in caps or fs < closes:
        fails.append(f"{st.clock(k[3])} {k[4]}: first_seen_at {fs} is not a stored run that could read the bar")

print(f"{day} up to the {st.clock(snap)} run (captured {datetime.datetime.fromtimestamp(cap, IST):%H:%M:%S}), "
      f"future {contract}: table {len(have)} rows, re-derived {len(want)}")
for k in sorted(have):
    r = have[k]
    print(f"  {st.clock(k[3])} {k[4]}{'/' + k[5] if k[5] != 'none' else ''} {k[6]} at {r['level']} "
          f"{float(r['level_price'])} vol z {r['vol_z']} first seen {datetime.datetime.fromtimestamp(int(r['first_seen_at']), IST):%H:%M:%S}")
print("ALL MATCH" if not fails else "MISMATCH:\n  " + "\n  ".join(fails))
sys.exit(1 if fails else 0)
