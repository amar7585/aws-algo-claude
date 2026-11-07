# pattern-detector v2 — pattern definitions

← [pattern-recorder README](README.md) · [Session-observation runbook](../docs/session-observation-runbook.md)

**Status: DEFINITIONS ACCEPTED 2026-11-05 — V1–V11 and B1–B6 accepted (Amar). V12
(the 11-04 question) is still open.** The library implements them (`structure.py`,
`patterns.py`, `RULES_VERSION = "v2.0"`). Phase C, running them live through the
`pattern-recorder` function, is **live since 2026-11-06 09:45**.

It lived in `pattern-detector/` while it was offline-only. Phase C (C1 = B) moved it,
with the library and `replay/`, into `pattern-recorder/`.

**Scope is recognition only.** It describes patterns and what was true when they formed.
Entries, exits, targets, stops, P&L and hit rates are decided later, per strategy.

---

## 0. Agreed so far

| | Decision | Who / when |
|---|---|---|
| A1 | Patterns first; exits are structure-based and decided per strategy later | Amar, 2026-10-29 |
| A2 | **One series: the active future** (`fut_security_id`), for candle shape, levels, volume and OI | Amar, 2026-11-05 |
| A3 | Levels: PDH/PDL, 15-min opening range high/low, swing high/low | Amar, 2026-11-05 |
| A4 | **Timeframe split: 15-min decides *where*** (levels, swings, structure), **a 5-min candle decides *when*** (the trigger) | Amar, 2026-11-05 |

## 1. Measured basis (Neon `nameless-mountain-15353651` / `Algo`, measured 2026-11-04/05)

| Finding | Number | Consequence |
|---|---|---|
| Index vs future 5-min candles, same colour | 80% of 6,196 bars | the shape depends on which series you read |
| Future bull engulfing that is also one on the index | 75 / 534 (14%); hammer 30%, doji 18% | read every shape on the future only (A2) |
| Basis (future − index), intraday swing | 35–54 pts a session (0.15–0.24%) | index levels cannot be laid on future candles unconverted |
| Basis, bar-to-bar step | sd 7.0, p90 11.5 pts | about the whole "near" tolerance below |
| 5-min future bar range | p10 8.0 · p25 12.0 · median 17.3 (0.073%) · p90 34.5 pts | sets `NEAR_PCT` and `MIN_RANGE_PCT` |
| 15-min future bar range | median 29.4 pts | |
| `candle_15min` future vs 5-min aggregated in threes | 300/300 exact since 10-22; 88% before | build 15-min bars from 5-min ones (one source) |
| Textbook candles on the future, per session | ~34, about half of all bars; ~1.7 with volume z ≥ 2 | a candle alone is not a signal |
| Candles near a level (0.05%), per session | ORB+PD 5.7 · +15-min swings 9.0 · +5-min swings 14.7 | swings come from 15-min bars, not 5-min |
| …the same, also with volume z ≥ 2 | 0.23 · 0.55 · 0.65 | requiring a spike leaves about one event every 2–4 sessions |
| History depth | future 5-min 88 sessions · 15-min OI 35 · 5-min OI 12 · logged rows 9 | 5-min OI evidence is thin |
| Detector vs poller write time after each quarter-hour | classifier +70 s (p90 78), poller's newest tick +112 s, poller first 0/230 | the trigger bar's 5-min OI is never there at detection |

---

## 2. Inputs

- **5-min bars:** the active future's closed bars from `algo.candle_5min`, queried on the
  full identity `(fut_security_id, 'FUTIDX')`. A bar is closed when
  `candle_ts + 300 <= captured_at`, the rule the detector already uses.
- **15-min bars:** **built in the detector from the 5-min bars**, three at a time, on the
  grid 09:15, 09:30 … 15:15 (`bucket = candle_ts - candle_ts % 900`). A 15-min bar exists
  only when all three 5-min bars are present, and it is closed when its last one is.
  This gives the trigger and the structure one source, and a replay uses the same bars
  that live used.
- **Index 5-min bars:** used only to convert strike levels (L4), and to record index
  levels for comparison. They are never read for shape.
- **Notation, for bar `b`:** `o h l c v`; `body = |c − o|`; `range = h − l`;
  `uw = h − max(o, c)` (upper wick); `lw = min(o, c) − l` (lower wick); bullish `c > o`,
  bearish `c < o`. Percentages are of the trigger bar's close.

## 3. Levels — *where* (all on the future's own prices)

