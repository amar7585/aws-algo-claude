"""V1: the REAL pattern-recorder handler, run over every stored live run, must write
exactly what the replay derives.

    python pattern-recorder/replay/handler_check.py      # exits 1 on any failure

For each of the sessions with stored run clocks (intraday_fno_data rows), the
handler is invoked once per stored snapshot with the payload shape
pattern-detector forwards. A fake database answers its SELECTs from the pulled
JSON (replay/data) - applying the same epoch bounds the SQL does - and records
its upserts into an in-memory table with the real ON CONFLICT semantics
(first_seen_* and created_at kept). Then, per session:

  * the table after the day's last run == the replay's events for that day,
    field by field (everything but the write timestamps);
  * every run's `fresh` count == the replay's fresh events for that run;
  * every row's first_seen run is the run that first could read its trigger bar
    closed (the run whose freshness window holds it);
  * every INSERT carries exactly len(COLUMNS) params per row and the row count
    guard sees the right number.

pg8000 and neon_access are stubbed; no network, no AWS, no Neon.
"""
import contextlib
import datetime
import io
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
FUNC = os.path.dirname(HERE)
sys.path.insert(0, FUNC)
sys.path.insert(0, HERE)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
na = types.ModuleType("neon_access")
na.connect = lambda conn_string: FakeConn()                    # noqa: E731
na.read_neon_connection_string = lambda: "postgres://stub"     # noqa: E731
na.ist_datetime = lambda epoch: datetime.datetime.fromtimestamp(int(epoch), IST)  # noqa: E731
sys.modules["neon_access"] = na

with contextlib.redirect_stdout(io.StringIO()):
    import replay as R          # the replay's data, sessions and run clocks
import db                       # noqa: E402
import handler                  # noqa: E402
import patterns as pt           # noqa: E402

TABLE = {}          # PK tuple -> row dict
FAILS = []
STATS = {"selects": 0, "inserts": 0}


def fail(msg):
    FAILS.append(msg)
    if len(FAILS) <= 10:
        print("FAIL", msg)


class FakeCursor:
    def __init__(self):
        self.description, self._rows, self.rowcount = None, [], -1

    def _answer(self, rows, cols):
        self.description = [(c,) for c in cols]
        self._rows = [tuple(r.get(c) for c in cols) for r in rows]

    def execute(self, sql, params=()):
        params = list(params)
        if sql.count("%s") != len(params):
            fail(f"placeholder/param mismatch: {sql.count('%s')} vs {len(params)}")
        if sql.startswith("SELECT"):
            STATS["selects"] += 1
            cols = [c.strip() for c in re.match(r"SELECT (.*?) FROM", sql).group(1).split(",")]
            if "algo.intraday_fno_data" in sql:
                sid, itype, since, until = params
                rows = [r for r in R.fno_rows if str(r["security_id"]) == sid and r["instrument_type"] == itype
                        and since <= r["snapshot_ts"] <= until]
            elif "security_id IN" in sql:
                itype, *rest = params
                contracts, (since, bar, closed_by) = rest[:-3], rest[-3:]
                rows = [r for r in R.fut_rows if str(r["security_id"]) in contracts
                        and since <= r["candle_ts"] and r["candle_ts"] + bar <= closed_by]
                if itype != "FUTIDX":
                    fail(f"futures read with instrument_type {itype}")
            else:
                sid, itype, since, bar, closed_by = params
                rows = [{**r, "security_id": "13"} for r in R.idx_rows
                        if since <= r["candle_ts"] and r["candle_ts"] + bar <= closed_by]
                if (sid, itype) != ("13", "INDEX"):
                    fail(f"index read on {(sid, itype)}")
            self._answer(sorted(rows, key=lambda r: (str(r.get("security_id")), r.get("candle_ts", r.get("snapshot_ts")))), cols)
            return
        if sql.startswith("INSERT INTO algo.pattern_events"):
            STATS["inserts"] += 1
            n = len(db.COLUMNS)
            if len(params) % n:
                fail(f"INSERT params {len(params)} not a multiple of {n}")
            batch = [dict(zip(db.COLUMNS, params[i:i + n])) for i in range(0, len(params), n)]
            for row in batch:
                key = tuple(row[c] for c in db.KEY)
                if key in TABLE:
                    for c in db.KEEP_ON_CONFLICT:
                        row[c] = TABLE[key][c]
                TABLE[key] = row
            self.rowcount = len(batch)
            return
        fail(f"unexpected SQL: {sql[:60]}")

    def fetchall(self):
        return self._rows


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


DETECTION_FIELDS = [c for c in db.COLUMNS if c not in ("first_seen_snapshot_ts", "first_seen_at", "created_at", "updated_at")]
stored = [s for s in R.sessions if s["fno"]]
runs = 0
for s in stored:
    clocks, _ = R.run_clocks(s)
    fresh_expected = {}
    for snap_ts, cap, after in clocks:
        row = R.fno_by_ts[snap_ts]
        payload = {"fno": {"security_id": "13", "instrument_type": "INDEX", "snapshot_ts": snap_ts,
                           "captured_at": cap, "fut_security_id": row["fut_security_id"],
                           "prev_snapshot_ts": row.get("prev_snapshot_ts")},
                   "sentiment": {}, "instrument": {}, "daily": None}
        with contextlib.redirect_stderr(io.StringIO()):
            out = handler.lambda_handler(payload, None)
        runs += 1
        expect_fresh = pt.fresh_events(s["events"], after, cap)
        if out["fresh"] != len(expect_fresh):
            fail(f"{s['date']} {R.st.clock(snap_ts)}: fresh {out['fresh']} vs replay {len(expect_fresh)}")
        for e in expect_fresh:
            fresh_expected[(e["trigger_ts"], e["family"], e["subtype"] or "none", e["direction"])] = cap
    day_rows = {k: v for k, v in TABLE.items() if R.st.ist_day(k[3]) == s["day"]}
    want = {}
    for e in s["events"]:
        r = db.event_row(e, "13", "INDEX", s["contract"], pt.RULES_VERSION, 0, 0, 0)
        want[tuple(r[c] for c in db.KEY)] = r
    if set(day_rows) != set(want):
        fail(f"{s['date']}: table has {len(day_rows)} rows, replay {len(want)}; "
             f"only-table {len(set(day_rows) - set(want))}, only-replay {len(set(want) - set(day_rows))}")
    for k in set(day_rows) & set(want):
        diff = [c for c in DETECTION_FIELDS if day_rows[k][c] != want[k][c]]
        if diff:
            fail(f"{s['date']} {k[3:]}: fields differ {diff}")
        first_cap = fresh_expected.get((k[3], k[4], k[5], k[6]))
        if first_cap is not None and day_rows[k]["first_seen_at"] != first_cap:
            fail(f"{s['date']} {k[3:]}: first_seen_at {day_rows[k]['first_seen_at']} vs announcing run {first_cap}")

print(f"V1: {runs} handler runs over {len(stored)} sessions, {len(TABLE)} rows in the fake table, "
      f"{STATS['selects']} SELECTs, {STATS['inserts']} INSERTs")
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
