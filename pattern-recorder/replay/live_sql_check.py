"""V1b: the REAL handler and REAL SQL against the LIVE table - and nothing is kept.

    NEON_CONNECTION_STRING="$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /algo/neon/connection \
        --with-decryption --query Parameter.Value --output text --region ap-south-1)" \
        python -I pattern-recorder/replay/live_sql_check.py [YYYY-MM-DD HH:MM]

Every connection the handler opens is wrapped: commit() does nothing, and close()
counts what the transaction can see and then ROLLS BACK. So pg8000, the generated
INSERT ... ON CONFLICT, the text[] casts and the row-count guard all run for real
against algo.pattern_events, and the table is left exactly as it was.

  1. The handler on one stored past run (default 2026-11-04 15:25): its live reads
     and the rows it would write must equal what the replay derives from the pulled
     data for that day; its in-transaction count must equal rows_upserted.
  2. ON CONFLICT on one connection: a second upsert of the same keys must keep
     first_seen_* and created_at, update everything else, and round-trip text[] and
     numerics.
  3. A fresh connection must find the table as it was before (normally empty).

Needs the unpacked driver in replay/driver (see README.md).
"""
import contextlib
import datetime
import decimal
import io
import json
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
CONN_STR = os.environ.get("NEON_CONNECTION_STRING") or sys.exit("NEON_CONNECTION_STRING is not set")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def raw_connect():
    p = urllib.parse.urlparse(CONN_STR)
    return pg8000.dbapi.connect(
        user=urllib.parse.unquote(p.username or ""), password=urllib.parse.unquote(p.password or ""),
        host=p.hostname, port=p.port or 5432, database=(p.path or "/").lstrip("/"),
        ssl_context=ssl.create_default_context())


class RollbackConn:
    """A real connection whose commit is suppressed and whose close rolls back."""
    seen = []

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        pass

    def rollback(self):
        self._conn.rollback()

    def close(self):
        cur = self._conn.cursor()
        cur.execute("SELECT count(*) FROM algo.pattern_events")
        RollbackConn.seen.append(cur.fetchone()[0])
        self._conn.rollback()
        self._conn.close()


na = types.ModuleType("neon_access")
na.connect = lambda s: RollbackConn(raw_connect())  # noqa: E731
na.read_neon_connection_string = lambda: CONN_STR  # noqa: E731
na.ist_datetime = lambda e: datetime.datetime.fromtimestamp(int(e), IST)  # noqa: E731
sys.modules["neon_access"] = na
sys.path.insert(0, FUNC)
import db        # noqa: E402
import handler   # noqa: E402
import inputs    # noqa: E402
import patterns as pt  # noqa: E402

# ---- guard: right target, and what the table holds before ---------------------------
c = raw_connect()
cur = c.cursor()
cur.execute("SELECT current_database(), (SELECT count(*) FROM algo.pattern_events)")
dbname, before = cur.fetchone()
c.close()
check("target is database Algo", dbname == "Algo", dbname)
print(f"     algo.pattern_events holds {before} rows before the check")

# ---- 1. the real handler on one stored run, rolled back ---------------------------------
when = sys.argv[1] if len(sys.argv) > 1 else "2026-11-04 15:25"
snap = int(datetime.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())
c = raw_connect()
cur = c.cursor()
cur.execute("SELECT security_id, instrument_type, snapshot_ts, captured_at, fut_security_id, prev_snapshot_ts "
            "FROM algo.intraday_fno_data WHERE security_id = '13' AND instrument_type = 'INDEX' AND snapshot_ts = %s",
            (snap,))
row = cur.fetchone()
c.close()
if row is None:
    sys.exit(f"no stored snapshot at {when}")
payload = {"fno": dict(zip(("security_id", "instrument_type", "snapshot_ts", "captured_at",
                            "fut_security_id", "prev_snapshot_ts"), row)), "sentiment": {}, "instrument": {}}

captured = {}
real_upsert = db.upsert_events


def spy(conn, rows):
    captured["rows"] = rows
    return real_upsert(conn, rows)


db.upsert_events = spy
with contextlib.redirect_stderr(io.StringIO()):
    out = handler.lambda_handler(payload, None)
