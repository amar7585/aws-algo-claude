# pattern-detector

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md)

The **two-clock turn detector**, and the gate on `strategy-manager`. Invoked by
`market-classifier` once the judgement row is written; it reads the future's
recent 5-minute bars and runs two branches on every snapshot:

- **Reversal turn** — an abnormal-volume-reversal (same-tick) + buildup/option-OI
  (+1-tick) rule. **Logs** the result either way, and invokes `strategy-manager`
  **only when a turn is confirmed**.
- **Continuation / breakout** — a fresh close through a named key level on
  abnormal volume, and the retest-hold entry that follows. Alert-only: it never
  invokes the manager.

Both branches push to **Telegram** on an actionable, fresh event.

As its **last** step, after everything above, it hands the run's payload to
[`pattern-recorder`](../pattern-recorder/README.md) (v2: candle patterns at levels,
recorded to `algo.pattern_events`), asynchronously. Unset means off. See
[The hand-off to pattern-recorder](#the-hand-off-to-pattern-recorder).

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 7 modules (`recorder.py` added 2026-11-06) |
| Layers | `neon-db-driver` + `neon-access` (no classifier layer — it does not score) |
| Schedule | **none** — invoked by `market-classifier`, once per snapshot (≤25×/day) |
| Reads | `algo.candle_5min` (the future's **and** the index's **closed** bars) and the prior snapshot's PCR and run clock (`captured_at`) from `algo.intraday_fno_data`; the fno + sentiment rows and the daily read arrive in the payload |
| Writes | **no table** — its output is its log and Telegram, until the rule is proven |
| Secrets | `/algo/neon/connection` (to read candles), `/algo/telegram/brief` (to alert) |

## Every threshold here is provisional

The rule was drawn from **5 hand-observed turns over 2 sessions** (2026-09-17
and -18); replayed, the coded volume rule flags 2 of them (below). The
cutoffs are starting points, calibrated once sessions accumulate — the same way
the classifier's option thresholds are deferred. So on early sessions the
manager will fire rarely. See the observations findings and
[[pattern-detector-futures-oi-design]] in project notes.

## The two-clock rule

At a chart turn the future speaks on two clocks:

**Same-tick — an abnormal-volume reversal candle on the future.** A **closed**
bar whose volume is a **spike**, `z ≥ VOLUME_Z_MIN` against its recent baseline,
**and** whose shape reverses: a marginal new low that closes bullish (an
up-turn), or a marginal new high that closes bearish (a down-turn).

**Spikes only.** The z was two-sided on the premise that the 2026-09-17 turns
had volume *collapse* into them. Measured 2026-10-23..28, that premise does not
hold:
- Replayed, the coded rule flags **2 of the 5** observed turns. Both are
  2026-09-18 lows, and both are spikes (z 9.84 and 3.42). It flags none of the
  09-17 turns.
- The low side can barely fire at all. A zero-volume bar only reaches
  `−mean/sd`, about −1.6 on the future, so on most bars z = −2 is out of reach.
- Across 2,175 futures bars (09-15..10-28) the collapse side flagged 0 real bars,
  against 35 spikes. Its only live output was a forming-bar artifact (below).

**The baseline leaves 09:15 bars out** (live since 2026-11-06 12:07:12 IST,
CodeSha256 `GgRBzfXXAQWqxuCHPoC+IDZzdyR/HwkTwvwps6d7Dog=`; the previous code was
`1nNqeiLS…`, the rollback). The same deploy holds the session limit on breaks
below. The baseline is
the 20 bars before the one scored, and it still reaches back into the previous
session. The open bar is several times a normal bar, so a plain 20-bar baseline
carried it until 10:55.
- **The effect, measured on 35 sessions (09-15..11-04):** with the open bar in,
  the 09:30 bar's baseline sd was 1.53× larger at the median, and up to 3.17×.
  That cut the z by about a third.
- **What it cost live:** on 2026-11-06 the 09:30 `orb_high` break scored z 1.48
  with the open bar in the baseline, so no LEVEL BREAK was sent. Without it the
  z is 5.55.
- **Replayed over 850 stored runs (09-15..11-06 12:00), deployed zip against
  this code:**
  - leaving it out gained 12 level breaks (today's 09:30 one included), 2 retest
    entries and 1 turn (2026-09-24 09:45, so 1 extra manager dispatch);
  - it lost none;
  - every gain is on a bar from 09:30 to 10:05;
  - 5 more candidates were logged as unconfirmed.
- **Unchanged:** the two 2026-09-18 turns (z 9.84 and 3.42) and every
  hand-logged observation.
- **Same rule in v2:** `pattern-recorder` uses it too.
- **Verified live, 2026-11-06:** all 14 runs after the deploy (12:15 to 15:25)
  equal a replay of the new code, with 0 errors. Each was "no turn" with no
  alerts, and each forwarded to the recorder.

**Not fixed: the 09:15 bar's own z.** It is scored against the previous
afternoon, and reaches 2.0 on 29 of 35 sessions (median 6.66). So for a gap
through PDH/PDL at the open, the volume gate filters almost nothing. 7 of the 23
replayed level breaks are such gaps. A real fix needs time-of-day volume, which
v2 records as `rvol_tod`, so it can be judged from data first.

**+1-tick — confirmation.** One snapshot later the turn is confirmed by the
futures `buildup` turning the right way (`LONG_BUILDUP` for a low,
`LONG_UNWINDING` for a high) **OR** the relevant option OI moving
(`|near_pe_oi_change_pct| ≥ OI_CONFIRM_PCT` for a low,
`near_ce_oi_change_pct` for a high). "buildup **or** option-OI" confirmed 5/5;
buildup alone 4/5, so option OI is the second leg. Confirmation is read off the
**current** snapshot; the candidate off the future bars one interval back.

**A turn fires only when a candidate is confirmed.** Level proximity — both
2026-09-18 lows sat on the 23300 max-OI-put / max-pain wall — is **annotated with
distance, not gated on**: a turn away from a wall is logged with its distance
rather than dropped, so the premise stays measurable.

## Stateless

The candidate is re-derived from the stored future bars each run — no pending
turn is remembered between invokes, matching `strategy-range-liquidity-sweep`.

**Closed bars only.** A bar is read once `candle_ts + 300 <= captured_at`, the
snapshot's stored run clock. This is the same rule the loader and the poller
use.
- **What it excludes.** `snapshot_ts` is the bar still forming, which the loader
  stores deliberately. It is seconds old and near-empty on every run but the last
  of the day, so it is left out.
  - Scored, it read as a volume "collapse". On 2026-10-23 11:00 it fired a TURN at
    z −3.0.
  - It also priced retest entries on a close that was seconds old.
- **What it keeps.** The 15:25 bar on the 15:30 run has already closed, so it is
  kept.
- **Why it stays replayable.** The bound is a stored value, not the clock, so a
  re-run on stored bars reaches the decision live reached.

`candle_5min` is queried on the **full identity** `(fut_security_id,
FUTURES_INSTRUMENT_TYPE)` — never `security_id` alone (hard rule #4); the
instrument type is the same constant the loader stored the future's bars under.

## The continuation / breakout branch

A second vocabulary beside the reversal turn. A reversal closes *against* the new
extreme; a continuation closes *through* a named level, so the two never collide.
**Provisional and under-measured — n=1** (the 2026-09-21 `orb_breakout_retest`).
The structure is committed; every number is a starting point.

- **The level is a gate.** The named levels are the 15-minute opening range
  (`orb_high` / `orb_low`, on the fno row) and the previous day's extremes
  (`pd_high` / `pd_low`, on the daily read — absent when the daily read is, and
  then that pair is simply unavailable). No level broken, no candidate.
- **Break** — a close through a level where the prior bar closed on the near
  side, so it is a crossing that fires once, not any bar sitting beyond a level.
  Read on the **index** bars (the levels are spot levels). The abnormal-volume tell
  (a spike, `z ≥ CONT_VOLUME_Z_MIN`) is the **future's**, aligned by timestamp,
  because the future carries the real traded volume.
- **Only this session's bars can be a break** (since 2026-11-06).
  - **Why:** the bars read reach back into the previous afternoon. Today's
    opening range applied to them found "breaks" there. Those were never fresh,
    so never alerted, but a retest of one on today's bars was.
  - **How often:** 4 RETEST ENTRY alerts in 35 sessions (09-16, 09-17, 09-22,
    10-07). Each named a break from the session before and carried that bar's z.
  - **Gap opens still count:** the bar *before* a break may be the previous
    session's last, so a 09:15 gap through a level is still a break.
- **Retest entry** — after a break, price closes back on the far side of the
  level (the dip), then a later bar closes back across it (the reclaim). The
  reclaim bar is the entry. A continuation that never dips back has no retest
  entry; that case is deliberately deferred.
- **Confirmation is annotated, not gated.** Buildup, PCR direction against the
  prior snapshot, and far-side option OI unwinding are attached to the alert as
  `confirmed_by`. A break confirms **same-tick**, not +1: the +1-tick lag is a
  reversal phenomenon.
- **It does not invoke the manager.** Alerts only; the reversal turn remains the
  sole manager gate.

## Output: log, Telegram, no table

It writes no table. Detections go to the log — its output — the way
`strategy-range-liquidity-sweep`'s do — and the actionable ones are pushed to
Telegram so a setup reaches the phone rather than waiting in CloudWatch.

| Event | Telegram when |
|---|---|
| Confirmed reversal **turn** | the reversal candle closed since the previous run |
| **Level break** | the break bar closed since the previous run |
| **Retest entry** | the reclaim bar closed since the previous run |

"Fresh" is what keeps this to one message per event. The detection is re-derived
statelessly every 15 minutes, so gating the alert on when the trigger bar closed
is what announces it once instead of on every re-derivation.

"Closed since the previous run" means the bar closed after the previous
snapshot's `captured_at` and by this one's.
- A bar that was still forming at the previous run is fresh on the run that
  first reads it closed.
- That includes candles starting on a quarter-hour. The earlier
  `(prev_snapshot_ts, snapshot_ts]` window could never call those fresh, so a
  turn on one was never alerted.

The manager dispatch for a confirmed reversal is **not** freshness-gated.

**Known gap, not fixed.** A turn confirmed only on its candle's later look (the
snapshot after the one that first read it closed) is dispatched, but not
alerted.

Delivery is fail-loud: a refused Telegram call raises, so Lambda records the
error and `error-notifier` reports it. It uses the same `/algo/telegram/brief`
parameter as the daily brief and `error-notifier`.

A table comes once the rule is proven on live sessions. It never writes the
hand-curated `algo.session_observations`, whose `outcome`/`resolved_ts` are human
and future-dependent.

## The chain

```
  → market-classifier    writes intraday_sentiments, invokes with fno + sentiment
  → pattern-detector      confirmed turn?  ── no ─→ stand down, log the reason
                                            └─ yes ─→ invoke ↓
  → strategy-manager      routes on regime|bias  (now runs ONLY on a turn)

  pattern-detector, last step, every run ─→ pattern-recorder (Event) → algo.pattern_events
```

## The hand-off to pattern-recorder

`recorder.py`. After the turn, the continuation alerts and Telegram are all done, the
detector forwards the payload it received to `pattern-recorder`, with `Event`.
- **Why a separate function, and why last:** this function has no `EventInvokeConfig`,
  so Lambda retries a failed invocation twice.
  - Had v2 run in here, a v2 failure after the existing branches would re-send their
    alerts and re-dispatch the manager on each retry.
  - Invoked last and asynchronously, a recorder failure stays in the recorder's own
    log group.
- **Unset means off:** with no `PATTERN_RECORDER_FUNCTION_NAME`, nothing is invoked and
  a log line says so. That env var is the kill switch, so turning v2 off needs no
  deploy.
- **Live since 2026-11-06:** code deployed 09:39 IST (CodeSha256 `1nNqeiLS…`; the
  previous code was `WU75ZJh7…`), switched on 09:40:22. The first hand-off was the
  09:45 run.
- **Verified 2026-11-06 on 816 stored payloads, old code against new:**
  - with it unset, every decision and outward call is identical (29 turns, 29
    dispatches, 43 Telegram alerts);
  - with it set, the same, plus the hand-off as the last call on every run.

**Write, then invoke** — there is nothing to write, so it just invokes, `Event`,
on a confirmed turn. `UNSET MEANS OFF` — with no `STRATEGY_MANAGER_FUNCTION_NAME`
set, a turn is detected and logged and nothing is dispatched.

## IAM

Its own execution role (`pattern-detector-role`), not a borrowed one:
`ssm:GetParameter` on `/algo/neon/connection` **and `/algo/telegram/brief`**
(+ `kms:Decrypt` via SSM), `logs` on **its own** log group only, and
`lambda:InvokeFunction` on `strategy-manager`'s ARN **and `pattern-recorder`'s ARN** —
not a wildcard. A role
missing the Telegram parameter does not fail at import: it raises on the first
alert, after the detection has already run. See CLAUDE.md on the silent
borrowed-role trap. `error-notifier` is subscribed to this function's log group.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `STRATEGY_MANAGER_FUNCTION_NAME` | *(unset)* | unset = off; set it to switch the manager gate on |
| `STRATEGY_MANAGER_INVOCATION_TYPE` | `Event` | a slow playbook must not sit inside this timeout |
| `PATTERN_RECORDER_FUNCTION_NAME` | *(unset)* | unset = off; set to `pattern-recorder` to hand every run to v2 (always `Event`) |
| `VOLUME_Z_MIN` | `2.0` | z that counts as a volume spike (spikes only) — **provisional** |
| `VOLUME_LOOKBACK` | `20` | bars for the z-score baseline, 09:15 bars left out |
| `CANDIDATE_WINDOW_SECONDS` | `1800` | how far back the reversal candle is looked for |
| `OI_CONFIRM_PCT` | `3.0` | \|option OI change\| that confirms a turn — **provisional** |
| `LEVEL_EPS_PCT` | `0.15` | proximity to a wall counted as "at the level" — annotated, not gated |
| `FUT_BARS_LOOKBACK` | `40` | closed future 5-min bars read per run |
| `FUTURES_INSTRUMENT_TYPE` | `FUTIDX` | the future's instrument_type in `candle_5min` |
| `CONT_VOLUME_Z_MIN` | `2.0` | z on the future's break bar that counts as a spike (spikes only) — **provisional, n=1** |
| `TELEGRAM_PARAMETER_NAME` | `/algo/telegram/brief` | `bot_token` + `chat_id`; shared with the daily brief and `error-notifier` |
| `HTTP_TIMEOUT_SECONDS` | `8` | Telegram call timeout |