| Id | Level | Definition | Available |
|---|---|---|---|
| L1 | `pdh` / `pdl` | max high / min low of the **same contract's** 5-min bars on the previous trading session | all day; absent if the contract has no bars that day |
| L2 | `orh` / `orl` | high / low of the future's 09:15, 09:20 and 09:25 bars (the 09:15 15-min bar) | from 09:30 |
| L3 | `swing_high` / `swing_low` | k=2 fractal on **this session's 15-min bars**: bar `t` is a swing high if `h[t] > h[t−1]`, `h[t] > h[t−2]`, `h[t] ≥ h[t+1]`, `h[t] ≥ h[t+2]` (the mirror for lows). It is **known only once bar `t+2` closes**, so there is no look-ahead. v1 uses the **latest** confirmed swing high and swing low. | from about 10:30 (the first bar that can confirm is 09:45, known at 10:30) |
| L4 *(needs your OK, V1)* | `call_wall` / `put_wall` / `max_pain` | `near_max_oi_call`, `near_max_oi_put`, `near_max_pain` from the latest `intraday_fno_data` row at or before the bar. These are **index strikes, converted to the future** with that bar's own basis, `strike + (c_future − c_index)`. From 15:15 on, the index is frozen by the closing auction, so the 15:10 basis is used instead. | from 09:45 |

- **Recorded only, not used to detect:** the distance to the **index** PDH/PDL and opening
  range, converted the same way. This lets the data say later whether index or future
  levels matter more.
- **Known gap:** futures trade until 15:40, but nothing stores bars after the 15:25 bar,
  so PDH/PDL leave out the 15:30–15:40 trades.
- **Assumption to check in the replay:** on a roll day the new contract already has the
  previous session's bars. The loader starts it about two sessions early, as on 09-28 and
  10-26.

**Role.** For bar `b`, a level is **support** if the previous bar closed above it, and
**resistance** if it closed below. A broken PDH therefore becomes support, which is what a
breakout-retest needs. Max pain takes its role by the same rule.

**Near.** A price is near a level when it is within `NEAR_PCT` of it.
- The **primary** level of an event is the nearest qualifying level on the relevant side.
- Every other level within `NEAR_PCT` is recorded in `other_levels`, for example ORL and
  PDL stacked 5 points apart. This gives one event, not two, and needs no extra
  merge setting.

## 4. Candle shapes — the 5-min trigger vocabulary

Single-bar shapes need `range ≥ MIN_RANGE_PCT`, because shape is meaningless on a
5-point bar. All ratios are textbook values and provisional. The single-bar ratios are
the ones measured in section 1.

| Shape | Direction | Bars | Definition |
|---|---|---|---|
| `hammer` | up | 1 | `lw ≥ 2·body`, `uw ≤ 0.3·range`, `body > 0` (either colour) |
| `shooting_star` | down | 1 | `uw ≥ 2·body`, `lw ≤ 0.3·range`, `body > 0` |
| `doji` *(V3)* | away from the level | 1 | `body ≤ 0.1·range` |
| `bullish_engulfing` | up | 2 | `b−1` bearish, `b` bullish, `o[b] ≤ c[b−1]`, `c[b] ≥ o[b−1]` |
| `bearish_engulfing` | down | 2 | mirror |
| `morning_star` | up | 3 | `b−2` bearish with `body ≥ 0.5·range`; `b−1` with `body ≤ 0.3·body[b−2]`; `b` bullish closing above the midpoint of `b−2`'s body. No gap is required, because intraday bars have none. |
| `evening_star` | down | 3 | mirror |
| `tweezer_bottom` / `tweezer_top` *(V3)* | up / down | 2 | `|l[b−1] − l[b]| ≤ TWEEZER_PCT`, with `b−1` bearish and `b` bullish (the mirror for tops) |

- A pattern **completes on its last bar `b`**, the trigger.
- Its **extreme** is the lowest low (for up) or highest high (for down) across its bars.
- When one bar matches several shapes (hammer + doji, say), the event lists **all** of them
  in `candles`. That avoids a precedence rule.

## 5. Pattern families = level × trigger

Five families. Each is a 5-min trigger at a level and a direction, and the 15-min context
in section 6 is attached to it.

**R1 — Rejection candle at a level (reversal).**
- An up shape completes on `b`, with its extreme within `NEAR_PCT` of a **support**
  (either side of it), and `c[b]` above that support.