db.upsert_events = real_upsert
rows = captured["rows"]
check(f"handler ran on {when}: {out['rows_upserted']} rows upserted, {out['events_in_session']} events",
      out["rows_upserted"] == out["events_in_session"] == len(rows), out)
check("its own uncommitted transaction saw exactly those rows (row-count guard + real INSERT)",
      RollbackConn.seen[-1] == before + len(rows), (RollbackConn.seen, before, len(rows)))

load = lambda n: json.load(open(os.path.join(HERE, "data", n + ".json"), encoding="utf-8"))  # noqa: E731
store = inputs.Store(load("fut_5m"), load("idx_5m"), load("fno"))
session = store.session_inputs(inputs.st.ist_day(snap), contract=str(row[4]), closed_by=int(row[3]), snapshot_ts=snap)
want = {tuple(r[k] for k in db.KEY): r for r in
        (db.event_row(e, "13", "INDEX", str(row[4]), pt.RULES_VERSION, 0, 0, 0)
         for e in inputs.detect(session, pt.detect_session))}
got = {tuple(r[k] for k in db.KEY): r for r in rows}
fields = [k for k in db.COLUMNS if k not in ("first_seen_snapshot_ts", "first_seen_at", "created_at", "updated_at")]
diff = [k for k in set(got) & set(want) if any(got[k][f] != want[k][f] for f in fields)]
check("live reads -> same rows as the replay derives from the pulled data",
      set(got) == set(want) and not diff,
      f"only-live {len(set(got) - set(want))}, only-replay {len(set(want) - set(got))}, differing {len(diff)}")

# ---- 2. ON CONFLICT on one connection, rolled back ---------------------------------------
conn = RollbackConn(raw_connect())
first = [{**r, "first_seen_snapshot_ts": 111, "first_seen_at": 111, "created_at": 111, "updated_at": 111} for r in rows]
second = [{**r, "first_seen_snapshot_ts": 222, "first_seen_at": 222, "created_at": 222, "updated_at": 333,
           "vol_z": (r["vol_z"] or 0) + 1} for r in rows]
n1 = db.upsert_events(conn, first)
n2 = db.upsert_events(conn, second)
check("upsert twice on the same keys: row count guard holds both times", n1 == n2 == len(rows), (n1, n2))
pick = next((r for r in second if r["other_levels"] != "{}" and r["level_strike"] is not None), second[0])
cur = conn.cursor()
cur.execute("SELECT count(*), max(first_seen_snapshot_ts), max(first_seen_at), max(created_at), min(updated_at) "
            "FROM algo.pattern_events WHERE first_seen_snapshot_ts IN (111, 222)")
cnt, fss, fsa, cra, upd = cur.fetchone()
check("ON CONFLICT keeps first_seen_* and created_at, updates updated_at",
      (cnt, fss, fsa, cra, upd) == (len(rows), 111, 111, 111, 333), (cnt, fss, fsa, cra, upd))
cur.execute("SELECT vol_z, other_levels, candles, level_strike, level_price FROM algo.pattern_events "
            "WHERE security_id = %s AND instrument_type = %s AND rules_version = %s AND trigger_ts = %s "
            "AND family = %s AND subtype = %s AND direction = %s", [pick[k] for k in db.KEY])
vz, ol, cd, ls, lp = cur.fetchone()
as_list = lambda lit: [x for x in lit.strip("{}").split(",") if x]  # noqa: E731
check("ON CONFLICT updates detection columns (vol_z)", decimal.Decimal(str(pick["vol_z"])) == vz, (pick["vol_z"], vz))
check("text[] round-trips", ol == as_list(pick["other_levels"]) and cd == as_list(pick["candles"]),
      (ol, pick["other_levels"], cd, pick["candles"]))
check("numerics round-trip", (ls is None and pick["level_strike"] is None or float(ls) == pick["level_strike"])
      and float(lp) == pick["level_price"], (ls, lp))
conn.close()

# ---- 3. nothing kept ----------------------------------------------------------------------
c = raw_connect()
cur = c.cursor()
cur.execute("SELECT count(*) FROM algo.pattern_events")
after = cur.fetchone()[0]
c.close()
check(f"table unchanged afterwards ({after} rows)", after == before, (before, after))
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
