"""V1b (v2.1): the REAL handler and REAL SQL against the LIVE tables - and nothing is kept.

    NEON_CONNECTION_STRING="$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /algo/neon/connection \
        --with-decryption --query Parameter.Value --output text --region ap-south-1)" \
        python -I pattern-recorder/replay/live_sql_check.py [YYYY-MM-DD HH:MM]

The handler runs inside ONE transaction that is rolled back at the end. Its commit()
releases and re-takes a savepoint and its rollback() rolls back to it, so the upsert,
the claim (SELECT ... FOR UPDATE SKIP LOCKED), the mark and the row-count guards all
run for real against algo.pattern_events, the transaction semantics stay intact, and
the table is left exactly as it was. Dhan is stubbed with the day's candle_5min bars;
Telegram and strategy-manager are recorded, never called.

  1. The handler on one stored tick (default: the day's tick with the most alerts):
     - its rows equal the library's re-derivation from the same live reads;
     - its alerts equal the rule applied to the stored day_trend;
     - each alert is sent once and marked, in-transaction.
  2. The same tick again, on the same transaction: nothing is resent.
  3. ON CONFLICT: a second upsert of the same keys keeps first_seen_* and created_at,
     updates the rest, never touches the sent marks, and round-trips text[] and numerics.
  4. A fresh connection finds the table as it was before.

Needs the unpacked driver in replay/driver (see README.md).
"""
import contextlib
import datetime
import decimal
import io
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


class SavepointConn:
    """One real transaction; commit/rollback map to a savepoint; finish() rolls it all back."""

    def __init__(self, conn):
        self._conn = conn
        self._x("SAVEPOINT v1b")

    def _x(self, sql):
        self._conn.cursor().execute(sql)

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._x("RELEASE SAVEPOINT v1b")
        self._x("SAVEPOINT v1b")

    def rollback(self):
        self._x("ROLLBACK TO SAVEPOINT v1b")

    def close(self):
        pass  # kept for the next run; finish() ends it

    def finish(self):
        self._conn.rollback()
        self._conn.close()


SHARED = {"conn": None}
na = types.ModuleType("neon_access")
na.connect = lambda s: SHARED["conn"]  # noqa: E731
na.read_neon_connection_string = lambda: CONN_STR  # noqa: E731
na.ist_datetime = lambda e: datetime.datetime.fromtimestamp(int(e), IST)  # noqa: E731
na.SSL_CONTEXT = ssl.create_default_context()
na.get_parameter = lambda name: "{}"  # noqa: E731
na.now_epoch = lambda: 0  # noqa: E731
sys.modules["neon_access"] = na
b3 = types.ModuleType("boto3")  # Telegram and the manager are recorded, never called
b3.client = lambda *a, **k: None  # noqa: E731
sys.modules["boto3"] = b3
sys.path.insert(0, FUNC)
import alerts    # noqa: E402
import db        # noqa: E402
import handler   # noqa: E402
import inputs    # noqa: E402
import patterns as pt  # noqa: E402
import structure as st  # noqa: E402
from config import HISTORY_DAYS  # noqa: E402

# ---- guard: right target, and what the table holds before ---------------------------
c = raw_connect()
cur = c.cursor()
# Counted on THIS rules_version only: the live recorder may write rows of another
# version while the check runs, and that must not read as a leak.
GUARD = ("SELECT count(*), count(*) FILTER (WHERE telegram_sent_at IS NOT NULL OR manager_sent_at IS NOT NULL) "
         "FROM algo.pattern_events WHERE rules_version = %s")
cur.execute("SELECT current_database()")
dbname = cur.fetchone()[0]
check("target is database Algo", dbname == "Algo", dbname)
cur.execute(GUARD, (pt.RULES_VERSION,))
before, marked_before = cur.fetchone()
print(f"     algo.pattern_events holds {before} {pt.RULES_VERSION} rows ({marked_before} with a sent mark) before the check")

# ---- the day's ticks, bars and expected alerts, from live reads ---------------------------
when = sys.argv[1] if len(sys.argv) > 1 else None
day = (datetime.datetime.strptime(when, "%Y-%m-%d %H:%M").date() if when else datetime.datetime.now(IST).date())
day_start = int(datetime.datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())
cur.execute("SELECT snapshot_ts, captured_at, prev_snapshot_ts, fut_security_id, fut_symbol, fut_bar_ts "
            "FROM algo.fno_5m_snapshot WHERE security_id = '13' AND instrument_type = 'INDEX' "
            "AND snapshot_ts >= %s AND snapshot_ts < %s ORDER BY snapshot_ts", (day_start, day_start + 86400))
ticks = [dict(zip(("snapshot_ts", "captured_at", "prev_snapshot_ts", "fut_security_id", "fut_symbol", "fut_bar_ts"), r))
         for r in cur.fetchall()]