- Down at resistance is the mirror.
- ***Subtype `plain`* (B2, added 2026-11-05):** bar `b` alone, with no textbook shape
  qualifying at `b` in that direction.
  - Its extreme is within `NEAR_PCT` of the level, and it closes back across it.
  - The close sits in the favourable `PLAIN_CLOSE_FRACTION` (0.5) of its own range: the
    upper half for an up rejection.
  - `range ≥ MIN_RANGE_PCT` applies, as for any single-bar shape.
  - At a level crossed that way earlier this session it is a C2 touch_hold, with
    `candles = ["plain"]`.
  - It is its own subtype, so its numbers can be compared with the textbook shapes.

**R2 — Sweep and reclaim (failed break, reversal).**
- **Pierce:** some bar `k` trades below support `S` by more than `NEAR_PCT` (a wick, or a
  close below).
- **Reclaim:** the first bar `b ≥ k` that **closes back above `S`**, provided price has not
  closed below `S` for more than `RECLAIM_MAX_BARS` bars in a row. Longer than that, it is
  a breakdown (C1), not a sweep.
- **Trigger** = the reclaim bar's close. A one-bar wick sweep has `b = k`.
- Records the depth of the sweep and the number of bars spent below.
- Resistance is the mirror.

**C1 — Break (continuation candidate).**
- `b` closes beyond a level in the break direction.
- `b−1` closed on the near side, so it is a crossing that fires once.
- `b`'s body runs in the break direction.
- Records `body/range` as the break's strength.
- This is today's `level_break`, moved to the future and to all levels.

**C2 — Break, retest, reclaim (continuation).** After a C1 at level `X` this session:
- **(i) dip-reclaim:** price closes back on the near side of `X` and a later bar closes
  across it again. This is today's `retest_entry`.
- **(ii) touch-and-hold:** an R1 candle forms at `X`, now in its flipped role, without
  closing back through.
- The trigger is the reclaim bar or the R1 bar.
- This is how an R2 or R1 at a level that was **broken earlier this session** gets labelled.
  The geometry is the same; the history differs.
- ***One retest per break* (B1, added 2026-11-05).**
  - The first C2 at a level in a direction is kept. Further C2s there, that way, are
    dropped until the level is broken (C1) or swept and reclaimed (R2) in that direction
    again.
  - Keying on "the last crossing" would not work: every reclaim in a chop is itself a
    crossing.
  - Measured before B1: 624 of 1,454 C2s were repeat retests, and they behaved like the
    first ones.
  - Consequence: 09-18's wall retest is announced at its first touch (13:20), not the
    13:45 second touch you logged.

**D — Double / triple test.**
- An R1 or R2 at a level **already tested earlier this session** is labelled
  `double_bottom` or `double_top` on the second test, and `triple_*` on the third.
- A test is a distinct 15-min bar whose extreme came within `NEAR_PCT` of the level,
  separated from the previous test by at least one 15-min bar that did not touch it.
  *Refined while building, 2026-11-05:*
  - A bar that swept past the level and still closed back on the right side is also a
    test; a bar that merely traded through it is not.
  - Only bars from the level's origin count: a swing's own bar, 09:15 for the opening
    range and PDH/PDL, and the first snapshot carrying a wall's strike. Without this,
    the 09:15 bar trading through a later swing low would read as its first bottom.
- The neckline is the swing between the tests. A later C1 through it is just a C1 at
  `swing_high`/`swing_low`, so no extra rule is needed.

## 6. 15-min structure — context attached to every event (recorded, never a gate)

- `test_count`: how many times the primary level was tested before this event (section 5D).
- `structure_15m`: built from the last two confirmed 15-min swings: HH+HL = `up`,
  LH+LL = `down`, anything else `mixed`, too few swings `none`. This is our own read on the
  future. The classifier's `structure` is on other bars and is not reused.
- `prior_break`: whether the primary level was closed through earlier this session, and
  in which direction (feeds C2).
- `clock`: the trigger bar's IST time, so opening and late-session events can be split
  out later rather than dropped now.

## 7. Validation — volume and OI (recorded, not required)

The logged sessions show why nothing here is a requirement yet:
- The 10-30 sweep (z 0.42) and the 11-04 12:15 low (z 0.75) were real and quiet.
- At reversal lows, futures OI usually points the wrong way (shorts pressing) and flips
  one 15-min tick later.
- Futures OI alone missed the 09-17 bottom; option OI caught it.

**Volume — computed and stored with the event:**
- `vol_z`: the trigger bar's volume z against the previous `VOLUME_LOOKBACK` closed bars of
  the same contract, **with 09:15 bars left out of the baseline**. That bar distorts the
  baseline until about 10:55 (*V4*).
- `vol_z_max`: the highest z over the pattern's bars, because a sweep's spike is often on
  the pierce bar rather than the reclaim.
