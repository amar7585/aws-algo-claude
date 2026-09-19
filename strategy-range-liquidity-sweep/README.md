# strategy-range-liquidity-sweep

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Components](../docs/components.md)

The 15-minute opening-range liquidity sweep, as a Lambda. Price runs a pool of
resting stops, fails to hold beyond it, closes back inside, and rotates back
across the range — and this function reports the candidates with entry, stop,
targets and risk-reward.

**The trade is the failure, not the break.**

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 8 modules |
| Layers | `neon-db-driver`, `neon-access`, `market-classifier` — the last for Wilder ATR only |
| Trigger | asynchronous invoke from `strategy-manager` |
| Reads | `algo.instrument_master` and the candle tables — **no broker API** |
| Writes | **nothing** — its log is its only output |
| Secrets | `/algo/neon/connection` |

## It writes nothing, and calls no broker API

The regime context arrives in the invocation payload: the snapshot, the daily
read. It does NOT hand over candles — this function reads its own bars from
Neon, because a playbook knows which bars it needs. That is why it carries
`neon-db-driver` and `neon-access`, and why its role can read
`/algo/neon/connection`.

There is still **no Dhan client and no token** — the only thing the manager
ever called Dhan for was a live price, and this playbook never reads it: a
sweep is confirmed by a *closed* bar reclaiming a level, so an unconfirmed live
tick is exactly what the setup must not act on. The reads are all SELECTs;
`db.py` contains no INSERT, no UPDATE and no upsert.

`config.py` keeps its own `IST` constant and `levels.py` its own
`ist_datetime`/`ist_time`, from when this function held no layers at all;
`handler.py` takes `ist_midnight_epoch` from `neon-access` like everything
else. `error-notifier` is the one function here that genuinely carries no
layers.

The consequence worth knowing: **reading this function's log is how a session's
candidates are recovered.** There is no `signal` table. The manager's
regime decision is recorded here, in the consumer, rather than duplicated by
the producer.

## It has no memory, and does not need any

The playbook caps attempts at one re-entry per side per session, and wants the
running cost of an idea reported. That looks like state across invocations.

It is not. **A candidate is a deterministic function of the bars**, so
re-deriving the day's whole sweep sequence from the session's candles on every
run reproduces the attempt count exactly. Nothing is persisted, and a re-run
reaches the same answer instead of double-counting an attempt.

## The gate

The coarse gate already happened — `strategy-manager` invokes this
function only for the `regime|bias` combinations its registry allows — today
that is `sideways|range-bound`. Everything in `gate.py` is
the part the manager cannot know.

| Check | Threshold | Source |
|---|---|---|
| Regime | `sideways`, any bias | the playbook's "neutral range / bullish sideways / bearish sideways" is exactly `sideways` × the three biases. Lower case since the shared classifier replaced the old `RANGE`/`TREND` pair |
| India VIX | within ±5% | playbook |
| Range already spent | `adr` < **0.78** × true ATR14 | **re-scaled — see below** |
| Opening-range width | ≥ 0.15% of price | playbook |
| Clock | 09:45–15:00, with 14:30–15:00 restricted | playbook |

**Every check runs even after one fails.** Short-circuiting would report the
first problem and hide the rest, so a session that failed on VIX would look
like it might otherwise have traded when the range was also spent.

**A check whose input is missing fails as unknown rather than passing.** The
10:00 run may have no `daily_market_sentiment` row yet — that function runs at 09:35
— so `pd_high`/`pd_low` are absent and the range gate has nothing to compare
against. A missing input must not read as a satisfied condition.

The gate **stops the scan**. It does not become a lower-confidence candidate,
which the playbook rules out by name: the pattern looks identical on both sides
of the gate.

### Why 0.78 and not 0.9

The playbook says `adr` below `0.9 × atr14`, and then says the threshold needs
re-scaling: 0.9 was calibrated against a 10-day mean of high-low, and against
gap-inclusive true ATR the equivalent is roughly 0.78. It instructs confirming
which definition `atr14` holds before trusting the number.

It holds **Wilder true ATR**, computed by `strategy-manager` from
`candle_daily`. The premise is measured, not assumed — on the 40 real daily
bars before 2026-09-11:

| | |
|---|---|
| Wilder true ATR14 (gap inclusive) | **170.08** |
| 10-day mean of high−low (the original basis) | **136.95** |

True ATR is the larger number, so the same 0.9 against it would be a looser
gate than the playbook intended. Hence 0.78.

## Three disagreements inside the playbook

Each is left visible in the output rather than resolved by guesswork. In all
three the **explicit rule wins over the worked example**, because the rule is
stated with its rationale and the example is arithmetic that silently omits it.

### 1. The sweep band floor, against Example 1

The rule is 0.04%–0.30%, and the prose explains the floor as "below 10 pts it
is noise inside the level" — 0.04% of 24,000 is 9.6 points, so rule and prose
agree. Example 1's sweep is **8.2 points, quoted as 0.034%**, and labelled
"inside the sweep band". It is below the floor by both statements of the rule.

