"""Offline replay of the v2 pattern library (pattern-recorder) over stored history.

    python pattern-recorder/replay/replay.py [data_dir]

Reads the JSON pulled by fetch.py (default replay/data), runs structure.py +
patterns.py from the parent folder per session, and writes out/replay_report.md and
out/events.json. Descriptive only: no hit rates, no P&L (patterns before exits).

What it checks and measures:
  - live == replay: on sessions with stored run clocks, detection on bars closed by
    each run's captured_at must equal full-session detection (no look-ahead), and
    no event may be announced twice or never;
  - recall against algo.session_observations;
  - OI joined at read time (15-min snapshots, 5-min fno_5m_snapshot);
  - forward moves on the future, against a no-pattern baseline of every bar.
"""
import collections
import datetime
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import structure as st  # noqa: E402
import patterns as pt   # noqa: E402
import inputs           # noqa: E402  - the SAME assembly the Lambda uses

DATA = sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")
SEP_FUT = "68407"
DAMAGE_FROM = datetime.date(2026, 9, 11)
DAMAGED_MINUTES = {9 * 60 + 40 + 15 * k for k in range(22)} | {15 * 60 + 10, 15 * 60 + 20}
OI_CONFIRM_PCT = 3.0
HORIZONS = (15, 30, 60)


