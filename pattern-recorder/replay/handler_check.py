"""V1 (v2.1): the REAL handler over every stored 5-min tick, against fakes. Offline.

    python pattern-recorder/replay/handler_check.py [data_dir]     (default replay/data)

Fakes: Dhan serves the stored 5-min candles of the tick's day up to the tick's
forming bar; Neon is an in-memory copy of the pulled tables plus a
pattern_events table with real upsert / claim / mark semantics; Telegram and
the strategy-manager invoke are recorded. Historical 15-min rows predate the
day_trend column, so each gets the value market-classifier/trend.py would have
written at its captured_at - the same code the classifier runs.

Checks, per session with stored ticks:
  1  ROWS      after the day's last tick, the table equals the library's events
               for the whole day (inputs.Store over the same data), field by field
  2  FIRST     every row's first_seen_at is the first tick that could read its bar
  3  ALERTS    Telegram got exactly the expected alerts, each once: an event is
               judged on its first tick, against the latest 15-min row by then
  4  MANAGER   strategy-manager got each alert once, except those before the
               day's first 15-min row
  5  RERUN     re-running the day's last tick sends nothing new
  6  FAILURE   a send that fails on its first try leaves no mark and raises; the
               re-run sends it once
"""
import collections
import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
DATA = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "data"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
load = lambda n: json.load(open(DATA / f"{n}.json", encoding="utf-8"))  # noqa: E731
FUT, IDX, FNO, SENT, FNO5, DAILY = (load(n) for n in ("fut_5m", "idx_5m", "fno", "sent", "fno5", "daily"))
for r in IDX:
    r.setdefault("security_id", "13")

# ---- stubs for the layer modules --------------------------------------------------------
na = types.ModuleType("neon_access")
na.ist_datetime = lambda e: datetime.datetime.fromtimestamp(int(e), IST)  # noqa: E731
na.SSL_CONTEXT = None
na.read_neon_connection_string = lambda: "stub"  # noqa: E731
na.connect = lambda s: None  # noqa: E731
na.get_parameter = lambda name: "{}"  # noqa: E731
na.now_epoch = lambda: 0  # noqa: E731
sys.modules["neon_access"] = na
b3 = types.ModuleType("boto3")
b3.client = lambda *a, **k: None  # noqa: E731
sys.modules["boto3"] = b3

sys.path.insert(0, str(REPO / "pattern-recorder"))
spec = importlib.util.spec_from_file_location("mc_trend", REPO / "market-classifier" / "trend.py")
mc_trend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc_trend)
import structure as st  # noqa: E402
import patterns as pt  # noqa: E402
import inputs  # noqa: E402
import alerts  # noqa: E402
import db  # noqa: E402
import handler  # noqa: E402


def day_of(ts):
    return st.ist_day(int(ts))


# ---- historical day_trend, as market-classifier would have written it ------------------------
IDX_BY_DAY = collections.defaultdict(list)
for r in IDX:
    IDX_BY_DAY[day_of(r["candle_ts"])].append({"ts": int(r["candle_ts"]), "open": float(r["open"]),
                                                "high": float(r["high"]), "low": float(r["low"]),
                                                "close": float(r["close"])})
DAILY_ROWS = sorted(DAILY, key=lambda r: int(r["trade_date"]))


def daily_for(day):
    start = day * 86400 - 19800
    before = [r for r in DAILY_ROWS if int(r["trade_date"]) < start]
    return {**before[-1], "stale": False} if before else None


trend_failed_days = set()
for r in SENT:
    if r.get("day_trend") is not None:
        continue
    d = day_of(r["snapshot_ts"])
    try:
        r["day_trend"], _ = mc_trend.day_trend(IDX_BY_DAY[d], daily_for(d), int(r["captured_at"]))
    except RuntimeError:
        r["day_trend"] = None
        trend_failed_days.add(d)


# ---- fake Neon ---------------------------------------------------------------------------
class Table:
    def __init__(self):
        self.rows, self.pending = {}, {}


EVENTS = Table()
FNO_BY_TS = {int(r["snapshot_ts"]): r for r in FNO}
SENT_SORTED = sorted(SENT, key=lambda r: int(r["snapshot_ts"]))
FNO5_SORTED = sorted(FNO5, key=lambda r: int(r["snapshot_ts"]))
FUT_BY = collections.defaultdict(list)
for r in FUT:
    FUT_BY[str(r["security_id"])].append(r)