**Consequence:** run against that session, this function reports the sweep as a
*rejected* candidate with reason "too shallow" (0.034% against a 0.040% floor).
The playbook requires rejections be reported with their reason precisely so the
thresholds can be calibrated, so the disagreement surfaces in the output on its
own. Set `MIN_PENETRATION_PCT=0.03` to make Example 1 qualify.

### 2. The stop buffer, against both examples

The rule: beyond the sweep extreme plus a buffer of `max(0.03% of price, 0.5 ×
mean 5-minute range of the last 6 bars)`, because "placing the stop at the
sweep extreme with no buffer is the most common way this setup gets stopped and
then works."

Both examples then place the stop **exactly at the sweep extreme**, with no
buffer of any size.

Applying the rule widens the risk and lowers the risk-reward. Computed with the
fixed 0.03% term — the only part of the rule the examples' numbers can be
checked against, since they quote no surrounding bars:

| | Playbook (no buffer) | With the buffer the rule requires |
|---|---|---|
| Example 1 | risk 19.20, RR 1:2.15 | risk **26.45**, RR **1:1.56** |
| Example 2 | risk 15.60, RR 1:2.79 | risk **22.83**, RR **1:1.81** |

Both still clear the 1.5 floor, which is the useful part: applying the rule the
playbook states does not invalidate the setups it illustrates.

### 3. The bias-adjusted target — the one that was a real bug

The playbook says a trade running against the day's drift should target the
overhead cluster — VWAP, the 100-period MA, the mid-range shelf — rather than
the full opposite extreme.

The first implementation took the **nearest** cluster member. Example 2 exposed
it: that setup's target is 24,134.45, quoted as "overhead cluster: VWAP
24,123.82 + MA100 24,130.02" — **above both named members**, not at the nearer
one. Taking the nearest put the target at 24,123.82 and the risk-reward at
1:1.44, which fails the 1.5 floor and would have **thrown out a setup the
playbook presents as a good one**.

Fixed to take the cluster's **far edge**, clamped to the opposite range side so
the "shorter than the full range" intent cannot be inverted. Example 2 then
targets 24,132.13 at 1:1.81. A target at the first obstacle is not a target.

## Pools

Computed in the playbook's priority order, each carrying a **side** — a `high`
pool is swept upward and faded short; a `low` pool is swept downward and faded
long. Keeping the side on the pool is what stops a long being generated from a
swept high.

1. **Opening 15-min high/low** — aggregated from the three 5-minute bars
   09:15–09:30. **Never Dhan's native 15-minute bucket**, whose alignment does
   not match the chart these levels were marked on; the playbook rules it out
   by name, and it is the same definition `intraday-market-sentiment` uses for
   `orb_high`/`orb_low`. The two are cross-checked and a mismatch is logged.
2. **Previous day high/low** — from the daily read, absent before 09:35.
3. **First-hour high/low** — 09:15–10:15.
4. **Session high/low made after the opening range** — after, because the
   opening range's own extremes are already pools 1; a session extreme that is
   simply the opening-range extreme is the same pool counted twice.

**Stacking distance is the least-evidenced number here.** The playbook names
stacked pools repeatedly and never says how near is near. It is set from the
playbook's own Example 2 — the 15-minute low at 24,106.00 stacked with the
previous day low at 24,091.50, 14.50 points apart, **0.060% of price** — so
`STACKED_POOL_PCT` is 0.08% to clear that with margin. It is the first number
that should be re-measured against real sessions.

## The stop-out triage

A stop-out has two completely different meanings and the single measurement
that separates them is **consecutive closes beyond the level**.

| Case | Signature | Consequence |
|---|---|---|
| **A** — acceptance | 2 consecutive 5-min closes beyond | the level did not hold. That side is dead for the session **and so is the other one** — if the range high has been accepted through, a later sweep of the range low is not a range trade, because the range no longer exists |
| **B** — extended sweep | never two closes beyond, back inside within a candle or two | the pool was deeper than the first wick suggested. **One** re-entry allowed |
| **C** — no follow-through | filled, never reached T1, ~8 bars later back around entry | expired rather than failed |

Acceptance is measured on the opening range's own boundaries **independently of
whether an attempt was ever taken**, because the range can break without this
function having produced a candidate and the stand-down applies either way.

The Case B re-entry is checked against the **original T2**, never a fresh one.
A wider stop against an unchanged target usually fails the 1.5 floor on its
own — the playbook's "arithmetic quietly saying the edge is gone" — and
recomputing the target too would let the setup move its own goalposts and pass.

**Hard cap: one re-entry per side per session.**

## Verified against real data

Run against the real 2026-09-11 NIFTY session — 75 five-minute bars pulled from
`algo.candle_5min`, with the 40 daily bars before it:

```
opening range   23,277.30 / 23,231.40   (45.90 pts, 0.197% of open - passes width)
day high/low    23,448.10 / 23,231.40   adr 216.70
true ATR14      170.08                  adr/atr14 = 1.274

GATE: FAILED
  - the day has already used 217 of its 170 point true-ATR14 (1.27x against a
    0.78x limit) - there is nothing left to rotate into
  - no new entries after 15:00
```

