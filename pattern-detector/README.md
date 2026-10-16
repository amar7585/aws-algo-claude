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

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 6 modules |
| Layers | `neon-db-driver` + `neon-access` (no classifier layer — it does not score) |
| Schedule | **none** — invoked by `market-classifier`, once per snapshot (≤25×/day) |
| Reads | `algo.candle_5min` (the future's **and** the index's bars) and the prior snapshot's PCR from `algo.intraday_fno_data`; the fno + sentiment rows and the daily read arrive in the payload |
| Writes | **no table** — its output is its log and Telegram, until the rule is proven |
| Secrets | `/algo/neon/connection` (to read candles), `/algo/telegram/brief` (to alert) |

## Every threshold here is provisional

The rule is measured on **5 turns over 2 sessions** (2026-09-17 and -18). The
cutoffs are starting points, calibrated once sessions accumulate — the same way
the classifier's option thresholds are deferred. So on early sessions the
manager will fire rarely. See the observations findings and
[[pattern-detector-futures-oi-design]] in project notes.

## The two-clock rule

At a chart turn the future speaks on two clocks:

**Same-tick — an abnormal-volume reversal candle on the future.** A bar whose
volume is `|z| ≥ VOLUME_Z_MIN` against its recent baseline **and** whose shape
reverses: a marginal new low that closes bullish (an up-turn), or a marginal new
high that closes bearish (a down-turn). The z-score is **two-sided on purpose** —
the anomaly's direction varies: the 2026-09-17 turns had volume *collapse* into
them, the 2026-09-18 turns had *spikes*. What is abnormal is the magnitude, not
the sign. Fired on 5/5 observed turns.

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
Reading the future's bars bounded on `snapshot_ts` rather than the clock is what
makes a re-run reach the same answer instead of merely repeating.

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
  (`|z| ≥ CONT_VOLUME_Z_MIN`) is the **future's**, aligned by timestamp, because
  the future carries the real traded volume.
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
| Confirmed reversal **turn** | the reversal candle closed within the last snapshot interval |
| **Level break** | the break bar closed within the last snapshot interval |
| **Retest entry** | the reclaim bar closed within the last snapshot interval |

"Fresh" is what keeps this to one message per event: the detection is re-derived
statelessly every 15 minutes, so gating the alert on the trigger bar's age is
what announces it once instead of on every re-derivation. The manager dispatch
for a confirmed reversal is **not** freshness-gated.

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
```

**Write, then invoke** — there is nothing to write, so it just invokes, `Event`,
on a confirmed turn. `UNSET MEANS OFF` — with no `STRATEGY_MANAGER_FUNCTION_NAME`
set, a turn is detected and logged and nothing is dispatched.

## IAM

Its own execution role (`pattern-detector-role`), not a borrowed one:
`ssm:GetParameter` on `/algo/neon/connection` **and `/algo/telegram/brief`**
(+ `kms:Decrypt` via SSM), `logs` on **its own** log group only, and
`lambda:InvokeFunction` on `strategy-manager`'s ARN — not a wildcard. A role
missing the Telegram parameter does not fail at import: it raises on the first
alert, after the detection has already run. See CLAUDE.md on the silent
borrowed-role trap. `error-notifier` is subscribed to this function's log group.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `STRATEGY_MANAGER_FUNCTION_NAME` | *(unset)* | unset = off; set it to switch the manager gate on |
| `STRATEGY_MANAGER_INVOCATION_TYPE` | `Event` | a slow playbook must not sit inside this timeout |
| `VOLUME_Z_MIN` | `2.0` | \|z\| that counts as an abnormal-volume bar — **provisional** |
| `VOLUME_LOOKBACK` | `20` | bars for the z-score baseline |
| `CANDIDATE_WINDOW_SECONDS` | `1800` | how far back the reversal candle is looked for |
| `OI_CONFIRM_PCT` | `3.0` | \|option OI change\| that confirms a turn — **provisional** |
| `LEVEL_EPS_PCT` | `0.15` | proximity to a wall counted as "at the level" — annotated, not gated |
| `FUT_BARS_LOOKBACK` | `40` | future 5-min bars read per run |
| `FUTURES_INSTRUMENT_TYPE` | `FUTIDX` | the future's instrument_type in `candle_5min` |
| `CONT_VOLUME_Z_MIN` | `2.0` | \|z\| on the future's break bar that counts as abnormal — **provisional, n=1** |
| `TELEGRAM_PARAMETER_NAME` | `/algo/telegram/brief` | `bot_token` + `chat_id`; shared with the daily brief and `error-notifier` |
| `HTTP_TIMEOUT_SECONDS` | `8` | Telegram call timeout |
