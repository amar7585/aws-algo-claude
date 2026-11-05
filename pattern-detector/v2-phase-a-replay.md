# pattern-detector v2 — Phase A replay, and the Phase B re-run

← [v2 pattern definitions](v2-pattern-definitions.md) · [pattern-detector README](README.md) · [replay tooling](replay/README.md)

**Measured 2026-11-05** by replaying `structure.py` + `patterns.py` (uncommitted, not
wired in) over every stored session, 2026-07-01 → 11-04. The data was read-only from
Neon `nameless-mountain-15353651` / `Algo`. Nothing was deployed or written.
Reproduce it with [`replay/`](replay/README.md).

**Descriptive only.** There are no hit rates and no P&L: entries and exits are decided
per strategy, later. Forward moves are on the future, measured from the trigger bar's
close in the event's direction.

## Current state — after B1 + B2 (re-run 2026-11-05)

B1 (one retest per break) and B2 (plain rejection) are in the library. B6 moved the
scripts into `replay/`. B3 waits for Phase C; B4 and B5 changed nothing. The tables at
the end of this file are the current run.

| | Phase A | After B1 + B2 |
|---|---|---|
| Events per clean session | 32.7 (median 32, 13–69) | **24.2** (median 23, 12–46) |
| Retests (C2) per session | 19.1 | **6.9** |
| Most C2 at one level in one session | 17 | **3** |
| Plain rejections (R1 `plain`) per session | — | 3.8 |
| Recall against the 9 logged rows | 5 / 9 | **7 / 9** (both clean rows) |
| Live == replay (stored run clocks) | 816 / 816 | 816 / 816 |

- **Plain rejections look like any bar.** R1 `plain` moved 25.0 / 24.3 (move +1.8)
  against the no-pattern baseline's 23.6 / 24.9. **Textbook-shape rejections did slightly
  worse than any bar:** 20.0 / 25.9 (−4.1). Both descriptive; not grounds to drop shapes.
- **The two remaining misses are both on damaged September bars:**
  - **09-17 10:10 evening star:** v2 reads it as a failed break (R2) at `max_pain` at
    10:15, a different family, so it isn't counted.
  - **09-18 13:45:**
    - **The mapping was wrong.** 23300 had been crossed up after the 11:30 low, so a
      touch from above is a **retest (C2)**, the same reading as 09-17's
      `orb_breakout_fade_reclaim`, not the sweep (R2) the definitions doc said.
    - **v2 announces this setup at its first touch, 13:20**, as C2 at `put_wall`. Under
      B1 the 13:45 second touch of the same retest is dropped.
    - **A variant didn't recover it.** I tried re-opening B1 once price has left the
      level by 2–4× NEAR. It would not have caught 13:45 either: price never left the
      wall by more than ~2× NEAR between the two touches. So B1 stays as built.
- **The fair OI test holds after B1:** measured from when the snapshot is known,
  buildup agreeing at tick0 went with a move of **+7.1 / +8.1 / +8.8** (R1·R2 / C1 / C2),
  against **−6.1 / −6.8 / −7.3** when it disagreed. Far-side option OI unwinding at a
  break still went with worse follow-through (C1 −30.4, n=16; C2 −9.5, n=16).
- **Unannounced events (B3, for Phase C):** 35, all on days the first live run was late:
  - 09-15 to 09-18: first run 10:00;
  - 09-30: first run 12:00;
  - 10-08: first run 12:33.

---

## Phase A summary (before B1/B2 — the numbers the B-decisions were made on)

1. **It behaves the same live as in replay.**
   - On the 35 sessions with stored run clocks, detecting on bars closed by each run
     gave exactly the same events as full-session detection, on **816 of 816 runs**.
   - No event would have been announced twice.
   - **35 events would never have been announced.** All of them are on days the first
     live run was late: 10:00 before the 09-18 retime, and 12:00 on 09-30. The current
     one-interval fallback on a session's first run cannot reach the morning (B3).