if not ticks:
    sys.exit(f"no stored ticks on {day}")
contract = str(ticks[-1]["fut_security_id"])
cur.execute("SELECT max(captured_at) FROM algo.intraday_fno_data WHERE security_id = '13' AND snapshot_ts >= %s",
            (day_start,))
last15 = cur.fetchone()[0]
horizon = int(last15) - 30 if last15 else day_start
cur.execute("SELECT security_id, instrument_type, candle_ts, open, high, low, close, volume FROM algo.candle_5min "
            "WHERE ((security_id = '13' AND instrument_type = 'INDEX') OR (security_id = %s AND instrument_type = 'FUTIDX')) "
            "AND candle_ts >= %s AND candle_ts + 300 <= %s ORDER BY candle_ts", (contract, day_start, horizon))
DAY_BARS = [dict(zip(("security_id", "instrument_type", "candle_ts", "open", "high", "low", "close", "volume"), r))
            for r in cur.fetchall()]
cur.execute("SELECT snapshot_ts, captured_at, day_trend FROM algo.intraday_sentiments WHERE security_id = '13' "
            "AND snapshot_ts >= %s ORDER BY snapshot_ts", (day_start,))
rows15 = cur.fetchall()
since = day_start - HISTORY_DAYS * st.DAY_SECONDS
fno_all = db.read_fno_rows(c, "13", "INDEX", since, ticks[-1]["captured_at"])
contracts = {str(r["fut_security_id"]) for r in fno_all if r.get("fut_security_id")} | {contract}
store = inputs.Store(db.read_future_bars(c, sorted(contracts), since, horizon),
                     db.read_index_bars(c, "13", "INDEX", since, horizon), fno_all)
c.close()

caps = [int(t["captured_at"]) for t in ticks]
full = inputs.detect(store.session_inputs(st.ist_day(day_start), contract=contract, closed_by=horizon), pt.detect_session)
per_tick = {}
for e in full:
    cap = next((x for x in caps if x >= e["trigger_ts"] + 300), None)
    if cap is None or cap > horizon:
        continue
    seen = [r for r in rows15 if int(r[1]) <= cap]
    trend = (seen[-1][2] if seen else None) or "none"
    if alerts.passes(e, trend):
        per_tick.setdefault(cap, []).append(e)
if when:
    snap = int(datetime.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())
    tick = next((t for t in ticks if int(t["snapshot_ts"]) == snap), None) or sys.exit(f"no tick at {when}")
else:
    # Prefer a tick that already has a 15-min row, so the manager path runs too.
    first15 = min((int(r[1]) for r in rows15), default=None)
    cap = (max(per_tick, key=lambda k: (first15 is not None and k >= first15, len(per_tick[k]), -k))
           if per_tick else caps[-1])
    tick = next(t for t in ticks if int(t["captured_at"]) == cap)
expected = per_tick.get(int(tick["captured_at"]), [])
stamp = st.clock(int(tick["snapshot_ts"]))


class FakeDhan:
    def __init__(self, *a):
        self.calls = 0

    def intraday_candles(self, instrument, interval, d):
        self.calls += 1
        want_type = "INDEX" if instrument["instrument_type"] == "INDEX" else "FUTIDX"
        bars = [b for b in DAY_BARS if b["instrument_type"] == want_type and str(b["security_id"]) == str(instrument["security_id"])
                and int(b["candle_ts"]) <= int(tick["snapshot_ts"])]
        out = {k: [] for k in ("timestamp", "open", "high", "low", "close", "volume")}
        for b in bars:
            out["timestamp"].append(int(b["candle_ts"]))
            for k in ("open", "high", "low", "close"):
                out[k].append(float(b[k]))
            out["volume"].append(int(b["volume"] or 0))
        return out