- `rvol_tod` *(V4)*: the trigger bar's volume divided by the median volume of the same
  clock bar over the previous `RVOL_SESSIONS` sessions. Spikes happen twice as often at the
  open and close.
- C2 only: `retest_vol_ratio`, the mean volume of the dip bars over the break bar's
  volume (does volume dry up on the retest?).

**OI — joined at read time, not stored (*V5*):**
- **5-min:** `fno_5m_snapshot.fut_bar_oi_change` where `fut_bar_ts` = each pattern bar and
  the bars after it, giving price and OI direction per bar. Also
  `near_pe/ce_oi_change_pct` on the same tick.
- **15-min:** `intraday_fno_data` + `intraday_sentiments` at the snapshot holding the
  trigger bar **and the next one** (+1 tick): `buildup`, PCR and its change,
  `near_pe/ce_oi_change_pct`.
- **Why not store them:**
  - Measured on 230 of 230 quarter-hours (2026-10-22 to 11-04): the classifier writes
    its row about 70 s after the quarter-hour (p90 78 s) and invokes the detector
    straight after. The poller's tick holding the newest closed bar's OI is written at
    about 112 s. It was written first **0 times**, so the detector never sees the trigger
    bar's own 5-min OI.
  - The source rows are immutable and keyed by time, so a read-time join is exact and
    always complete.
- **What "agrees" means**, from the logged rows:

| Event | Agrees |
|---|---|
| reversal up | PE OI falling; futures OI falling on the thrust (covering); buildup turning LONG_* by +1 tick |
| reversal down | CE OI falling; futures OI falling on the drop (unwinding); buildup turning SHORT_BUILDUP / LONG_UNWINDING by +1 tick |
| break up | same tick: SHORT_COVERING or LONG_BUILDUP, PCR rising, CE OI falling (+ whether it is covering or fresh longs) |
| break down | mirror |

## 8. Timing, freshness, statelessness

- **Runs on the existing 15-min chain** (*V7*), on every snapshot, alongside today's two
  branches.
- **Stateless:** each run re-derives the session from its stored bars:
  - up to 75 future 5-min bars;
  - the previous session's bars, for PDH/PDL;
  - the same-clock bars from 20 sessions, for `rvol_tod`;
  - the latest fno row, for L4.
- **It emits only events whose trigger bar closed since the previous run**, using today's
  `bar_closed_within`. Writes are keyed, so a re-run reaches the same answer and does not
  duplicate.
- **Lag:** 1–16 minutes after the trigger bar closes.
- **Window:** triggers from the 09:30 bar to the 15:25 bar. Swing levels exist from about
  10:30 and L4 from 09:45. Nothing is cut by clock time; `clock` is recorded instead.

## 9. Not in v1

- Head and shoulders, triangles, flags, wedges, channels, range boxes. Each gets added once
  you name it and log an example.
- Round numbers, VWAP, weekly/monthly levels.
- Any Telegram alert or `strategy-manager` dispatch from v2 (*V8*).
- Changes to today's reversal TURN (the manager gate) or the continuation alerts (*V9*).
- Calibrating any threshold, ML, new Dhan calls, and changes to the loader or poller.
- Writing `session_observations`, which stays hand-logged.

## 10. Decisions

**V1–V11 were accepted as recommended by Amar on 2026-11-05. V12 is still open.**

| # | Decision | Recommendation (accepted unless marked open) |
|---|---|---|
| V1 | Add OI walls + max pain as levels (L4)? | **Yes.** The 09-18 lows (23300 put wall / max pain) and the 10-30 turn (23000 put wall) sat on them. |
| V2 | Swing levels: the latest swing only, or every unbroken swing this session? | **Latest**, as measured at 9.0/session. The replay can report "all unbroken" alongside. |
| V3 | Candle set: core (hammer, shooting star, engulfing, morning/evening star) plus doji and/or tweezers? | **Core + doji.** Tweezers wait for an example. |
| V4 | Leave 09:15 out of the volume baseline, and add time-of-day relative volume? | **Yes to both**, both recorded. |
| V5 | OI joined at read time rather than stored with the event? | **Yes.** At detection time the newest 5-min OI is missing on 230 of 230 quarter-hours (section 7). |
| V6 | Output: a new table `algo.pattern_events`, or the log only? | **Table**, but only after the offline replay, and its schema gets its own approval. |
| V7 | Run on the 15-min chain (1–16 min lag)? | **Yes for v1.** A 5-min cadence needs a new source of bars, so that is a separate decision when entries are designed. |
| V8 | Telegram from v2? | **None** until the replay results are reviewed. |
| V9 | Leave today's TURN gate and continuation alerts as they are? | **Yes.** |
| V10 | Where the code goes | **A pure library inside `pattern-detector/`**, not wired into the handler in phase A. ~~The offline replay runs from a scratch folder and isn't committed.~~ **Changed by B6 (2026-11-05):** the replay lives in [`replay/`](replay/README.md), outside the Lambda zip; its data, driver and outputs are git-ignored. |
| V11 | Sweep vs breakdown boundary, `RECLAIM_MAX_BARS` | **6 bars (30 min), provisional.** The replay measures the real spread. |
| V12 | How the logged rows map (below) | **OPEN — your call** on the two marked ⚠. |