That is the correct answer: 2026-09-11 opened at 23,270 and closed near 23,435
having spent 1.27× its ATR — a trending day, exactly what this playbook must
refuse. With the gate bypassed to exercise the scan, the same session yields:

```
8 pools: 15min high/low, PDH, PDL, 1Hr high/low, session high/low
6 sweep attempts found:
  09:15  PDL          148.70 (0.636%)  -> breakout
  09:15  session low    6.85 (0.029%)  -> too shallow
  10:05  15min high     8.75 (0.038%)  -> too shallow
  10:20  1Hr high      12.40 (0.053%)  -> QUALIFIES
  10:40  1Hr high       5.95 (0.025%)  -> too shallow
  11:05  1Hr high       0.15 (0.001%)  -> too shallow
opening-range high accepted through at 10:10
```

Note the interaction: the one qualifying sweep is at 10:20, but the opening
range was **accepted through at 10:10**, so the range had already gone and the
handler stands that candidate down too. The 10:05 sweep of the 15-minute high
missing the band by 0.002pp is the kind of near miss the playbook wants named
rather than quietly waved through.

**One gate input cannot yet be verified against real data.**
`vix_change_pct` comes only from `intraday_fno_data`, which has no rows
until its first live session. The VIX check was verified with supplied values
either side of the ±5% threshold; the real series arrives with the first
session.

## Output

`report.py` renders the playbook's own blocks — `SWEEP CANDIDATE`, `RE-ENTRY`
with the cumulative-risk line, and the rejection list. ASCII only, since the
playbook's prose uses em dashes and a unicode minus.

**Rejections are always rendered.** The playbook: "Report every candidate
found, including the ones that failed the sweep band or the RR filter, with the
reason. The rejections are how the thresholds get calibrated over time." A
clean "no setup today" is the correct output on most days.

## Configuration

Every threshold is an environment override, because the playbook is explicit
that these are starting values rather than laws. The ones **not** from the
playbook are marked.

| Variable | Default | Source |
|---|---|---|
| `EXPECTED_CONTEXT_VERSION` | 1 | asserted against the manager |
| `MIN_PENETRATION_PCT` | 0.04 | playbook — but see disagreement 1 |
| `MAX_PENETRATION_PCT` | 0.30 | playbook |
| `MIN_RISK_REWARD` | 1.5 | playbook |
| `MAX_VIX_CHANGE_PCT` | 5.0 | playbook |
| `MAX_RANGE_USED_VS_ATR` | 0.78 | playbook's re-scaled equivalent of 0.9 |
| `MIN_ORB_RANGE_PCT` | 0.15 | playbook |
| `STOP_BUFFER_PCT` | 0.03 | playbook |
| `STOP_BUFFER_ATR_FACTOR` | 0.5 | playbook |
| `STOP_BUFFER_LOOKBACK_BARS` | 6 | playbook |
| `STACKED_POOL_PCT` | 0.08 | **not in the playbook** — set from its Example 2 |
| `NO_FOLLOW_THROUGH_RISK_FRACTION` | 0.5 | **not in the playbook** — "back around the entry" has no number |
| `MAX_ATTEMPTS_PER_SIDE` | 2 | playbook |
| `ACCEPTANCE_CLOSES` | 2 | playbook |
| `NO_FOLLOW_THROUGH_BARS` | 8 | playbook |

The rest are not thresholds — they decide when this playbook runs at all, and
what it reads:

| Variable | Default | Source |
|---|---|---|
| `ALLOWED_REGIMES` | `sideways` | the gate. This is a range playbook; firing it on a trend day is the main way it loses money |
| `ALLOWED_BIASES` | `bullish,bearish,range-bound` | the gate — direction is not the constraint, regime is |
| `CANDLE_INTERVAL_MINUTES` | 5 | which candle table the bars come from |
| `HISTORY_5MIN_BARS` | 120 | how many 5-minute bars are read |
| `HISTORY_DAILY_BARS` | 40 | enough for the ATR period with room to spare |
| `ATR_PERIOD` | 14 | Wilder ATR, from the `market-classifier` layer |

## Deployment shape

Zip the eight modules at the **zip root**. Handler `handler.lambda_handler`,
set under Code → Runtime settings → Edit. **Three layers:** `neon-db-driver`,
`neon-access` and `market-classifier`. No environment variables are required —
every tunable has a default in `config.py`.

**Its own execution role**, with two inline policies: `lambda-logs`
(`logs:CreateLogStream` + `logs:PutLogEvents` on this function's own log group)
and `algo-ssm-read` (`ssm:GetParameter` on `/algo/neon/connection` alone, plus
`kms:Decrypt` conditioned on `kms:ViaService = ssm.<region>.amazonaws.com`). No
Lambda invoke — nothing runs after this. Never borrow another function's role:
the console-generated policies here are scoped to a single log group ARN and a
borrowed one produces **no logs at all**, which for a function whose only
output is its log means it produces nothing whatsoever while appearing to
succeed.

Subscribe its log group to `error-notifier`.

## What it does not do

No orders, no position sizing, no strike selection, no database write, no
recommendation to take the trade. The playbook is explicit: "This is analysis of
what price has already done."
