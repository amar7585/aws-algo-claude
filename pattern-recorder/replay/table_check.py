"""V3 (v2.1): what the LIVE recorder wrote == an independent re-derivation of the day. Read-only.

    NEON_CONNECTION_STRING="$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /algo/neon/connection \
        --with-decryption --query Parameter.Value --output text --region ap-south-1)" \
        python -I pattern-recorder/replay/table_check.py [YYYY-MM-DD]

For the day (default today), takes the newest stored 5-min tick (algo.fno_5m_snapshot,
the clock v2.1 runs on) and re-derives the session through the same db reads,
inputs.py and patterns.py, on a READ ONLY session, from candle_5min. Then compares
with algo.pattern_events rows of this RULES_VERSION for the day:

  * the same set of events (trigger bar, family, subtype, direction);
  * every detection field equal;
  * first_seen_at of each row is the captured_at of the FIRST tick that could read
    its trigger bar closed;
  * the sent marks: shown, and telegram_sent_at never set before first_seen_at.

THE HORIZON. The live recorder reads TODAY's candles from Dhan; this check reads
candle_5min, which intraday-data-loader refreshes only every 15 minutes (and whose
newest stored bar is still forming). So only events whose trigger bar closed by
the loader's last complete fetch are compared: bars closed by the newest 15-min
row's captured_at minus 30 s (the loader runs ~15 s after the quarter, the row
lands ~42-127 s after). After the close that covers the whole day.
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

LOADER_MARGIN_SECONDS = 30

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
cur.execute("SELECT snapshot_ts, captured_at, fut_security_id FROM algo.fno_5m_snapshot "
            "WHERE security_id = '13' AND instrument_type = 'INDEX' AND snapshot_ts >= %s AND snapshot_ts < %s "
            "ORDER BY snapshot_ts", (day_start, day_start + 86400))
ticks = cur.fetchall()
if not ticks:
    sys.exit(f"no stored 5-min ticks on {day}")
snap, cap, contract = int(ticks[-1][0]), int(ticks[-1][1]), str(ticks[-1][2])
cur.execute("SELECT max(captured_at) FROM algo.intraday_fno_data WHERE security_id = '13' AND instrument_type = 'INDEX' "
            "AND snapshot_ts >= %s AND captured_at <= %s", (day_start, cap))
last15 = cur.fetchone()[0]
horizon = min(cap, int(last15) - LOADER_MARGIN_SECONDS) if last15 else day_start

since = st.ist_day_start(snap) - HISTORY_DAYS * st.DAY_SECONDS
fno_rows = db.read_fno_rows(conn, "13", "INDEX", since, cap)
contracts = {str(r["fut_security_id"]) for r in fno_rows if r.get("fut_security_id")} | {contract}
store = inputs.Store(db.read_future_bars(conn, sorted(contracts), since, horizon),
                     db.read_index_bars(conn, "13", "INDEX", since, horizon), fno_rows)
session = store.session_inputs(st.ist_day(snap), contract=contract, closed_by=horizon, snapshot_ts=snap)
want = {}
for e in inputs.detect(session, pt.detect_session):
    r = db.event_row(e, "13", "INDEX", contract, pt.RULES_VERSION, 0, 0, 0)
    want[tuple(r[k] for k in db.KEY)] = r

cur.execute(f"SELECT {', '.join(db.COLUMNS + db.SENT_MARKS)} FROM algo.pattern_events WHERE security_id = '13' "
            f"AND instrument_type = 'INDEX' AND rules_version = %s AND trigger_ts >= %s AND trigger_ts < %s",
            (pt.RULES_VERSION, day_start, day_start + 86400))
have_all = {}
for vals in cur.fetchall():
    r = dict(zip(db.COLUMNS + db.SENT_MARKS, vals))
    have_all[tuple(r[k] for k in db.KEY)] = r
conn.close()
have = {k: r for k, r in have_all.items() if k[3] + st.BAR_SECONDS <= horizon}


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
caps = sorted(int(t[1]) for t in ticks)
for k, r in have_all.items():
    first = next((c for c in caps if c >= k[3] + st.BAR_SECONDS), None)
    if int(r["first_seen_at"]) != first:
        fails.append(f"{st.clock(k[3])} {k[4]}: first_seen_at {r['first_seen_at']} is not the first tick "
                     f"that could read the bar ({first})")
    if r["telegram_sent_at"] is not None and int(r["telegram_sent_at"]) < int(r["first_seen_at"]):
        fails.append(f"{st.clock(k[3])} {k[4]}: sent before it was first seen")

stamp = datetime.datetime.fromtimestamp(cap, IST).strftime("%H:%M:%S")
print(f"{day} {pt.RULES_VERSION} up to the {st.clock(snap)} tick (captured {stamp}), future {contract}: "
      f"table {len(have_all)} rows ({len(have)} within the loader horizon {st.clock(horizon - st.BAR_SECONDS)} bar), "
      f"re-derived {len(want)}")
for k in sorted(have_all):
    r = have_all[k]
    sent = []
    if r["telegram_sent_at"] is not None:
        sent.append(f"telegram {datetime.datetime.fromtimestamp(int(r['telegram_sent_at']), IST):%H:%M:%S}")
    if r["manager_sent_at"] is not None:
        sent.append(f"manager {datetime.datetime.fromtimestamp(int(r['manager_sent_at']), IST):%H:%M:%S}")
    print(f"  {st.clock(k[3])} {k[4]}{'/' + k[5] if k[5] != 'none' else ''} {k[6]} at {r['level']} "
          f"{float(r['level_price'])} vol z {r['vol_z']} first seen "
          f"{datetime.datetime.fromtimestamp(int(r['first_seen_at']), IST):%H:%M:%S}"
          + (f"  SENT {', '.join(sent)}" if sent else ""))
print("ALL MATCH" if not fails else "MISMATCH:\n  " + "\n  ".join(fails))
sys.exit(1 if fails else 0)