def load(name):
    with open(os.path.join(DATA, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def day_date(day):
    return datetime.date(1970, 1, 1) + datetime.timedelta(days=day)


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


fut_rows, idx_rows = load("fut_5m"), load("idx_5m")
fno_rows, sent_rows, fno5_rows, obs_rows = load("fno"), load("sent"), load("fno5"), load("obs")
for r in fno_rows:
    r["snapshot_ts"] = int(r["snapshot_ts"])

TODAY = st.ist_day(time.time())
store = inputs.Store(fut_rows, idx_rows, fno_rows)
fno_by_ts = {r["snapshot_ts"]: r for r in fno_rows}
sent_by_ts = {int(r["snapshot_ts"]): r for r in sent_rows}
fno5_by_bar = {(str(r["fut_security_id"]), int(r["fut_bar_ts"])): r for r in fno5_rows}
days = [d for d in store.days if d < TODAY]

# ---- per-session detection --------------------------------------------------------------
sessions = []
for day in days:
    contract, how = store.active_contract(day)
    if contract is None:
        continue
    si = store.session_inputs(day, contract)
    if not si["session_bars"]:
        continue
    sessions.append({
        "day": day, "date": day_date(day), "contract": contract, "contract_how": how,
        "bars": si["session_bars"], "prev": si["prev_bars"], "idx": si["index_bars"],
        "prev_idx": si["prev_index_bars"], "fno": si["fno_rows"], "rvol": si["rvol_history"],
        "damaged": contract == SEP_FUT and day_date(day) >= DAMAGE_FROM,
    })

all_events = []
for s in sessions:
    evs = pt.detect_session(s["bars"], s["prev"], s["idx"], s["prev_idx"], s["fno"], s["rvol"])
    for e in evs:
        e["date"] = s["date"].isoformat()
        e["contract"] = s["contract"]
        e["session_damaged"] = s["damaged"]
        e["slot_damaged"] = s["damaged"] and any(
            st.ist_minute(b["ts"]) in DAMAGED_MINUTES
            for b in s["bars"] if e["first_ts"] <= b["ts"] <= e["trigger_ts"])
    s["events"] = evs
    all_events.extend(evs)


# ---- consistency: simulated live runs vs full-session detection --------------------------
def run_clocks(s):
    """
    [(snapshot_ts, captured_at, closed_after)] for each run of the session, where
    closed_after is the freshness lower bound live uses: the captured_at of the
    row at prev_snapshot_ts. On a session's first run that row is the PREVIOUS
    day's last, so the whole morning is fresh even when the first run is late
    (09-30, 10-08). Measured 2026-11-05; the Phase A replay wrongly used "one
    interval back" here. Sessions before stored rows existed are simulated on the
    09:45 grid (+70 s, the measured median), first run fresh from the open.
    """
    if s["fno"]:
        out = []
        for r in s["fno"]:
            cap = int(r["captured_at"]) if r.get("captured_at") else r["snapshot_ts"] + 70
            prev = fno_by_ts.get(int(r["prev_snapshot_ts"])) if r.get("prev_snapshot_ts") else None
            after = int(prev["captured_at"]) if prev and prev.get("captured_at") else cap - 900
            out.append((r["snapshot_ts"], cap, after))
        return out, "stored"
    base = st.ist_day_start(s["bars"][0]["ts"])
    grid = [9 * 60 + 45 + 15 * k for k in range(24)] + [15 * 60 + 35]
    out, after = [], s["bars"][0]["ts"] - 1
    for m in grid:
        cap = base + m * 60 + 70
        out.append((base + m * 60, cap, after))
        after = cap
    return out, "simulated"


cons = {"runs": 0, "truncated_checked": 0, "truncated_mismatch": 0, "skipped": 0, "double": 0}
mismatch_examples = []
for s in sessions:
    clocks, how = run_clocks(s)
    assigned = collections.Counter()
    for snap_ts, cap, closed_after in clocks:
        expect = pt.fresh_events(s["events"], closed_after, cap)
        for e in expect:
            assigned[id(e)] += 1
        cons["runs"] += 1
        if how == "stored":
            # What live computes at this run: the same assembly, bars closed by
            # its captured_at, fno rows up to its snapshot.
            cut = store.session_inputs(s["day"], s["contract"], closed_by=cap, snapshot_ts=snap_ts)
            got = pt.fresh_events(inputs.detect(cut, pt.detect_session), closed_after, cap)
            strip = lambda es: [{k: v for k, v in e.items() if k not in ("date", "contract", "session_damaged", "slot_damaged")} for e in es]  # noqa: E731
            cons["truncated_checked"] += 1
            if strip(got) != strip(expect):
                cons["truncated_mismatch"] += 1
                if len(mismatch_examples) < 3:
                    mismatch_examples.append((s["date"].isoformat(), st.clock(snap_ts),
                                              len(got), len(expect)))
    for e in s["events"]:
        n = assigned[id(e)]
        if n == 0:
            cons["skipped"] += 1
        elif n > 1:
            cons["double"] += 1


# ---- read-time joins: forward moves and OI --------------------------------------------------
bars_by_session = {s["date"].isoformat(): s["bars"] for s in sessions}


def forward(e):
    bars = bars_by_session[e["date"]]
    c = e["trigger_close"]
    after = [b for b in bars if b["ts"] > e["trigger_ts"]]
    out = {}
    up = e["direction"] == "up"
    for h in HORIZONS + ("close",):
        win = after if h == "close" else [b for b in after if b["ts"] < e["trigger_ts"] + 300 + h * 60]
        need = None if h == "close" else h // 5
        if not win or (need and len(win) < need):
            out[h] = None
            continue
        hi, lo, last = max(b["high"] for b in win), min(b["low"] for b in win), win[-1]["close"]
        mfe = (hi - c) if up else (c - lo)
        mae = (c - lo) if up else (hi - c)
        move = (last - c) if up else (c - last)
        out[h] = {"mfe": mfe, "mae": mae, "move": move}
    return out


def tick_rows(trigger_ts):
    t0 = -(-(trigger_ts + 300) // 900) * 900
    return fno_by_ts.get(t0), fno_by_ts.get(t0 + 900), fno_by_ts.get(t0 - 900), sent_by_ts.get(t0), sent_by_ts.get(t0 + 900)


BULL = ("LONG_BUILDUP", "SHORT_COVERING")
BEAR = ("SHORT_BUILDUP", "LONG_UNWINDING")


def oi15(e):
    f0, f1, fprev, s0, s1 = tick_rows(e["trigger_ts"])
    if f0 is None:
        return None
    up = e["direction"] == "up"
    b0 = (s0 or {}).get("buildup")
    b1 = (s1 or {}).get("buildup")
    pe0, ce0 = num(f0.get("near_pe_oi_change_pct")), num(f0.get("near_ce_oi_change_pct"))
    pe1 = num((f1 or {}).get("near_pe_oi_change_pct"))
    ce1 = num((f1 or {}).get("near_ce_oi_change_pct"))
    pcr0, pcrp = num(f0.get("near_pcr_oi")), num((fprev or {}).get("near_pcr_oi"))
    favour = BULL if up else BEAR
    unwind0 = (pe0 if up else ce0)
    unwind1 = (pe1 if up else ce1)
    return {
        "buildup0": b0, "buildup1": b1,
        "buildup_agrees0": b0 in favour if b0 else None,
        "buildup_agrees1": b1 in favour if b1 else None,
        "opt_unwind0": None if unwind0 is None else unwind0 <= -OI_CONFIRM_PCT,
        "opt_unwind_either": None if unwind0 is None else (unwind0 <= -OI_CONFIRM_PCT or (unwind1 is not None and unwind1 <= -OI_CONFIRM_PCT)),
        "pcr_agrees0": None if pcr0 is None or pcrp is None else ((pcr0 > pcrp) if up else (pcr0 < pcrp)),
        "fut_oi_falling0": None if num(f0.get("fut_oi_change_pct")) is None else num(f0.get("fut_oi_change_pct")) < 0,
    }


def oi5(e):
    r = fno5_by_bar.get((e["contract"], e["trigger_ts"]))
    if r is None or r.get("fut_bar_oi_change") is None:
        return None
    bars = bars_by_session[e["date"]]
    i = next(k for k, b in enumerate(bars) if b["ts"] == e["trigger_ts"])
    dp = bars[i]["close"] - (bars[i - 1]["close"] if i > 0 else bars[i]["open"])
    doi = float(r["fut_bar_oi_change"])
    quad = ("long_buildup" if doi > 0 else "short_covering") if dp > 0 else ("short_buildup" if doi > 0 else "long_unwinding")
    nxt = [fno5_by_bar.get((e["contract"], e["trigger_ts"] + 300 * k)) for k in (1, 2, 3)]
    nxt_oi = [float(x["fut_bar_oi_change"]) for x in nxt if x and x.get("fut_bar_oi_change") is not None]
    return {"quadrant": quad, "doi": doi,
            "next3_doi": sum(nxt_oi) if len(nxt_oi) == 3 else None}


for e in all_events:
    e["fwd"] = forward(e)
    e["oi15"] = oi15(e)
    e["oi5"] = oi5(e)


# ---- recall against the hand-logged rows -----------------------------------------------------
EXPECT = {  # v2-pattern-definitions.md section 10 mapping
    "orb_breakout_fade_reclaim": ({"C2"}, {"orh"}),
    "evening_star": ({"R1"}, None),
    "double_bottom": ({"R1", "R2"}, None),
    "double_top": ({"R1", "R2"}, None),
    # Mapped R2 in the definitions, which was wrong: 23300 had been crossed UP after
    # the 11:30 low, so a touch from above is a RETEST (C2) - the same reading as
    # orb_breakout_fade_reclaim above. Corrected 2026-11-05.
    "breakout_fade_reclaim": ({"C2"}, {"max_pain", "put_wall"}),
    "orb_breakout_retest": ({"C1"}, {"orh", "pdh"}),
    "orb_liquidity_sweep": ({"R2"}, {"orl"}),
    "pd_breakout_retest": ({"C1", "C2"}, {"pdh"}),
}
recall = []
for o in obs_rows:
    ets = int(o["event_ts"])
    fwd = num(o.get("forward_return_pct")) or 0
    direction = "up" if fwd > 0 else "down"
    date = day_date(st.ist_day(ets)).isoformat()
    near = [e for e in all_events if e["date"] == date and abs(e["trigger_ts"] - ets) <= 900]
    same_dir = [e for e in near if e["direction"] == direction]
    fams, lvls = EXPECT.get(o["pattern_type"], (set(), None))
    exact = [e for e in same_dir if e["family"] in fams
             and (lvls is None or e["level"] in lvls or set(e["other_levels"]) & lvls)]
    sess = next((s for s in sessions if s["date"].isoformat() == date), None)
    recall.append({"row": f"{date[5:]} {st.clock(ets)}", "pattern_type": o["pattern_type"],
                   "direction": direction, "damaged_session": bool(sess and sess["damaged"]),
                   "exact": exact, "same_dir": same_dir, "near": near})


# ---- report ---------------------------------------------------------------------------------------
def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def pct(flags):
    flags = [f for f in flags if f is not None]
    return f"{100 * sum(flags) / len(flags):.0f}% (n={len(flags)})" if flags else "n/a"


def bucket(clock):
    h, m = map(int, clock.split(":"))
    t = h * 60 + m
    for lo, hi, name in ((570, 630, "09:30-10:25"), (630, 720, "10:30-11:55"), (720, 810, "12:00-13:25"),
                         (810, 900, "13:30-14:55"), (900, 930, "15:00-15:25")):
        if lo <= t < hi:
            return name
    return "other"


def key(e):
    return f"{e['family']}{'/' + e['subtype'] if e['subtype'] else ''} {e['direction']}"


clean = [s for s in sessions if not s["damaged"]]
dam = [s for s in sessions if s["damaged"]]
ev_clean = [e for e in all_events if not e["session_damaged"]]
L = []
L.append("# pattern-detector v2 - Phase A replay report\n")
L.append(f"Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} local. Library: "
         f"`pattern-detector/structure.py` + `patterns.py` (uncommitted). NEAR {st.NEAR_PCT}%, "
         f"MIN_RANGE {pt.MIN_RANGE_PCT}%, RECLAIM_MAX_BARS {pt.RECLAIM_MAX_BARS}, SWING_K {st.SWING_K}.\n")
L.append("**Descriptive only** - no hit rates, no P&L. Forward moves are on the future, from the trigger bar's close.\n")
L.append("## 1. Coverage\n")
L.append(f"- Sessions replayed: **{len(sessions)}** ({sessions[0]['date']} -> {sessions[-1]['date']}); "
         f"clean **{len(clean)}**, damaged SEP-future **{len(dam)}** ({dam[0]['date'] if dam else '-'} -> {dam[-1]['date'] if dam else '-'}).")
L.append(f"- With stored 15-min OI rows (option walls, run clocks): {sum(1 for s in sessions if s['fno'])}; "
         f"with 5-min OI: {len({e['date'] for e in all_events if e['oi5']})}.")
L.append(f"- Events: **{len(all_events)}** total; on clean sessions {len(ev_clean)} "
         f"= **{len(ev_clean) / max(len(clean), 1):.1f} per session**.\n")

per_sess = [len(s["events"]) for s in clean]
if per_sess:
    L.append(f"Events per clean session: min {min(per_sess)}, median {statistics.median(per_sess)}, max {max(per_sess)}.\n")

L.append("## 2. Consistency (does live == replay?)\n")
L.append(f"- Simulated runs: {cons['runs']}. Events never announced by any run: **{cons['skipped']}**; announced twice: **{cons['double']}**.")
L.append(f"- Real-data no-look-ahead check (sessions with stored run clocks): detection on bars closed by each run's "
         f"`captured_at` vs full-session detection, fresh events compared: **{cons['truncated_checked'] - cons['truncated_mismatch']}/{cons['truncated_checked']} runs identical**.")
if mismatch_examples:
    L.append(f"- Mismatch examples (date, run, got, expected): {mismatch_examples}")
L.append("")

L.append("## 3. Events per session, by family (clean sessions)\n")
L.append("| Family | Events | Per session | With vol z >= 2 | Median vol_z_max | Median rvol_tod |")
L.append("|---|---|---|---|---|---|")
by_key = collections.defaultdict(list)
for e in ev_clean:
    by_key[key(e)].append(e)
for k_ in sorted(by_key):
    es = by_key[k_]
    L.append(f"| {k_} | {len(es)} | {len(es) / len(clean):.2f} | {sum(1 for e in es if (e['vol_z'] or 0) >= 2)} | "
             f"{med([e['vol_z_max'] for e in es])} | {med([e['rvol_tod'] for e in es])} |")
L.append("")

L.append("### By primary level (clean sessions)\n")
L.append("| Level | " + " | ".join(["R1", "R2", "C1", "C2"]) + " |")
L.append("|---|---|---|---|---|")
lv_count = collections.defaultdict(collections.Counter)
for e in ev_clean:
    lv_count[e["level"]][e["family"]] += 1
for lv in sorted(lv_count):
    L.append(f"| {lv} | " + " | ".join(str(lv_count[lv][f]) for f in ("R1", "R2", "C1", "C2")) + " |")
L.append("")

L.append("### R1 candles and chart-pattern labels (clean sessions)\n")
cand_c = collections.Counter(c for e in ev_clean if e["family"] == "R1" for c in e["candles"])
cp_c = collections.Counter(e["chart_pattern"] for e in ev_clean if e["family"] in ("R1", "R2") and e["chart_pattern"])
L.append("- R1 candles: " + ", ".join(f"{k} {v}" for k, v in cand_c.most_common()))
L.append("- Chart-pattern labels on R1/R2: " + (", ".join(f"{k} {v}" for k, v in cp_c.most_common()) or "none"))
L.append("")

L.append("### By time of day (clean sessions, events per session)\n")
L.append("| Window | R1 | R2 | C1 | C2 |")
L.append("|---|---|---|---|---|")
tb = collections.defaultdict(collections.Counter)
for e in ev_clean:
    tb[bucket(e["clock"])][e["family"]] += 1
for b_ in ("09:30-10:25", "10:30-11:55", "12:00-13:25", "13:30-14:55", "15:00-15:25"):
    L.append(f"| {b_} | " + " | ".join(f"{tb[b_][f] / len(clean):.2f}" for f in ("R1", "R2", "C1", "C2")) + " |")
L.append("")

L.append("## 4. Recall against the hand-logged rows\n")
L.append("Window: trigger within 15 min of the logged `event_ts`. *Exact* = the mapped family (and level, where the mapping names one), same direction.\n")
L.append("| Row | pattern_type | dir | damaged | exact match | other same-direction events within 15 min |")
L.append("|---|---|---|---|---|---|")
for r in recall:
    ex = "; ".join(f"{e['clock']} {key(e)} @{e['level']} ({e['distance_pts']:+.0f}) {','.join(e['candles'])} {e['chart_pattern'] or ''}".strip() for e in r["exact"]) or "**none**"
    oth = "; ".join(f"{e['clock']} {key(e)} @{e['level']}" for e in r["same_dir"] if e not in r["exact"]) or "-"
    L.append(f"| {r['row']} | {r['pattern_type']} | {r['direction']} | {'yes' if r['damaged_session'] else 'no'} | {ex} | {oth} |")
L.append("")

L.append("## 5. OI at read time\n")
L.append(f"15-min rows ({sum(1 for s in sessions if s['fno'])} sessions). tick0 = the snapshot whose window holds the trigger bar's close, tick1 = the next. "
         f"Option unwind = the far-side option OI change <= -{OI_CONFIRM_PCT}% (PE for up, CE for down).\n")
L.append("| Family | n | buildup agrees tick0 | tick1 | option unwind tick0 | tick0 or 1 | PCR moves its way tick0 | fut OI falling tick0 |")
L.append("|---|---|---|---|---|---|---|---|")
for k_ in sorted(by_key):
    es = [e for e in all_events if key(e) == k_ and e["oi15"]]
    if not es:
        continue
    g = lambda f: pct([e["oi15"][f] for e in es])  # noqa: E731
    L.append(f"| {k_} | {len(es)} | {g('buildup_agrees0')} | {g('buildup_agrees1')} | {g('opt_unwind0')} | {g('opt_unwind_either')} | {g('pcr_agrees0')} | {g('fut_oi_falling0')} |")
L.append("")
L.append(f"5-min futures OI at the trigger bar ({len({e['date'] for e in all_events if e['oi5']})} sessions): price/OI quadrant of the trigger bar, and the sign of the OI change over the next three bars.\n")
L.append("| Family | n | long_buildup | short_covering | short_buildup | long_unwinding | next-3 OI falling |")
L.append("|---|---|---|---|---|---|---|")
for k_ in sorted(by_key):
    es = [e for e in all_events if key(e) == k_ and e["oi5"]]
    if not es:
        continue
    q = collections.Counter(e["oi5"]["quadrant"] for e in es)
    L.append(f"| {k_} | {len(es)} | " + " | ".join(str(q[x]) for x in ("long_buildup", "short_covering", "short_buildup", "long_unwinding"))
             + f" | {pct([None if e['oi5']['next3_doi'] is None else e['oi5']['next3_doi'] < 0 for e in es])} |")
L.append("")

L.append("## 6. What the future did next (clean sessions, descriptive)\n")
L.append("Points from the trigger close, in the event's direction. MFE = best move in its favour, MAE = worst against, move = where it stood at the horizon. Medians.\n")
L.append("| Family | n | 15m MFE / MAE | 30m MFE / MAE | 60m MFE / MAE | 60m move | to close move |")
L.append("|---|---|---|---|---|---|---|")
for k_ in sorted(by_key):
    es = by_key[k_]

    def m(h, f):
        return med([e["fwd"][h][f] for e in es if e["fwd"].get(h)])
    L.append(f"| {k_} | {len(es)} | {m(15, 'mfe')} / {m(15, 'mae')} | {m(30, 'mfe')} / {m(30, 'mae')} | {m(60, 'mfe')} / {m(60, 'mae')} | {m(60, 'move')} | {m('close', 'move')} |")
L.append("")
L.append("Same, split by time of day for the two reversal families (60m MFE / MAE, n):\n")
L.append("| Window | R1 up | R1 down | R2 up | R2 down |")
L.append("|---|---|---|---|---|")
for b_ in ("09:30-10:25", "10:30-11:55", "12:00-13:25", "13:30-14:55", "15:00-15:25"):
    cells = []
    for f, d in (("R1", "up"), ("R1", "down"), ("R2", "up"), ("R2", "down")):
        es = [e for e in ev_clean if e["family"] == f and e["direction"] == d and bucket(e["clock"]) == b_ and e["fwd"].get(60)]
        cells.append(f"{med([e['fwd'][60]['mfe'] for e in es])} / {med([e['fwd'][60]['mae'] for e in es])} ({len(es)})" if es else "-")
    L.append(f"| {b_} | " + " | ".join(cells) + " |")
L.append("")

L.append("## 7. Behaviours worth a decision\n")
whip = 0
for s in clean:
    evs = s["events"]
    for e in evs:
        if e["family"] == "R2":
            if any(x["family"] == "C2" and x["level"] == e["level"] and x["direction"] != e["direction"]
                   and 0 < x["trigger_ts"] - e["trigger_ts"] <= 6 * 300 for x in evs):
                whip += 1
r2_total = sum(1 for e in ev_clean if e["family"] == "R2")
L.append(f"- **R2 then opposite C2 at the same level within 30 min** (a failed-break read that turned into a retest): {whip} of {r2_total} R2 events.")
c2_repeat = collections.Counter((e["date"], e["level"], e["direction"]) for e in ev_clean if e["family"] == "C2")
L.append(f"- **Repeated C2 at one level in one session**: {sum(1 for v in c2_repeat.values() if v > 1)} level-sessions had 2+ C2 events (max {max(c2_repeat.values()) if c2_repeat else 0}).")
dens = collections.Counter(e["level"] for e in ev_clean)
L.append(f"- **Busiest levels**: " + ", ".join(f"{k} {v / len(clean):.1f}/session" for k, v in dens.most_common(4)) + ".")
late = sum(1 for e in ev_clean if bucket(e["clock"]) == "15:00-15:25")
L.append(f"- **15:00 or later**: {late} events ({late / max(len(ev_clean), 1) * 100:.0f}%).")
L.append("")


# ---- 8. follow-up cuts: unannounced events, baseline, splits, fair OI test ----------------------
def fwd_from(e, ref_ts):
    """Forward 60m in the event's direction from the close of the bar ending at ref_ts."""
    bars = bars_by_session[e["date"]]
    ref = next((b for b in bars if b["ts"] + 300 == ref_ts), None)
    if ref is None:
        return None
    win = [b for b in bars if ref["ts"] < b["ts"] < ref["ts"] + 300 + 3600]
    if len(win) < 12:
        return None
    c, up = ref["close"], e["direction"] == "up"
    hi, lo, last = max(b["high"] for b in win), min(b["low"] for b in win), win[-1]["close"]
    return {"mfe": (hi - c) if up else (c - lo), "mae": (c - lo) if up else (hi - c),
            "move": (last - c) if up else (c - last)}


def cut_row(name, rows):
    rows = [r for r in rows if r]
    return (f"| {name} | {len(rows)} | {med([r['mfe'] for r in rows])} / {med([r['mae'] for r in rows])} | "
            f"{med([r['move'] for r in rows])} |")


L.append("## 8. Follow-up cuts\n")
skipped = []
for s in sessions:
    clocks, how = run_clocks(s)
    hit = set()
    for snap, cap, after in clocks:
        for e in pt.fresh_events(s["events"], after, cap):
            hit.add(id(e))
    for e in s["events"]:
        if id(e) not in hit:
            closes = e["trigger_ts"] + 300
            why = ("before the first run's window" if closes <= clocks[0][2]
                   else "after the last run" if closes > clocks[-1][1] else "between runs")
            skipped.append((e["date"], why, st.clock(clocks[0][1])))
by_why = collections.Counter((d, w, f) for d, w, f in skipped)
L.append(f"**Events no run would announce: {len(skipped)}** - " + (", ".join(
    f"{d} {w} (first run {f}) x{n}" for (d, w, f), n in sorted(by_why.items())) or "none") + "\n")

base = {"up": [], "down": []}
for s in clean:
    for b in s["bars"]:
        if 570 <= st.ist_minute(b["ts"]) <= 895:
            for d in ("up", "down"):
                f = forward({"date": s["date"].isoformat(), "trigger_ts": b["ts"],
                             "trigger_close": b["close"], "direction": d}).get(60)
                if f:
                    base[d].append(f)
L.append("**No-pattern baseline** - every clean-session bar 09:30-14:55, read in each direction. "
         "Then splits of the events (clean sessions). 60m for / against, and the move at 60m, medians, pts.\n")
L.append("| Cut | n | 60m for / against | 60m move |")
L.append("|---|---|---|---|")
L.append(cut_row("BASELINE any bar, up", base["up"]))
L.append(cut_row("BASELINE any bar, down", base["down"]))
f60 = lambda es: [e["fwd"].get(60) for e in es]  # noqa: E731
rev = [e for e in ev_clean if e["family"] in ("R1", "R2")]
r1 = [e for e in ev_clean if e["family"] == "R1"]
for name, es in (
        ("R1 textbook shape (not plain)", [e for e in r1 if e["subtype"] != "plain"]),
        ("R1 plain (B2)", [e for e in r1 if e["subtype"] == "plain"]),
        ("R1 doji only", [e for e in r1 if e["candles"] == ["doji"]]),
        ("R1/R2 first test", [e for e in rev if not e["chart_pattern"]]),
        ("R1/R2 double / triple / multi", [e for e in rev if e["chart_pattern"]]),
        ("R1/R2 vol_z_max >= 2", [e for e in rev if (e["vol_z_max"] or 0) >= 2]),
        ("R1/R2 vol_z_max < 2", [e for e in rev if (e["vol_z_max"] or 0) < 2]),
        ("R1/R2 at pdh/pdl/orh/orl", [e for e in rev if e["level"] in ("pdh", "pdl", "orh", "orl")]),
        ("R1/R2 at 15-min swings", [e for e in rev if e["level"].startswith("swing")]),
        ("R1/R2 at walls / max pain", [e for e in rev if e["level"] in ("call_wall", "put_wall", "max_pain")]),
        ("R2 wick", [e for e in ev_clean if e["subtype"] == "wick"]),
        ("C1 vol_z >= 2", [e for e in ev_clean if e["family"] == "C1" and (e["vol_z"] or 0) >= 2]),
        ("C1 vol_z < 2", [e for e in ev_clean if e["family"] == "C1" and (e["vol_z"] or 0) < 2]),
        ("C2 (first retest only, B1)", [e for e in ev_clean if e["family"] == "C2"])):
    L.append(cut_row(name, f60(es)))
L.append("")

L.append("**OI, measured from when the confirmation is KNOWN** (clean sessions with 15-min OI). "
         "Measuring from the trigger instead is circular: a later snapshot's buildup is built from "
         "the very price move being measured.\n")
L.append("| Cut | n | 60m for / against | 60m move |")
L.append("|---|---|---|---|")
t0 = lambda e: -(-(e["trigger_ts"] + 300) // 900) * 900  # noqa: E731
t1 = lambda e: t0(e) + 900  # noqa: E731
ev_oi = [e for e in ev_clean if e["oi15"]]
for label, fams in (("R1/R2", ("R1", "R2")), ("C1", ("C1",)), ("C2", ("C2",))):
    es = [e for e in ev_oi if e["family"] in fams]
    L.append(cut_row(f"{label} buildup agrees at tick0 (from tick0)", [fwd_from(e, t0(e)) for e in es if e["oi15"]["buildup_agrees0"]]))
    L.append(cut_row(f"{label} buildup disagrees at tick0 (from tick0)", [fwd_from(e, t0(e)) for e in es if e["oi15"]["buildup_agrees0"] is False]))
    L.append(cut_row(f"{label} buildup agrees at tick1 (from tick1)", [fwd_from(e, t1(e)) for e in es if e["oi15"]["buildup_agrees1"]]))
    L.append(cut_row(f"{label} buildup disagrees at tick1 (from tick1)", [fwd_from(e, t1(e)) for e in es if e["oi15"]["buildup_agrees1"] is False]))
    L.append(cut_row(f"{label} option unwind at tick0 (from tick0)", [fwd_from(e, t0(e)) for e in es if e["oi15"]["opt_unwind0"]]))
    L.append(cut_row(f"{label} no option unwind at tick0 (from tick0)", [fwd_from(e, t0(e)) for e in es if e["oi15"]["opt_unwind0"] is False]))
L.append("")

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "replay_report.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(L) + "\n")
with open(os.path.join(OUT, "events.json"), "w", encoding="utf-8") as f:
    json.dump(all_events, f, default=str)
print("\n".join(L))