SENT, MGR = [], []
handler.DhanClient = FakeDhan
handler.read_token_record = lambda: {"access_token": "x"}  # noqa: E731
handler.read_client_id = lambda r: "c"  # noqa: E731
handler.notify.send_telegram = lambda text: SENT.append(text)  # noqa: E731
handler.dispatch.invoke_manager = lambda payload: MGR.append(payload)  # noqa: E731
handler.ALERTS_ENABLED = True
handler.STRATEGY_MANAGER_FUNCTION_NAME = "strategy-manager"
event = {"source": "intraday-fno-poller", "tick": {**tick, "security_id": "13", "instrument_type": "INDEX"},
         "index": {"security_id": "13", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
         "future": {"security_id": contract, "exchange_segment": "NSE_FNO", "instrument_type": "FUTIDX"}}

# ---- 1. the real handler on one tick, in one rolled-back transaction ----------------------
SHARED["conn"] = SavepointConn(raw_connect())
captured = {}
real_upsert = db.upsert_events


def spy(conn, rows):
    captured["rows"] = rows
    return real_upsert(conn, rows)


db.upsert_events = spy
with contextlib.redirect_stderr(io.StringIO()):
    out = handler.lambda_handler(event, None)
db.upsert_events = real_upsert
rows = captured["rows"]
check(f"handler ran on the {day} {stamp} tick: {out['rows_upserted']} rows, {out['alerts']} alerts "
      f"(day trend {out['day_trend']})", out["rows_upserted"] == out["events_in_session"] == len(rows), out)
want = {tuple(r[k] for k in db.KEY): r for r in
        (db.event_row(e, "13", "INDEX", contract, pt.RULES_VERSION, 0, 0, 0) for e in full
         if e["trigger_ts"] + 300 <= int(tick["captured_at"]))}
got = {tuple(r[k] for k in db.KEY): r for r in rows}
fields = [k for k in db.COLUMNS if k not in ("first_seen_snapshot_ts", "first_seen_at", "created_at", "updated_at")]
diff = [k for k in set(got) & set(want) if any(got[k][f] != want[k][f] for f in fields)]
check("rows equal the library's re-derivation from the same live reads", set(got) == set(want) and not diff,
      f"only-handler {len(set(got) - set(want))}, only-library {len(set(want) - set(got))}, differing {len(diff)}")
check(f"alerts equal the rule on the stored day_trend ({len(expected)})", out["alerts"] == len(expected),
      (out["alerts"], len(expected)))
cur = SHARED["conn"].cursor()
cur.execute("SELECT count(*), count(telegram_sent_at), count(manager_sent_at) FROM algo.pattern_events "
            "WHERE rules_version = %s AND trigger_ts >= %s AND trigger_ts < %s",
            (pt.RULES_VERSION, day_start, day_start + 86400))
n_rows, n_tg, n_mgr = cur.fetchone()
check(f"in-transaction: {n_rows} rows, {n_tg} telegram marks = {len(SENT)} sends, {n_mgr} manager marks = {len(MGR)} invokes",
      n_rows == len(rows) and n_tg == len(SENT) == out["alerts"] and n_mgr == len(MGR) == out["sent_manager"],
      (n_rows, n_tg, len(SENT), n_mgr, len(MGR)))

# ---- 2. the same tick again: nothing resent ------------------------------------------------
with contextlib.redirect_stderr(io.StringIO()):
    out2 = handler.lambda_handler(event, None)
check("re-run of the same tick sends nothing new (claim finds the marks)",
      out2["sent_telegram"] == 0 and out2["sent_manager"] == 0 and len(SENT) == n_tg, out2)

# ---- 3. ON CONFLICT keeps first_seen_* and the marks -----------------------------------------
second = [{**r, "first_seen_snapshot_ts": 222, "first_seen_at": 222, "created_at": 222, "updated_at": 333,
           "vol_z": (r["vol_z"] or 0) + 1} for r in rows]
n2 = db.upsert_events(SHARED["conn"], second)
cur.execute("SELECT count(*), min(updated_at), max(first_seen_at), count(telegram_sent_at) FROM algo.pattern_events "
            "WHERE rules_version = %s AND trigger_ts >= %s AND trigger_ts < %s",
            (pt.RULES_VERSION, day_start, day_start + 86400))
cnt, upd, fsa, tg2 = cur.fetchone()
check("ON CONFLICT updates the rest, keeps first_seen_*, never clears a sent mark",
      n2 == len(rows) and cnt == len(rows) and upd == 333 and fsa != 222 and tg2 == n_tg, (n2, cnt, upd, fsa, tg2))
pick = next((r for r in second if r["other_levels"] != "{}" and r["level_strike"] is not None), second[0])
cur.execute("SELECT vol_z, other_levels, candles, level_strike, level_price FROM algo.pattern_events "
            "WHERE security_id = %s AND instrument_type = %s AND rules_version = %s AND trigger_ts = %s "
            "AND family = %s AND subtype = %s AND direction = %s", [pick[k] for k in db.KEY])
vz, ol, cd, ls, lp = cur.fetchone()
as_list = lambda lit: [x for x in lit.strip("{}").split(",") if x]  # noqa: E731
check("text[] and numerics round-trip", decimal.Decimal(str(pick["vol_z"])) == vz and ol == as_list(pick["other_levels"])
      and cd == as_list(pick["candles"]) and (ls is None and pick["level_strike"] is None or float(ls) == pick["level_strike"])
      and float(lp) == pick["level_price"], (vz, ol, cd, ls, lp))
SHARED["conn"].finish()

# ---- 4. nothing kept ------------------------------------------------------------------------
c = raw_connect()
cur = c.cursor()
cur.execute(GUARD, (pt.RULES_VERSION,))
after, marked_after = cur.fetchone()
c.close()
check(f"table unchanged afterwards ({after} rows, {marked_after} marked)",
      after == before and marked_after == marked_before, (before, after, marked_before, marked_after))
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