class Cur:
    def __init__(self, conn):
        self.conn, self.description, self._rows, self.rowcount = conn, None, [], 0

    def _set(self, cols, rows):
        self.description = [(c,) for c in cols]
        self._rows = [tuple(r.get(c) for c in cols) for r in rows]

    def execute(self, sql, params=()):
        p = list(params)
        if sql.startswith("INSERT INTO algo.pattern_events"):
            n = len(db.COLUMNS)
            rows = [dict(zip(db.COLUMNS, p[i:i + n])) for i in range(0, len(p), n)]
            for row in rows:
                k = tuple(row[c] for c in db.KEY)
                old = EVENTS.rows.get(k)
                if old:
                    row = {**row, **{c: old[c] for c in db.KEEP_ON_CONFLICT}}
                    row.update({m: old.get(m) for m in db.SENT_MARKS})
                EVENTS.rows[k] = row
            self.rowcount = len(rows)
        elif "FROM algo.pattern_events" in sql and "FOR UPDATE" in sql:
            col = re.search(r"AND (\w+) IS NULL", sql).group(1)
            k = tuple(p)
            row = EVENTS.rows.get(k)
            free = row is not None and row.get(col) is None and (k, col) not in EVENTS.pending
            self._rows = [(1,)] if free else []
        elif sql.startswith("UPDATE algo.pattern_events"):
            col = re.search(r"SET (\w+) =", sql).group(1)
            k = tuple(p[1:])
            self.rowcount = 1 if k in EVENTS.rows else 0
            EVENTS.pending[(k, col)] = p[0]
        elif "FROM algo.intraday_fno_data" in sql and "snapshot_ts = %s" in sql:
            r = FNO_BY_TS.get(int(p[2]))
            self._set(list(r) if r else ["snapshot_ts"], [r] if r else [])
        elif "FROM algo.intraday_fno_data" in sql:
            since, cap = int(p[2]), int(p[3])
            cols = ["snapshot_ts", "captured_at", "prev_snapshot_ts", "fut_security_id",
                    "near_max_oi_call", "near_max_oi_put", "near_max_pain"]
            self._set(cols, [r for r in FNO if int(r["snapshot_ts"]) >= since and int(r["captured_at"]) <= cap])
        elif "FROM algo.intraday_sentiments" in sql:
            start, cap = int(p[2]), int(p[3])
            got = [r for r in SENT_SORTED if int(r["snapshot_ts"]) >= start and int(r["captured_at"]) <= cap]
            self._set(list(SENT[0]), got[-1:])
        elif "FROM algo.candle_5min" in sql and "security_id IN" in sql:
            n = sql.count("%s") - 4
            contracts, since, bar, closed_by = p[1:1 + n], int(p[1 + n]), int(p[2 + n]), int(p[3 + n])
            rows = [r for c in contracts for r in FUT_BY[c]
                    if int(r["candle_ts"]) >= since and int(r["candle_ts"]) + bar <= closed_by]
            self._set(["security_id", "candle_ts", "open", "high", "low", "close", "volume"], rows)
        elif "FROM algo.candle_5min" in sql:
            since, bar, closed_by = int(p[2]), int(p[3]), int(p[4])
            rows = [r for r in IDX if int(r["candle_ts"]) >= since and int(r["candle_ts"]) + bar <= closed_by]
            self._set(["security_id", "candle_ts", "open", "high", "low", "close", "volume"], rows)
        elif "SELECT captured_at FROM algo.fno_5m_snapshot" in sql:
            got = [r for r in FNO5_SORTED if int(r["snapshot_ts"]) < int(p[2])]
            self._rows = [(got[-1]["captured_at"],)] if got else []
        elif "FROM algo.fno_5m_snapshot" in sql:
            start, cap = int(p[2]), int(p[3])
            self._rows = [(r["fut_bar_ts"], r["fut_bar_oi_change"]) for r in FNO5_SORTED
                          if int(r["snapshot_ts"]) >= start and int(r["captured_at"]) <= cap]
        elif "FROM algo.daily_market_sentiment" in sql:
            got = [r for r in DAILY_ROWS if int(r["trade_date"]) < int(p[2])]
            self._set(list(db.DAILY_COLUMNS), got[-1:])
        else:
            raise RuntimeError("unexpected SQL " + sql[:80])

    def fetchall(self):
        return self._rows