**How the 9 logged rows map onto v2.** They were logged on index bars; the replay checks
what the future-native definitions actually fire on.

| Logged row | pattern_type | v2 family |
|---|---|---|
| 09-17 09:45 | `orb_breakout_fade_reclaim` (continuation) | C2 (i) at `orh`: break, fade back inside, reclaim |
| 09-17 10:10 | `evening_star` | R1 `evening_star` at resistance. *Replay 2026-11-05:* v2 reads a failed break (R2) at `max_pain` at 10:15, but on damaged SEP-future bars, so this is settled as far as the data can settle it. |
| 09-17 11:10 | `double_bottom` | D (R1/R2 on a second test) |
| 09-17 12:35 | `double_top` | D |
| 09-18 11:30 | `double_bottom` at orb_low | D at `orl` |
| 09-18 13:45 | `breakout_fade_reclaim` at max pain / put wall | ~~R2~~ **C2 touch_hold at `put_wall`/`max_pain`**, corrected 2026-11-05. 23300 had been crossed up after the 11:30 low, so a touch from above is a retest, the same reading as 09-17 09:45. Under B1 it is announced at the first touch, 13:20. |
| 09-21 11:50 | `orb_breakout_retest` | C1 at `orh` (+PDH confluence); the C2 follows at 13:10 |
| 10-30 10:35 | `orb_liquidity_sweep` | R2 at `orl` |
| 11-04 12:25 | `pd_breakout_retest` | ⚠ Price stayed below PDH for 11:30–12:20 (10 bars). With `RECLAIM_MAX_BARS` = 6 that is a breakdown, and 12:25 is a **new C1 up through PDH**, not a retest. Was the hour below PDH still "the retest" to you? |

## 11. Settings (all provisional, all env-overridable)

| Name | Value | Basis |
|---|---|---|
| `NEAR_PCT` | 0.05% (~12 pts) | measured counts at this value; ≈ p25 of 5-min range |
| `MIN_RANGE_PCT` | 0.03% (~7 pts) | ≈ p10 of 5-min range; effect not measured |
| `SWING_K` | 2 | measured (9.0/session) |
| `RECLAIM_MAX_BARS` | 6 | not measured |
| `PLAIN_CLOSE_FRACTION` | 0.5 | B2 plain rejection; not measured |
| `VOLUME_LOOKBACK` | 20 | today's value |
| `RVOL_SESSIONS` | 20 | not measured |
| hammer/star wick ratios | 2.0 / 0.3 | textbook; used in section 1 |
| doji body ratio | 0.1 | textbook; used in section 1 |
| star body ratios | 0.5 / 0.3 | textbook; not measured here |
| `TWEEZER_PCT` | 0.02% | only if V3 includes tweezers; not measured |

## 12. Build order after sign-off (each phase approved separately)

**Phase A — offline, no AWS change.** *Run 2026-11-05; results and Phase B decisions in
[v2-phase-a-replay.md](v2-phase-a-replay.md).*
- Write the pure library in `pattern-detector/` and replay it over the stored history.
- It reports:
  - events per session, by family;
  - **recall** against the 9 logged rows;
  - how often volume/OI agreed (12 sessions of 5-min OI, 35 of 15-min);
  - **descriptive** forward moves on the future after each event (MFE/MAE by time of day),
    with no hit rates and no P&L.

**Phase B — you review the replay.** Settle V-items, settings, the table schema, and
whether anything alerts. *B1–B6 accepted 2026-11-05. B1, B2 and B6 are built and re-run,
with results at the top of [v2-phase-a-replay.md](v2-phase-a-replay.md). Still open: V12
(the 11-04 question), the table schema, and Phase C.*

**Phase C — wire it into the handler** (log + table) and deploy, under its own plan.