2. **Recall: 5 of the 9 logged rows match the mapped family.**
   - **Both rows on clean bars match:**
     - 10-30 sweep → 10:40 R2 at `orl`, labelled triple_bottom.
     - 11-04 → 12:20 C1 up through `pdh`, one bar *before* the index's 12:25 reclaim.
   - **The 7 rows on damaged SEP-future bars:** 3 match (09-17 09:45, 09-18 11:30 with
     the double_bottom label, 09-21 11:50).
   - **Misses:**
     - 09-17 10:10, 11:10 and 12:35 sit on damaged bars.
     - **09-18 13:45 is a real gap.** The future touched the converted 23300 wall within
       1.3 pts, but no textbook candle formed, and R1 needs one (B2).
3. **Too many events: a median of 32 per clean session** (13–69).
   - Retests (C2) are 19.1 a session.
   - **624 of the 1,454 C2s are repeat retests of the same break**, from price chopping
     around a broken level (B1).
4. **Most families look like any bar.**
   - Any bar, read in either direction, moves a median 23.6 pts for and 24.9 against
     within 60 min.
   - R1 rejections (20.1 / 27.2 up, 20.0 / 25.0 down) are no better than that.
   - Double/triple-test labels are worse than first tests: 18.4 / 28.3 against 25.3 / 28.9.
   - Reversals at option walls / max pain are the worst: 27.0 / 43.5. Price runs through
     walls more than it turns at them.
5. **What stands out** (descriptive, small samples, nothing calibrated):
   - **Breaks on a volume spike:** C1 with vol z ≥ 2 moved 28.8 / 22.9 (move +5.0),
     against 24.0 / 29.8 (−2.5) without. **Volume matters for breaks.**
   - **One-bar wick sweeps:** R2 wick moved 51.1 / 35.0 up (n=21) and 54.6 / 22.5 down
     (n=12). The excursions are bigger and favourable ones dominate, but n is small and
     these come at more volatile moments.
   - **Volume does not separate reversals:** R1/R2 with z ≥ 2 moved 20.0 / 30.4, against
     22.5 / 27.8 without. That fits the quiet 10-30 and 11-04 lows.
6. **OI** (15-min OI on 35 sessions, 5-min on 12):
   - **Same-tick buildup "agrees" with a break 80–85% of the time, and that is mostly
     price restated.** The buildup label is built from price and OI direction over the
     same window. It is not independent confirmation.
   - **The one OI read that separates, measured fairly:** measured from when the
     snapshot is known, a reversal whose futures buildup agreed at tick0 moved
     42.7 / 35.0 (move +8.0). One that disagreed moved 32.0 / 48.8 (−6.1). About 95
     events each.
   - Measured from the trigger instead (+26.5 against −30), the split is circular: the
     tick1 label is the next 15–30 min of price.
   - **Far-side option OI unwinding at a break went with worse follow-through:** C1
     −30.4 (n=16), C2 −13.6 (n=42).
   - **5-min:** breaks are mostly positions closing rather than opening. Up-breaks were
     covering 24 vs fresh longs 18; down-breaks were unwinding 29 vs fresh shorts 21.

**Caveats.**
- Events overlap in time, so one move can be counted several times.
- The effective sample is sessions (76 clean), not events.
- MFE/MAE scale with volatility. The OI cuts cover only Sep–Nov, a more volatile stretch,
  so compare them within themselves, not against the all-session baseline.
- **No threshold should be calibrated on this.**

## Decisions for Phase B — ACCEPTED 2026-11-05 (Amar)