class Conn:
    def cursor(self):
        return Cur(self)

    def commit(self):
        for (k, col), v in EVENTS.pending.items():
            EVENTS.rows[k][col] = v
        EVENTS.pending.clear()

    def rollback(self):
        EVENTS.pending.clear()

    def close(self):
        pass


handler.connect = lambda s: Conn()  # noqa: E731
handler.read_token_record = lambda: {"access_token": "x"}  # noqa: E731
handler.read_client_id = lambda record: "c"  # noqa: E731
NOW_TICK = {"snapshot_ts": 0}


class FakeDhan:
    def __init__(self, *a):
        self.calls = 0

    def intraday_candles(self, instrument, interval, day):
        self.calls += 1
        sid = str(instrument["security_id"])
        src = IDX if instrument["instrument_type"] == "INDEX" else FUT_BY[sid]
        bars = [r for r in src if datetime.datetime.fromtimestamp(int(r["candle_ts"]), IST).date() == day
                and int(r["candle_ts"]) <= NOW_TICK["snapshot_ts"]]
        out = {k: [] for k in ("timestamp", "open", "high", "low", "close", "volume")}
        for r in sorted(bars, key=lambda r: int(r["candle_ts"])):
            out["timestamp"].append(int(r["candle_ts"]))
            for k in ("open", "high", "low", "close"):
                out[k].append(float(r[k]))
            out["volume"].append(int(r["volume"] or 0))
        return out


handler.DhanClient = FakeDhan
SENT_MSGS, MANAGER = [], []
FAIL_NEXT = {"on": False}


def fake_send(text):
    if FAIL_NEXT["on"]:
        FAIL_NEXT["on"] = False
        raise RuntimeError("injected telegram failure")
    SENT_MSGS.append(text)


handler.notify.send_telegram = fake_send
handler.dispatch.invoke_manager = lambda payload: MANAGER.append(payload)  # noqa: E731
handler.ALERTS_ENABLED = True
handler.STRATEGY_MANAGER_FUNCTION_NAME = "strategy-manager"


