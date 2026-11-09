"""Hand-built logic checks for pattern-detector v2 (structure.py + patterns.py).

Synthetic bars test LOGIC only - not evidence. The real-data replay (replay.py) is
the evidence: a synthetic fixture hid a live-data bug in this repo before (CLAUDE.md).

    python pattern-recorder/replay/checks.py      # exits 1 on any failure
"""
import datetime
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import structure as st  # noqa: E402
import patterns as pt   # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
FAILS = []


def t(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return int(datetime.datetime(2026, 11, day, h, m, tzinfo=IST).timestamp())


def mk(day, start, rows, vol=None):
    ts0 = t(day, start)
    out = []
    for k, (o, h, l, c) in enumerate(rows):
        assert l <= min(o, c) and h >= max(o, c), (start, k, o, h, l, c)
        out.append({"ts": ts0 + 300 * k, "open": o, "high": h, "low": l, "close": c,
                    "volume": (vol[k] if vol else 1000 + (k % 7) * 37)})
    return out


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def prev_session():
    rows = []
    for k in range(75):
        p = 23100 + (k % 5) * 3
        rows.append((p, p + 5, p - 5, p + 1))
    rows[10] = (23100, 23300, 23095, 23101)   # PDH 23300
    rows[40] = (23100, 23105, 22900, 23101)   # PDL 22900
    return mk(2, "09:15", rows)


PREV = prev_session()
ORB_A = [(23100, 23150, 23090, 23120), (23120, 23130, 23060, 23080), (23080, 23100, 23070, 23090)]


def find(events, **kw):
    return [e for e in events if all(e.get(k) == v for k, v in kw.items())]


# ---- 15-min bars and swings ---------------------------------------------------
b5 = mk(3, "09:15", ORB_A)
b15 = st.build_15min(b5)
check("build_15min: one bar from three", len(b15) == 1 and b15[0]["high"] == 23150
      and b15[0]["low"] == 23060 and b15[0]["close"] == 23090 and b15[0]["open"] == 23100, b15)
check("build_15min: incomplete bucket dropped", st.build_15min(b5[:2]) == [])

# ---- shapes -----------------------------------------------------------------------
ham = mk(3, "10:00", [(23070, 23074, 23058, 23072)])
check("shape hammer", [s["name"] for s in pt.shapes_at(ham, 0)] == ["hammer"], pt.shapes_at(ham, 0))
star = mk(3, "10:00", [(23072, 23090, 23068, 23070)])
check("shape shooting_star", "shooting_star" in [s["name"] for s in pt.shapes_at(star, 0)], pt.shapes_at(star, 0))
doji = mk(3, "10:00", [(23070, 23080, 23060, 23071)])
check("shape doji", "doji" in [s["name"] for s in pt.shapes_at(doji, 0)], pt.shapes_at(doji, 0))
tiny = mk(3, "10:00", [(23070, 23072, 23069, 23070)])
check("shape: tiny bar forms no single-bar shape", pt.shapes_at(tiny, 0) == [], pt.shapes_at(tiny, 0))
eng = mk(3, "10:00", [(23125, 23127, 23108, 23110), (23108, 23132, 23101, 23130)])
check("shape bullish_engulfing", "bullish_engulfing" in [s["name"] for s in pt.shapes_at(eng, 1)], pt.shapes_at(eng, 1))
ms = mk(3, "10:00", [(23130, 23132, 23100, 23102), (23102, 23106, 23095, 23100), (23100, 23125, 23098, 23122)])
check("shape morning_star", "morning_star" in [s["name"] for s in pt.shapes_at(ms, 2)], pt.shapes_at(ms, 2))
es = mk(3, "10:00", [(23100, 23132, 23098, 23130), (23130, 23136, 23126, 23131), (23131, 23133, 23105, 23108)])
check("shape evening_star", "evening_star" in [s["name"] for s in pt.shapes_at(es, 2)], pt.shapes_at(es, 2))

# ---- A: R1 hammer at ORL --------------------------------------------------------
sa = mk(3, "09:15", ORB_A + [(23090, 23095, 23075, 23080), (23080, 23085, 23068, 23072),
                             (23070, 23074, 23058, 23072)])
ev = pt.detect_session(sa, PREV, sa, PREV)
hit = find(ev, family="R1", direction="up", level="orl", clock="09:40")
check("A: R1 up at orl on the hammer", len(hit) == 1 and "hammer" in hit[0]["candles"], ev)
check("A: first test, no chart pattern", hit and hit[0]["prior_tests"] == 0 and hit[0]["chart_pattern"] is None, hit)

# ---- B: R2 wick at ORL ----------------------------------------------------------
sb = mk(3, "09:15", ORB_A + [(23090, 23095, 23075, 23080), (23080, 23085, 23040, 23075)])
ev = pt.detect_session(sb, PREV, sb, PREV)
hit = find(ev, family="R2", subtype="wick", direction="up", level="orl", clock="09:35")
check("B: R2 wick up at orl", len(hit) == 1 and hit[0]["bars_beyond"] == 0, ev)
check("B: not also an R1 (extreme beyond NEAR)", not find(ev, family="R1", clock="09:35", level="orl"), ev)

# ---- C: C1 down, then R2 close_beyond reclaim -------------------------------------
sc = mk(3, "09:15", ORB_A + [(23090, 23095, 23075, 23080), (23080, 23082, 23040, 23050),
                             (23050, 23058, 23035, 23055), (23055, 23085, 23050, 23080)])
ev = pt.detect_session(sc, PREV, sc, PREV)
check("C: C1 down at orl on the first close below", len(find(ev, family="C1", direction="down", level="orl", clock="09:35")) == 1, ev)
hit = find(ev, family="R2", subtype="close_beyond", direction="up", level="orl", clock="09:45")
check("C: R2 close_beyond up at orl, 2 bars beyond", len(hit) == 1 and hit[0]["bars_beyond"] == 2, ev)

# ---- D: C1 up, failed-break R2 down, C2 dip_reclaim up ------------------------------
sd = mk(3, "09:15", ORB_A + [(23140, 23160, 23135, 23158), (23158, 23165, 23140, 23145),
                             (23145, 23160, 23142, 23157)])
ev = pt.detect_session(sd, PREV, sd, PREV)
check("D: C1 up at orh on the 09:30 close (opening-range break)", len(find(ev, family="C1", direction="up", level="orh", clock="09:30")) == 1, ev)
check("D: 09:35 close back inside reads as a failed break (R2 down)", len(find(ev, family="R2", direction="down", level="orh", clock="09:35")) == 1, ev)
hit = find(ev, family="C2", subtype="dip_reclaim", direction="up", level="orh", clock="09:40")
check("D: C2 dip_reclaim up at orh", len(hit) == 1 and hit[0]["prior_break"]["direction"] == "up", ev)
check("D: retest volume ratio present", hit and hit[0]["retest_vol_ratio"] is not None, hit)

# ---- E: more than RECLAIM_MAX_BARS closes beyond -> a fresh C1, not a retest -----------
rows = ORB_A + [(23140, 23160, 23135, 23158)] + [(23148, 23152, 23138, 23145)] * 8 + [(23145, 23170, 23144, 23165)]
se = mk(3, "09:15", rows)
ev = pt.detect_session(se, PREV, se, PREV)
check("E: no event on the shallow 09:35 close back (depth <= NEAR)", not find(ev, clock="09:35", level="orh"), ev)
check("E: 10:15 re-cross after 8 bars below is C1, not C2",
      len(find(ev, family="C1", direction="up", level="orh", clock="10:15")) == 1
      and not find(ev, family="C2", clock="10:15"), find(ev, clock="10:15"))

# ---- F: double bottom at a 15-min swing low ---------------------------------------
orb_f = [(23130, 23200, 23060, 23150), (23150, 23180, 23140, 23160), (23160, 23170, 23130, 23140)]
body_f = [
    (23140, 23150, 23125, 23130), (23130, 23140, 23120, 23135), (23135, 23145, 23125, 23130),  # 09:30 l 23120
    (23130, 23135, 23115, 23120), (23120, 23128, 23110, 23118), (23118, 23130, 23115, 23125),  # 09:45 l 23110
    (23125, 23128, 23105, 23110), (23110, 23115, 23100, 23112), (23112, 23130, 23108, 23128),  # 10:00 l 23100
    (23132, 23145, 23130, 23140), (23140, 23150, 23135, 23145), (23145, 23155, 23138, 23150),  # 10:15 l 23130
    (23150, 23158, 23140, 23152), (23152, 23160, 23145, 23155), (23155, 23158, 23142, 23150),  # 10:30 l 23140
    (23150, 23152, 23130, 23135), (23135, 23138, 23120, 23125), (23125, 23128, 23115, 23126),  # 10:45 l 23115
    (23125, 23127, 23108, 23110), (23108, 23132, 23101, 23130),                                # 11:00, 11:05
]
sf = mk(3, "09:15", orb_f + body_f)
sw = st.find_swings(st.build_15min(sf))
lows = [s for s in sw if s["kind"] == "low"]
check("F: swing low 23100 at 10:00, known 10:45",
      len(lows) == 1 and lows[0]["price"] == 23100 and lows[0]["ts"] == t(3, "10:00") and lows[0]["known_at"] == t(3, "10:45"), sw)
ev = pt.detect_session(sf, PREV, sf, PREV)
hit = find(ev, family="R1", direction="up", level="swing_low", clock="11:05")
check("F: R1 up at swing_low on the engulfing, labelled double_bottom",
      len(hit) == 1 and "bullish_engulfing" in hit[0]["candles"] and hit[0]["prior_tests"] == 1
      and hit[0]["chart_pattern"] == "double_bottom", hit or ev)

# ---- B1: one retest per break ------------------------------------------------------------
sd_chop = mk(3, "09:15", ORB_A + [(23140, 23160, 23135, 23158), (23158, 23165, 23140, 23145),
                                  (23145, 23160, 23142, 23157), (23157, 23158, 23140, 23146),
                                  (23146, 23160, 23144, 23156)])
ev = pt.detect_session(sd_chop, PREV, sd_chop, PREV)
c2_up = find(ev, family="C2", direction="up", level="orh")
check("B1: chop around orh keeps ONE up-retest of the 09:30 break",
      [e["clock"] for e in c2_up] == ["09:40"], [(e["clock"], e["family"], e["direction"]) for e in ev])
check("B1: the down-retest opened by the 09:35 failed break is kept once",
      [e["clock"] for e in find(ev, family="C2", direction="down", level="orh")] == ["09:45"],
      [(e["clock"], e["family"], e["direction"]) for e in ev])

# ---- B2: plain rejection ------------------------------------------------------------------
sp = mk(3, "09:15", ORB_A + [(23090, 23095, 23075, 23080), (23080, 23085, 23068, 23072),
                             (23066, 23075, 23058, 23072)])
ev = pt.detect_session(sp, PREV, sp, PREV)
hit = find(ev, family="R1", subtype="plain", direction="up", level="orl", clock="09:40")
check("B2: plain R1 up at orl (touch, close in the upper half, no textbook shape)",
      len(hit) == 1 and hit[0]["candles"] == ["plain"], ev)
sp_low = mk(3, "09:15", ORB_A + [(23090, 23095, 23075, 23080), (23080, 23085, 23068, 23072),
                                 (23070, 23075, 23058, 23061)])
ev = pt.detect_session(sp_low, PREV, sp_low, PREV)
check("B2: no plain R1 when the close sits in the lower half",
      not find(ev, family="R1", clock="09:40"), find(ev, clock="09:40"))
ev = pt.detect_session(sa, PREV, sa, PREV)
check("B2: a textbook hammer is not also reported as plain",
      not find(ev, subtype="plain", clock="09:40"), find(ev, clock="09:40"))

# ---- G: freshness ---------------------------------------------------------------------
ev = pt.detect_session(sd, PREV, sd, PREV)
fresh = pt.fresh_events(ev, t(3, "09:36"), t(3, "09:46"))
check("G: fresh window (09:36, 09:46] announces 09:35 and 09:40 triggers only",
      sorted({e["clock"] for e in fresh}) == ["09:35", "09:40"], [(e["clock"], e["family"]) for e in fresh])

# ---- I: v2.1 - the key levels are the INDEX's, priced on the future with the bar's basis -----
# The future trades 50 above the index; the index's opening-range low (23000) sits 10
# points lower than the future's own would imply, so v2.1's orl on the future is
# 23000 + 50 = 23050, not the future's 23060. The 09:40 hammer (low 23058) is at it.
idx_sa = [{**b, "open": b["open"] - 50, "high": b["high"] - 50, "low": b["low"] - 50,
           "close": b["close"] - 50} for b in sa]
idx_sa[1]["low"] = 23000
idx_prev = [{**b, "open": b["open"] - 50, "high": b["high"] - 50, "low": b["low"] - 50,
             "close": b["close"] - 50} for b in PREV]
ev = pt.detect_session(sa, PREV, idx_sa, idx_prev)
hit = find(ev, family="R1", direction="up", level="orl", clock="09:40")
check("I: orl is the index's 23000 + basis 50 = 23050 on the future",
      len(hit) == 1 and hit[0]["level_price"] == 23050 and hit[0]["level_strike"] == 23000, hit)
check("I: no key levels at all without index bars", not pt.detect_session(sa, PREV), pt.detect_session(sa, PREV))

# ---- H: no look-ahead - truncated detection == full detection, every run ----------------
# v2.1 runs every 5 minutes, after the poller (~107 s past each boundary).
rng = random.Random(7)


def walk(day, n, p0, seed_vol):
    rows, vols, p = [], [], p0
    for k in range(n):
        o = p
        c = o + rng.gauss(0, 12)
        h = max(o, c) + abs(rng.gauss(0, 6))
        l = min(o, c) - abs(rng.gauss(0, 6))
        rows.append((round(o, 1), round(h, 1), round(l, 1), round(c, 1)))
        vols.append(int(seed_vol * (1 + abs(rng.gauss(0, 0.6)))))
        p = c
    return mk(day, "09:15", rows, vols)


mismatches = 0
runs = 0
for seed in range(6):
    rng.seed(seed)
    prev = walk(2, 75, 23100, 50000)
    sess = walk(3, 75, prev[-1]["close"], 50000)
    idx_prev = [{**b, "open": b["open"] - 60, "high": b["high"] - 60, "low": b["low"] - 60, "close": b["close"] - 60} for b in prev]
    idx = [{**b, "open": b["open"] - 55 + rng.gauss(0, 4), "close": b["close"] - 55 + rng.gauss(0, 4)} for b in sess]
    idx = [{**b, "high": max(b["high"] - 55, b["open"], b["close"]), "low": min(b["low"] - 55, b["open"], b["close"])} for b in idx]
    centre = round(sess[0]["close"] / 50) * 50
    fno = [{"snapshot_ts": t(3, "09:45") + 900 * k, "near_max_oi_call": centre + 100 + 50 * (k // 8),
            "near_max_oi_put": centre - 100, "near_max_pain": centre} for k in range(24)]
    full = pt.detect_session(sess, prev, idx, idx_prev, fno)
    for T in range(t(3, "09:35") + 107, t(3, "15:31"), 300):
        cut = [b for b in sess if b["ts"] + 300 <= T]
        cut_idx = [b for b in idx if b["ts"] + 300 <= T]
        part = pt.detect_session(cut, prev, cut_idx, idx_prev, [r for r in fno if r["snapshot_ts"] <= T])
        expect = [e for e in full if e["trigger_ts"] + 300 <= T]
        runs += 1
        if part != expect:
            mismatches += 1
            if mismatches <= 2:
                a = {(e["trigger_ts"], e["family"], e["level"]) for e in part}
                b = {(e["trigger_ts"], e["family"], e["level"]) for e in expect}
                print("   diff at", st.clock(T), "only-truncated:", sorted(a - b)[:3], "only-full:", sorted(b - a)[:3])
    print(f"   seed {seed}: {len(full)} events in the random session")
check(f"H: truncated == full on {runs} simulated runs (no look-ahead)", mismatches == 0, f"{mismatches} mismatching runs")

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