| # | Decision | Recommendation |
|---|---|---|
| B1 | Keep only the **first retest after each break** (C2)? | **Yes.** It cuts C2 from 19.1 to 10.9 a session. Repeats behave like first retests (22.7 / 24.0 against 22.8 / 24.8), so nothing measured is lost. |
| B2 | The 09-18 13:45 miss: a touch-and-reject with no textbook candle | **Add it as a separate R1 subtype `plain`** (extreme within NEAR, closing back in the event's direction in the favourable half of its bar), measured alongside the textbook shapes rather than widening R1 silently. Alternative: leave it and log more examples first. |
| B3 | Late first run of a session | **Phase C:** the first run of a session announces from the session open, not one interval back. |
| B4 | Option walls / max pain | **Keep recording them** (V1). The data says price breaks them more than it turns at them, so they are not candidates for alerts. |
| B5 | Alerts | **None yet.** The reads that separate so far are a break on a volume spike, a reversal with buildup agreeing at tick0, and R2 wick (small n). Revisit after more sessions. |
| B6 | The replay scripts live only in this session's scratch folder | **Move them into the repo**, e.g. `pattern-detector/replay/`, kept out of the Lambda zip, so they survive the session. This changes V10. |
| B7 | V12, still open | **11-04:** v2 reads 12:20 as C1 up through PDH, with the future crossing a bar before the index. **09-17 10:10 evening star:** the nearest v2 read is 10:15 R2 down at `max_pain`, but on damaged bars, so inconclusive. |

---

*Current generated tables (after B1 + B2) follow, unedited — reproduce with `python pattern-detector/replay/replay.py`.*

Generated 2026-11-05 10:03 local. Library: `pattern-detector/structure.py` + `patterns.py` (uncommitted). NEAR 0.05%, MIN_RANGE 0.03%, RECLAIM_MAX_BARS 6, SWING_K 2.

**Descriptive only** - no hit rates, no P&L. Forward moves are on the future, from the trigger bar's close.

## 1. Coverage

- Sessions replayed: **88** (2026-07-01 -> 2026-11-04); clean **76**, damaged SEP-future **12** (2026-09-11 -> 2026-09-29).
- With stored 15-min OI rows (option walls, run clocks): 35; with 5-min OI: 12.
- Events: **2214** total; on clean sessions 1841 = **24.2 per session**.

Events per clean session: min 12, median 23.0, max 46.

## 2. Consistency (does live == replay?)

- Simulated runs: 2141. Events never announced by any run: **35**; announced twice: **0**.
- Real-data no-look-ahead check (sessions with stored run clocks): detection on bars closed by each run's `captured_at` vs full-session detection, fresh events compared: **816/816 runs identical**.

## 3. Events per session, by family (clean sessions)

| Family | Events | Per session | With vol z >= 2 | Median vol_z_max | Median rvol_tod |
|---|---|---|---|---|---|
| C1 down | 224 | 2.95 | 51 | 0.4 | 1.9 |
| C1 up | 209 | 2.75 | 51 | 0.5 | 2.3 |
| C2/dip_reclaim down | 86 | 1.13 | 8 | 0.3 | 1.2 |
| C2/dip_reclaim up | 80 | 1.05 | 7 | 0.5 | 1.7 |
| C2/touch_hold down | 159 | 2.09 | 22 | 0.1 | 1.8 |
| C2/touch_hold up | 161 | 2.12 | 20 | 0.2 | 1.6 |
| C2/wick_reclaim down | 19 | 0.25 | 3 | -0.1 | 1.8 |
| C2/wick_reclaim up | 18 | 0.24 | 2 | -0.4 | 1.5 |
| R1 down | 214 | 2.82 | 23 | 0.1 | 1.5 |
| R1 up | 231 | 3.04 | 25 | 0.1 | 1.4 |
| R1/plain down | 124 | 1.63 | 10 | -0.3 | 1.4 |
| R1/plain up | 162 | 2.13 | 16 | -0.1 | 1.6 |
| R2/close_beyond down | 55 | 0.72 | 3 | 1.6 | 1.4 |
| R2/close_beyond up | 66 | 0.87 | 3 | 0.9 | 1.3 |
| R2/wick down | 12 | 0.16 | 6 | 2.6 | 3.8 |
| R2/wick up | 21 | 0.28 | 5 | 0.5 | 2.2 |

### By primary level (clean sessions)

| Level | R1 | R2 | C1 | C2 |
|---|---|---|---|---|
| call_wall | 26 | 7 | 18 | 15 |
| max_pain | 57 | 16 | 38 | 37 |
| orh | 74 | 26 | 91 | 104 |
| orl | 86 | 25 | 62 | 78 |
| pdh | 23 | 6 | 37 | 49 |
| pdl | 13 | 9 | 37 | 48 |
| put_wall | 42 | 15 | 19 | 25 |
| swing_high | 182 | 24 | 63 | 85 |
| swing_low | 228 | 26 | 68 | 82 |

### R1 candles and chart-pattern labels (clean sessions)

- R1 candles: plain 286, doji 136, bearish_engulfing 89, bullish_engulfing 89, hammer 73, shooting_star 62, morning_star 29, evening_star 22
- Chart-pattern labels on R1/R2: double_bottom 190, double_top 159, triple_bottom 59, triple_top 26, multi_test_bottom 12

### By time of day (clean sessions, events per session)

| Window | R1 | R2 | C1 | C2 |
|---|---|---|---|---|
| 09:30-10:25 | 1.82 | 0.63 | 1.36 | 1.67 |
| 10:30-11:55 | 1.79 | 0.38 | 1.16 | 1.49 |
| 12:00-13:25 | 2.34 | 0.51 | 1.49 | 1.83 |
| 13:30-14:55 | 2.64 | 0.43 | 1.30 | 1.49 |
| 15:00-15:25 | 1.03 | 0.07 | 0.39 | 0.41 |

## 4. Recall against the hand-logged rows

Window: trigger within 15 min of the logged `event_ts`. *Exact* = the mapped family (and level, where the mapping names one), same direction.

| Row | pattern_type | dir | damaged | exact match | other same-direction events within 15 min |
|---|---|---|---|---|---|
| 09-17 09:45 | orb_breakout_fade_reclaim | up | yes | 09:50 C2/dip_reclaim up @orh (+7) | 09:30 C1 up @orh |
| 09-17 10:10 | evening_star | down | yes | **none** | 10:15 R2/close_beyond down @max_pain; 10:20 C2/touch_hold down @pdh; 10:20 C2/wick_reclaim down @max_pain |
| 09-17 11:10 | double_bottom | up | yes | 11:10 R1/plain up @put_wall (+4) plain | - |
| 09-17 12:35 | double_top | down | yes | 12:45 R1/plain down @max_pain (+3) plain | - |
| 09-18 11:30 | double_bottom | up | yes | 11:15 R1/plain up @orl (-5) plain double_bottom; 11:20 R1 up @orl (-5) hammer double_bottom | 11:40 C2/touch_hold up @orl |
| 09-18 13:45 | breakout_fade_reclaim | up | yes | **none** | - |
| 09-21 11:50 | orb_breakout_retest | up | yes | 11:50 C1 up @pdh (+4) bullish_engulfing | 11:40 C2/dip_reclaim up @call_wall |
| 10-30 10:35 | orb_liquidity_sweep | up | no | 10:40 R2/close_beyond up @orl (+25)  triple_bottom | 10:50 C2/dip_reclaim up @orl |
| 11-04 12:25 | pd_breakout_retest | up | no | 12:20 C1 up @pdh (+0); 12:20 C2/dip_reclaim up @swing_low (+10) | 12:15 R2/wick up @max_pain; 12:20 R1/plain up @max_pain; 12:25 R1/plain up @max_pain |

## 5. OI at read time

15-min rows (35 sessions). tick0 = the snapshot whose window holds the trigger bar's close, tick1 = the next. Option unwind = the far-side option OI change <= -3.0% (PE for up, CE for down).

| Family | n | buildup agrees tick0 | tick1 | option unwind tick0 | tick0 or 1 | PCR moves its way tick0 | fut OI falling tick0 |
|---|---|---|---|---|---|---|---|
| C1 down | 124 | 85% (n=123) | 51% (n=116) | 10% (n=121) | 16% (n=121) | 75% (n=114) | 51% (n=123) |
| C1 up | 105 | 80% (n=105) | 50% (n=103) | 14% (n=104) | 23% (n=104) | 76% (n=96) | 65% (n=105) |
| C2/dip_reclaim down | 36 | 81% (n=36) | 33% (n=36) | 11% (n=36) | 19% (n=36) | 44% (n=36) | 28% (n=36) |
| C2/dip_reclaim up | 39 | 69% (n=39) | 59% (n=39) | 10% (n=39) | 21% (n=39) | 63% (n=38) | 49% (n=39) |
| C2/touch_hold down | 85 | 72% (n=85) | 49% (n=83) | 7% (n=85) | 16% (n=85) | 68% (n=82) | 52% (n=85) |
| C2/touch_hold up | 87 | 72% (n=87) | 56% (n=85) | 12% (n=85) | 19% (n=85) | 65% (n=82) | 61% (n=87) |
| C2/wick_reclaim down | 16 | 75% (n=16) | 67% (n=15) | 6% (n=16) | 12% (n=16) | 69% (n=13) | 81% (n=16) |
| C2/wick_reclaim up | 14 | 71% (n=14) | 54% (n=13) | 21% (n=14) | 29% (n=14) | 54% (n=13) | 50% (n=14) |
| R1 down | 117 | 49% (n=117) | 46% (n=114) | 16% (n=116) | 31% (n=116) | 28% (n=112) | 59% (n=117) |
| R1 up | 105 | 45% (n=105) | 53% (n=96) | 21% (n=103) | 31% (n=103) | 37% (n=102) | 49% (n=105) |
| R1/plain down | 84 | 50% (n=84) | 56% (n=82) | 12% (n=84) | 18% (n=84) | 21% (n=75) | 56% (n=84) |
| R1/plain up | 78 | 51% (n=77) | 44% (n=73) | 20% (n=76) | 32% (n=76) | 41% (n=74) | 49% (n=77) |
| R2/close_beyond down | 32 | 59% (n=32) | 53% (n=32) | 12% (n=32) | 22% (n=32) | 20% (n=30) | 62% (n=32) |
| R2/close_beyond up | 49 | 55% (n=49) | 41% (n=49) | 13% (n=47) | 23% (n=47) | 24% (n=46) | 47% (n=49) |
| R2/wick down | 8 | 25% (n=8) | 62% (n=8) | 50% (n=8) | 50% (n=8) | 29% (n=7) | 38% (n=8) |
| R2/wick up | 21 | 35% (n=20) | 48% (n=21) | 50% (n=20) | 60% (n=20) | 15% (n=20) | 40% (n=20) |

5-min futures OI at the trigger bar (12 sessions): price/OI quadrant of the trigger bar, and the sign of the OI change over the next three bars.

| Family | n | long_buildup | short_covering | short_buildup | long_unwinding | next-3 OI falling |
|---|---|---|---|---|---|---|
| C1 down | 50 | 0 | 0 | 21 | 29 | 67% (n=49) |
| C1 up | 42 | 18 | 24 | 0 | 0 | 67% (n=39) |
| C2/dip_reclaim down | 11 | 0 | 0 | 5 | 6 | 73% (n=11) |
| C2/dip_reclaim up | 17 | 8 | 9 | 0 | 0 | 65% (n=17) |
| C2/touch_hold down | 31 | 2 | 3 | 10 | 16 | 58% (n=31) |
| C2/touch_hold up | 26 | 8 | 12 | 4 | 2 | 77% (n=26) |
| C2/wick_reclaim down | 10 | 2 | 1 | 4 | 3 | 67% (n=9) |
| C2/wick_reclaim up | 10 | 2 | 5 | 2 | 1 | 60% (n=10) |
| R1 down | 28 | 4 | 5 | 6 | 13 | 48% (n=27) |
| R1 up | 29 | 9 | 13 | 3 | 4 | 58% (n=26) |
| R1/plain down | 16 | 0 | 0 | 6 | 10 | 56% (n=16) |
| R1/plain up | 24 | 7 | 14 | 0 | 3 | 57% (n=21) |
| R2/close_beyond down | 13 | 0 | 0 | 8 | 5 | 54% (n=13) |
| R2/close_beyond up | 18 | 10 | 6 | 1 | 1 | 56% (n=18) |
| R2/wick down | 3 | 0 | 0 | 1 | 2 | 33% (n=3) |
| R2/wick up | 10 | 3 | 2 | 2 | 3 | 50% (n=10) |

## 6. What the future did next (clean sessions, descriptive)

Points from the trigger close, in the event's direction. MFE = best move in its favour, MAE = worst against, move = where it stood at the horizon. Medians.

| Family | n | 15m MFE / MAE | 30m MFE / MAE | 60m MFE / MAE | 60m move | to close move |
|---|---|---|---|---|---|---|
| C1 down | 224 | 13.0 / 17.1 | 18.5 / 21.2 | 27.2 / 28.9 | -2.0 | -1.7 |
| C1 up | 209 | 11.7 / 15.3 | 16.7 / 20.8 | 21.5 / 27.2 | -0.4 | 2.4 |
| C2/dip_reclaim down | 86 | 12.4 / 13.7 | 16.4 / 20.7 | 20.0 / 27.1 | -8.5 | -10.2 |
| C2/dip_reclaim up | 80 | 13.3 / 16.5 | 17.1 / 20.0 | 24.9 / 21.4 | 3.4 | 6.1 |
| C2/touch_hold down | 159 | 11.5 / 15.0 | 16.7 / 20.0 | 25.0 / 25.2 | 2.6 | -2.5 |
| C2/touch_hold up | 161 | 11.0 / 14.6 | 15.8 / 18.4 | 23.4 / 25.0 | 0.0 | -2.3 |
| C2/wick_reclaim down | 19 | 15.9 / 23.6 | 30.0 / 29.0 | 33.2 / 46.5 | -16.6 | 11.2 |
| C2/wick_reclaim up | 18 | 25.2 / 16.8 | 26.0 / 34.0 | 43.1 / 37.6 | -16.6 | 28.8 |
| R1 down | 214 | 10.4 / 12.4 | 15.3 / 15.7 | 20.0 / 25.0 | -4.7 | -1.9 |
| R1 up | 231 | 10.0 / 14.9 | 14.4 / 19.8 | 20.1 / 27.2 | -3.8 | -0.9 |
| R1/plain down | 124 | 9.5 / 15.9 | 15.0 / 20.1 | 27.7 / 29.0 | -3.2 | -1.4 |
| R1/plain up | 162 | 11.5 / 12.8 | 19.2 / 17.4 | 24.0 / 21.6 | 2.0 | -1.3 |
| R2/close_beyond down | 55 | 14.5 / 15.0 | 17.7 / 20.8 | 25.4 / 28.2 | 1.1 | -7.9 |
| R2/close_beyond up | 66 | 13.7 / 18.8 | 17.8 / 25.9 | 23.4 / 37.0 | -9.2 | -9.0 |
| R2/wick down | 12 | 15.8 / 12.5 | 22.8 / 25.0 | 54.6 / 22.5 | 18.4 | -14.9 |
| R2/wick up | 21 | 23.6 / 19.8 | 36.1 / 22.2 | 51.1 / 35.0 | 9.7 | 27.9 |

Same, split by time of day for the two reversal families (60m MFE / MAE, n):

| Window | R1 up | R1 down | R2 up | R2 down |
|---|---|---|---|---|
| 09:30-10:25 | 34.3 / 26.5 (74) | 23.8 / 33.0 (64) | 41.9 / 44.9 (25) | 37.1 / 33.6 (23) |
| 10:30-11:55 | 21.0 / 25.4 (67) | 16.9 / 26.9 (69) | 25.9 / 35.0 (20) | 40.3 / 13.2 (9) |
| 12:00-13:25 | 15.3 / 26.3 (99) | 21.0 / 24.8 (79) | 18.0 / 26.6 (22) | 24.0 / 21.6 (17) |
| 13:30-14:55 | 21.8 / 21.0 (83) | 21.5 / 22.1 (64) | 28.0 / 40.0 (15) | 10.5 / 64.0 (9) |
| 15:00-15:25 | - | - | - | - |

## 7. Behaviours worth a decision

- **R2 then opposite C2 at the same level within 30 min** (a failed-break read that turned into a retest): 50 of 154 R2 events.
- **Repeated C2 at one level in one session**: 87 level-sessions had 2+ C2 events (max 3).
- **Busiest levels**: swing_low 5.3/session, swing_high 4.7/session, orh 3.9/session, orl 3.3/session.
- **15:00 or later**: 144 events (8%).

## 8. Follow-up cuts

**Events no run would announce: 35** - 2026-09-15 before the first run's window (first run 10:00) x2, 2026-09-16 before the first run's window (first run 10:00) x3, 2026-09-17 before the first run's window (first run 10:00) x3, 2026-09-18 before the first run's window (first run 10:00) x3, 2026-09-30 before the first run's window (first run 12:00) x13, 2026-10-08 before the first run's window (first run 12:33) x11

**No-pattern baseline** - every clean-session bar 09:30-14:55, read in each direction. Then splits of the events (clean sessions). 60m for / against, and the move at 60m, medians, pts.

| Cut | n | 60m for / against | 60m move |
|---|---|---|---|
| BASELINE any bar, up | 4560 | 23.6 / 24.9 | -0.4 |
| BASELINE any bar, down | 4560 | 24.9 / 23.6 | 0.4 |
| R1 textbook shape (not plain) | 367 | 20.0 / 25.9 | -4.1 |
| R1 plain (B2) | 232 | 25.0 / 24.3 | 1.8 |
| R1 doji only | 73 | 25.0 / 27.6 | 5.6 |
| R1/R2 first test | 372 | 25.5 / 26.9 | 0.4 |
| R1/R2 double / triple / multi | 367 | 18.8 / 25.9 | -4.3 |
| R1/R2 vol_z_max >= 2 | 97 | 20.6 / 30.4 | -4.4 |
| R1/R2 vol_z_max < 2 | 642 | 23.2 / 26.0 | -1.6 |
| R1/R2 at pdh/pdl/orh/orl | 260 | 25.2 / 26.7 | 0.4 |
| R1/R2 at 15-min swings | 334 | 18.7 / 22.1 | -1.4 |
| R1/R2 at walls / max pain | 145 | 29.8 / 40.3 | -11.8 |
| R2 wick | 28 | 51.1 / 31.0 | 18.4 |
| C1 vol_z >= 2 | 81 | 28.8 / 22.9 | 5.0 |
| C1 vol_z < 2 | 290 | 24.0 / 29.8 | -2.5 |
| C2 (first retest only, B1) | 447 | 23.9 / 25.5 | -1.0 |

**OI, measured from when the confirmation is KNOWN** (clean sessions with 15-min OI). Measuring from the trigger instead is circular: a later snapshot's buildup is built from the very price move being measured.

| Cut | n | 60m for / against | 60m move |
|---|---|---|---|
| R1/R2 buildup agrees at tick0 (from tick0) | 132 | 42.7 / 37.4 | 7.1 |
| R1/R2 buildup disagrees at tick0 (from tick0) | 130 | 34.5 / 45.0 | -6.1 |
| R1/R2 buildup agrees at tick1 (from tick1) | 113 | 41.9 / 35.0 | 7.1 |
| R1/R2 buildup disagrees at tick1 (from tick1) | 140 | 34.6 / 40.0 | -7.5 |
| R1/R2 option unwind at tick0 (from tick0) | 41 | 32.4 / 43.5 | 4.9 |
| R1/R2 no option unwind at tick0 (from tick0) | 216 | 37.0 / 42.9 | -1.1 |
| C1 buildup agrees at tick0 (from tick0) | 121 | 39.8 / 35.0 | 8.1 |
| C1 buildup disagrees at tick0 (from tick0) | 31 | 39.0 / 29.5 | -6.8 |
| C1 buildup agrees at tick1 (from tick1) | 81 | 43.5 / 38.0 | -4.1 |
| C1 buildup disagrees at tick1 (from tick1) | 66 | 36.2 / 42.9 | -3.1 |
| C1 option unwind at tick0 (from tick0) | 16 | 23.0 / 61.4 | -30.4 |
| C1 no option unwind at tick0 (from tick0) | 133 | 45.0 / 31.1 | 9.9 |
| C2 buildup agrees at tick0 (from tick0) | 133 | 42.0 / 34.5 | 8.8 |
| C2 buildup disagrees at tick0 (from tick0) | 49 | 30.9 / 44.0 | -7.3 |
| C2 buildup agrees at tick1 (from tick1) | 92 | 37.6 / 32.0 | 2.9 |
| C2 buildup disagrees at tick1 (from tick1) | 81 | 34.3 / 41.9 | -6.8 |
| C2 option unwind at tick0 (from tick0) | 16 | 30.7 / 41.7 | -9.5 |
| C2 no option unwind at tick0 (from tick0) | 165 | 41.4 / 37.1 | 7.8 |