def run(tick):
    NOW_TICK["snapshot_ts"] = int(tick["snapshot_ts"])
    event = {"source": "intraday-fno-poller",
             "tick": {k: tick.get(k) for k in ("security_id", "instrument_type", "snapshot_ts", "captured_at",
                                               "prev_snapshot_ts", "fut_security_id", "fut_symbol", "fut_bar_ts")},
             "index": {"security_id": "13", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
             "future": {"security_id": str(tick["fut_security_id"]), "exchange_segment": "NSE_FNO",
                        "instrument_type": "FUTIDX"}}
    with contextlib.redirect_stderr(io.StringIO()):
        return handler.lambda_handler(json.loads(json.dumps(event, default=float)), None)


# ---- the run -------------------------------------------------------------------------------
ticks_by_day = collections.defaultdict(list)
for r in FNO5_SORTED:
    ticks_by_day[day_of(r["snapshot_ts"])].append(r)
EVENT_FIELDS = ("first_ts", "trigger_close", "extreme", "level", "level_price", "level_strike", "distance_pts",
                "distance_pct", "prior_tests", "chart_pattern", "structure_15m", "vol_z", "vol_z_max", "rvol_tod",
                "retest_vol_ratio", "sweep_depth_pct", "bars_beyond", "break_strength", "index_twin_offset_pct")
store = inputs.Store(FUT, IDX, FNO)
fails = collections.Counter()
totals = collections.Counter()
failure_tested = False
for day in sorted(ticks_by_day):
    date = datetime.date(1970, 1, 1) + datetime.timedelta(days=day)
    ticks = ticks_by_day[day]
    if day in trend_failed_days:
        print(f"{date}: SKIPPED - market-classifier/trend.py raises on this day's stored inputs")
        totals["skipped_days"] += 1
        continue
    SENT_MSGS.clear()
    MANAGER.clear()
    for i, tick in enumerate(ticks):
        out = run(tick)
        if not failure_tested and out["alerts"] == 0 and i + 1 < len(ticks):
            pass
    # 1 ROWS
    contract = str(ticks[-1]["fut_security_id"])
    full = inputs.detect(store.session_inputs(day, contract=contract), pt.detect_session)
    table = {(r["trigger_ts"], r["family"], r["subtype"], r["direction"]): r for k, r in EVENTS.rows.items()
             if day_of(r["trigger_ts"]) == day and r["rules_version"] == pt.RULES_VERSION}
    want = {(e["trigger_ts"], e["family"], e["subtype"] or "none", e["direction"]): e for e in full}
    row_bad = set(table) ^ set(want)
    row_bad |= {k for k in set(table) & set(want)
                if any(table[k][f] != want[k][f] for f in EVENT_FIELDS if f not in ("level_strike",))
                or (table[k]["level_strike"] != want[k]["level_strike"])}
    # 2 FIRST
    caps = sorted(int(t["captured_at"]) for t in ticks)
    first_bad = [k for k, r in table.items()
                 if r["first_seen_at"] != next((c for c in caps if c >= r["trigger_ts"] + 300), None)]
    # 3 + 4 ALERTS / MANAGER: expected from first sight
    expect, expect_mgr = [], 0
    for k, e in want.items():
        cap = next((c for c in caps if c >= e["trigger_ts"] + 300), None)
        if cap is None:
            continue
        rows15 = [r for r in SENT_SORTED if day_of(r["snapshot_ts"]) == day and int(r["captured_at"]) <= cap]
        trend = (rows15[-1]["day_trend"] if rows15 else None) or "none"
        if alerts.passes(e, trend):
            expect.append(k)
            expect_mgr += 1 if rows15 else 0
    marks = [k for k, r in table.items() if r.get("telegram_sent_at") is not None]
    alert_bad = sorted(set(expect) ^ set(marks)) + (["dup"] if len(SENT_MSGS) != len(expect) else [])
    mgr_bad = len(MANAGER) != expect_mgr
    # 5 RERUN
    before = (len(SENT_MSGS), len(MANAGER))
    run(ticks[-1])
    rerun_bad = (len(SENT_MSGS), len(MANAGER)) != before
    # 6 FAILURE (once, on the first day with an alert): un-mark one alert, fail its send, re-run
    fail_bad = False
    if not failure_tested and marks:
        k0 = (sorted(marks)[0])
        full_key = next(kk for kk, r in EVENTS.rows.items()
                        if (r["trigger_ts"], r["family"], r["subtype"], r["direction"]) == k0
                        and r["rules_version"] == pt.RULES_VERSION)
        EVENTS.rows[full_key]["telegram_sent_at"] = None
        cap = next(c for c in caps if c >= k0[0] + 300)
        t0 = next(t for t in ticks if int(t["captured_at"]) == cap)
        FAIL_NEXT["on"] = True
        raised = False
        try:
            run(t0)
        except RuntimeError as exc:
            raised = "injected" in str(exc)
        unmarked = EVENTS.rows[full_key]["telegram_sent_at"] is None
        n0 = len(SENT_MSGS)
        run(t0)
        fail_bad = not (raised and unmarked and EVENTS.rows[full_key]["telegram_sent_at"] is not None
                        and len(SENT_MSGS) == n0 + 1)
        failure_tested = True
        print(f"   failure test on {date} {k0}: raised={raised} unmarked_after_failure={unmarked} "
              f"sent_on_rerun={len(SENT_MSGS) == n0 + 1}")
    status = {"rows": not row_bad, "first": not first_bad, "alerts": not alert_bad, "manager": not mgr_bad,
              "rerun": not rerun_bad, "failure": not fail_bad}
    for name, ok in status.items():
        fails[name] += 0 if ok else 1
    totals["days"] += 1
    totals["ticks"] += len(ticks)
    totals["rows"] += len(table)
    totals["alerts"] += len(expect)
    totals["manager"] += expect_mgr
    print(f"{date}: {len(ticks)} ticks, {len(table)} rows, {len(expect)} alerts ({expect_mgr} to the manager)  "
          + " ".join(f"{n}={'ok' if ok else 'FAIL'}" for n, ok in status.items()))

print()
print(f"{totals['days']} sessions, {totals['ticks']} ticks, {totals['rows']} rows, {totals['alerts']} alerts "
      f"({totals['alerts'] / max(totals['days'], 1):.1f}/day), {totals['manager']} manager dispatches; "
      f"{totals['skipped_days']} skipped; failure test run: {failure_tested}")
print("ALL PASS" if not any(fails.values()) and failure_tested else f"FAILED: {dict(fails)}")
sys.exit(0 if not any(fails.values()) and failure_tested else 1)
