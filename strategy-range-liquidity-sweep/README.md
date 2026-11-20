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
| Runtime | Python 3.14, zip package, 12 modules |
| Layers | `neon-db-driver`, `neon-access`, `market-classifier` — the last for Wilder ATR only |
| Trigger | asynchronous invoke from `strategy-manager` — **every 5-min tick since 2026-11-14** (pattern-recorder's tick hand-off) |
| Reads | today's 5-min index candles **from the payload** (`index_bars`); `algo.candle_daily` for ATR; today's 15-min rows (`algo.intraday_sentiments` + `algo.intraday_fno_data`) — **no broker API** |
| Writes | `algo.playbook_alerts` — the alert register, **only when `REGISTER_ENABLED=on`** — **on since 2026-11-14 22:01 IST** (CodeSha256 `VGiH4SNG…` since 2026-11-20 22:48 IST (out at 15:15, before the closing auction); before it `HVmgAGjN…`, bias removed 2026-11-19 20:49, and `yZ30IM0U…`, one rule set, 2026-11-19 20:37; versions kept: 8 = `HVmgAGjN…` (main 5eb3ed1), 7 = `RaMJeVy2…` (Amar's within-levels gate) - versions 1-6 deleted 2026-11-19). Otherwise nothing: its log and its Telegram messages are its output |
| Secrets | `/algo/neon/connection` |

## Since 2026-11-20: out at 15:15, before the closing auction

**Decided by Amar 2026-11-20 ("Out at 15:15").**

- **Deployed:** 2026-11-20 22:48 IST as CodeSha256 `VGiH4SNG…`.
- **Rollback:** version 8 (`HVmgAGjN…`) is the code from before.
- **First live session:** Mon 2026-11-23.
- **Status:** an interim, until the risk manager Amar asked for takes over the 15:15 exit for every playbook.

The live function was not invoked after the deploy. Verified instead:
- the uploaded zip run through the real handler on all 69 of 11-20's ticks, identical to that day's validation;
- the downloaded live code equal to the repo on all 12 modules;
- handler, layers and environment unchanged.

**The rule.** If a trade is still in after the 15:10 candle, it is closed at that candle's close, which is the 15:15 price. It gets a new step and outcome, `CLOSED AT 15:15`, sent at the 15:16 tick. A trade that exits on the 15:10 candle ends its watch there too.

**Why.** Since 2026-08-03, NSE's closing auction has stopped continuous trading in the NIFTY stocks at 15:15.

- The index's 15:15 and 15:20 candles are flat lines: median range 0.00 on 76 of 76 sessions.
- The 15:25 candle is one jump to the official close: median 24.9 pts, max 201.
- Until now `manage.follow` judged stops and targets on that jump, and marked a trade still in to it. Nobody trades at that print.
- The future mostly does not follow the jump. From 15:10 to 15:25 the future moved a median 9.1 pts against the index's 24.2, with correlation 0.57 (0.75 before 08-03). On 08-03 the index rose 201 pts and the future moved −1.

**Replay** (read-only, the production modules on 110 sessions 06-15 .. 11-20; written up in `pattern-recorder/replay/out/auction-close-2026-11-20/`, which is git-ignored). The gate-off run reproduces the 24 entries and +11.53R in the one-more-look table below. Only 2 of the 24 trades were still in at 15:10, and the jump never hit a stop or T2:

| | as before (to the 15:25 print) | out at 15:15 | followed on the future to 15:30 |
|---|---|---|---|
| 08-13 long, 15-min low (re-entered 11:35) | +0.38R | −0.59R | −0.65R |
| 11-13 long, 15-min low (live) | +0.75R | +0.67R | +0.68R |
| all 17 trades since 08-03, gate off | +9.91R | +8.87R | +8.82R |

Exiting after the 15:15 candle instead (out at 15:20) gives the same +8.87R. Its close is a median 0.3 pts (max 4.8) from the 15:10 close. Amar chose 15:15, which also sends the message five minutes sooner.

**What stays as it was:**
- The register rows already written, including the live 11-13 long, which is still `open` at +0.75R. Only the current session is re-derived on each run.
- Entries and the watch: no new entries after 15:00, and the watch ends at 15:00.

## Since 2026-11-14: every tick and the trade after entry

Decided by Amar on 2026-11-14 after a measurement on 104 sessions (06-16 .. 11-13):

- **The 2-close count does not tell a break from a sweep.** Once a push closes back inside, about 2 in 3 are
  sweeps however many closes it made beyond.
- **What does tell them apart needs no number:** the candle back inside closes beyond the push's *whole*
  range (below every push candle's low, for an up push). 91% of those pushes were sweeps, against 50% without it.
  - **One more look** (Amar 2026-11-16, option B): when that first close back inside is not past the whole push,
    the next candle may still be — it must close inside, print no new extreme and close past the same line. Those
    pushes swept 72% (n 25; the rest that fail both looks 45%). See the 2026-11-16 deploy note below.
- **Every exit on elapsed time lost to holding** on no-trend days.
- **The exit that held up is structural:** exit when a candle closes back beyond the swept level, then re-enter
  on a new best close.

**When it runs.** pattern-recorder hands every tick to the manager, and the manager sends it here *whatever the
regime*, so an open trade keeps being watched. Each run re-derives the session candle by candle. At every candle
it uses only the candles up to it and the 15-min row in force then — written by the candle's close plus
`ROW_VISIBLE_LAG_SECONDS`. That makes every run reach the same answer, so each step is announced once: on the run
whose newest candle it is.

**When it may enter** (Amar's gate, live since 2026-11-15 09:33, `RaMJeVy2…`): **price within the levels** — the
entry candle's close between min(PDL, 15-min low) and max(PDH, 15-min high) — and **volatility not high**: regime
`sideways` or `trending`, not `volatile-expansion`, plus the fine gate below. Amar: "if price is within levels then
why it matters — depends on high volatility." No trend test and no bias. Replayed before the change (41 sessions
as live): +4 entries (10-01 short and 10-30 long, each under both rule sets — all reached T2; 10-30 is Amar's
logged trade, blocked before only because the regime read trending), none removed; new rules +1.62R → +4.40R,
full losses 1 → 1; current −0.50R → +2.84R. The within-levels test is what keeps the losers out: without it the
11-03 short against an up-trend enters and loses; on all 21 gate-off trades over 104 sessions it excludes 3 (all
losers, all against the day trend) and keeps all 9 winners — two of those three by under 2 points, so the edge is
sharp. Verified after the change: old vs new differ on exactly those 4 entries; send-once 41/41.

Before 2026-11-15 09:33 the cell was regime `sideways` + `day_trend` `none`. **No bias**
since 2026-11-14 22:58 (Amar: a sweep happens within the levels whatever the day's bias; it is a level-and-volatility
event). Before 2026-11-14 the manager's registry decided the cell; until 22:58 that day the cell also required bias
`range-bound`. Measured before removing it (41 sessions as live): +5 entries on 09-25, 10-07, 10-28 (all longs on a
bearish bias), none removed; new rules 2 → 5 entries, +1.04R → +1.62R, full losses 1 → 1; current rules 1 → 3,
−1.00R → −0.50R. Verified after the change: old vs new code differ on exactly those 5 entries (blocked → entered in
the register) plus the bias wording dropped from 6 reasons; send-once 41/41.

**One rule set (since 2026-11-19).** Until then an older rule set ("current": 2 closes beyond the opening range =
the range broke, 2 attempts per side, a re-entry only after Case B, no trade management) ran beside these, and every
message ended with which set produced it. Amar removed it on 2026-11-19 ("we have enough validation"): replayed over
45 sessions with stored 15-min rows, these rules took 9 trades for +318 points, the old set 5 for +98. Verified before
the deploy: on all 45 sessions the register equals the previous build's `new` rows exactly and every message equals
the previous build's minus the tag (in the replay 116 `current` rows were no longer written, and no message was
current-only). Deployed 2026-11-19 20:37:20 IST, CodeSha256 `yZ30IM0U…` (previous `0NUOBVS0…`). The 12 `current` rows
already in `algo.playbook_alerts` (5 sessions) were backed up (`pattern-recorder/replay/out/backup-2026-11-19/`,
git-ignored) and deleted 20:37 IST; Lambda versions 1–6 deleted, version 7 (`RaMJeVy2…`) kept as a rollback copy.

| | the rules |
|---|---|
| a push with 2+ closes beyond | a sweep if the candle back inside closes past the whole push — or, failing that, the next candle closes inside, prints no new extreme and closes past it (one more look, since 2026-11-16; it is then the sweep candle) — the push is in the band and the candle after prints no new extreme (`sweep.push_sweeps`) |
| "the range broke" | a 2+ close push at the opening range that failed that test (both looks), or has not come back yet (`sweep.range_broke_at`) |
| attempts | one live idea per side |
| after entry | `manage.follow`: EXIT (close back beyond the level, now watching), RE-ENTRY (a new best close the trade's way, only when an entry would be allowed: cell + before 15:00), WATCH ENDED, TARGET REACHED, STOP HIT, and since 2026-11-20 CLOSED AT 15:15 (still in after the 15:10 candle: out at its close, before the closing auction). One exit and one re-entry per trade, as measured |

**Messages.** All of them go to Telegram (`TELEGRAM_ENABLED` is a kill switch, on by default): every entry and every
trade step.

**The SIGNAL carries the price at the signal** (Amar 2026-11-15, live since 09:26 that day, `CkCH//Y3…`). The
playbook's entry is the close of the candle back inside; the message can only arrive after the NEXT (confirming)
candle closes, so a market order fills near that close. The SIGNAL now says so, with the risk-reward at that price
against the same stop and T2:

```
SWEEP SIGNAL SHORT 15min high - 2026-11-13 11:05
sweep 10:40+, entry 25199.65 stop 25259.45
at the signal 25189.1 (11:00 candle close) - enter at market, RR there 1:1.59
T1 25175.42 T2 25077.55 RR 2.04 (C)
from: sweep playbook, new logic
```

On the replayed entries the two can differ a lot (09-25: 1:2.85 at the playbook's entry, 1:1.56 at the signal). The
register already stores that price as `signal_close`. Verified: send-once 41/41, every other message and outcome
identical to before; handler checks 9/9 + 8/8; deploy smoke (11-13 full day) rewrote the same 9 rows, nothing sent.

**Verified offline, 2026-11-14** (session 9eae7046 scratchpad, real handler, every tick of 41 stored sessions,
option-4 regime and `day_trend` recomputed):
- **Send-once:** on 41 of 41 days, the messages sent across the ticks equal the final tick's derivation, each once.
- **Current logic:** 1 entry (11-09 long, stopped), the same as the 11-14 replay of today's rules.
- **New logic:** 2 entries (11-09, and 11-13 trade 2 short 25199.65, which reached T2 25077.55 at 14:05), the same
  as the structural replay.
- **The exit / re-entry code** equals the step-1 measurement on all 113 measured ideas (+22.67R both ways).

**A fixed hole.** Run on a 15-min snapshot (an alert hand-off), this function used to skip announcing the first
candle of each 15-minute window. It compared against `prev_snapshot_ts` instead of one candle earlier. Fixed; the
every-tick path never had it.

## The alert register (live since 2026-11-14 22:01 IST, `REGISTER_ENABLED=on`; first live session 2026-11-16)

Amar's design: price touches a level → that **alert candle** is recorded; later scans know the condition was met
and look for the **entry**; a touch that goes away waits for a retest, and with no retest the trade is marked
**missed**. He chose how it is kept: **re-derive and record** — every run rebuilds the whole session's register
from the closed candles ([register.py](register.py)) and upserts it into `algo.playbook_alerts`
([migration](../migrations/2026-11-14-playbook-alerts.sql)). **Nothing reads it back**, so a failed or retried run
cannot leave a wrong state: the next run rewrites the day. It changes no entry, exit or message.

**One row per alert** — a level touched on one side. `logic` is always `new` since 2026-11-19 (the old `current` rule set was removed and its rows deleted). Retests of the same
level while it is watching are counted in `touches`, and `touch_log` says, per touch, in the playbook's own words,
why it did or did not give an entry ("wick 0.02% through: too shallow", "push of 3 closes beyond … -> ENTRY").

| status | meaning |
|---|---|
| `watching` | touched, no entry yet, nothing has ended it |
| `entered` | the playbook took the trade — `outcome` / `steps` say how it went |
| `blocked` | the structure qualified but the gate or the cell refused it — followed in shadow, same fields |
| `missed` | no entry, and price reached **T2** (the opposite side of the opening range) without us — the full move |
| `invalid` | no entry, and the level was accepted through, the range broke for good (no new alerts after that), or — new rules — a push was still beyond the level at 15:00 |
| `ended` | no entry, and 15:00 came first |
| `covered` | no entry here because a trade on the same side was taken (or blocked) at another level |

The two definitions Amar approved after the replay: missed = T2, not T1 (a T1 can sit on the level — 11-13, PDL,
0.75 pt away while PDL broke), and a push still beyond at 15:00 = invalid (34 of 38 new-rule "ended" alerts were
exactly that).

**The write** goes on the connection the reads opened, **after the messages**, as one statement (an upsert that also
counts, against the pre-statement snapshot, any stored row this run did not re-derive), then a commit. A stored row
not re-derived, or a session with stored rows and none derived, raises and rolls back — a touch on closed candles
cannot un-happen, so that means the derivation changed under the data. A failed write raises after the messages
are out; Lambda's automatic retry may then repeat them (a duplicate is visible, a lost signal is not).

**Verified offline 2026-11-14** (scratchpad eac892bd, 104 sessions, 41 with stored 15-min rows):
- the production module equals the replay prototype on every row, apart from the agreed 15:00 rule (35 alerts
  `ended` → `invalid`); every tick of 41 sessions (2,796 ticks): rows never shrink, every row seen is in the final
  register, a closed alert never changes
- the known trades are `entered`: 11-09 09:45 long (both rule sets, stop hit), 11-13 11:00 short (new rules, T2)
- with the register off, the pre-deploy tick harness output is identical, line for line
- the real handler against a fake connection, 13/13: one statement, written after every send, 28 parameters per
  row, one commit; a lost row and "none derived, some stored" both raise and roll back; off = no SQL at all
- the real statement, rendered from 11-13 rows, run on Neon against a temp table dropped at commit: insert, then
  update in place (first_seen_at kept), and the planted stale row counted

**Deployed 2026-11-14 ~22:00 IST** (Amar: "deploy now, we will test on Monday's session"): the live code was kept
as Lambda version 1 (`HsOac/jT…`), the table created (migration applied), the zip deployed (`IBg6OIzl…`),
`REGISTER_ENABLED=on`. Smoke-invoked with real 11-13 data: register off → as before (24 rows, trade 2, nothing
sent); on → the 09:25 tick wrote 2 rows, the full day 9 (both earlier rows re-derived), a second full-day run
rewrote the same 9; nothing sent, no error. **The 9 stored 11-13 rows equal the offline replay row for row.** A
full-day run takes ~1.9 s with the register (~1.4 s without), 102 MB of 256. Those 11-13 rows stay in the table
as that session's record.

**Docstring-only redeploy, ~22:19 IST** (`YLhNk/U9…`, `IBg6OIzl…` kept as version 2): `config.py` and `notify.py`
still said the function "writes nothing". Code identical apart from docstrings (checked module by module, AST with
docstrings stripped); the folder zip now equals the deployed code byte for byte. Smoke: the 11-13 full day rewrote
the same 9 rows, first_seen_at kept, nothing sent, no error.

**No-bias deploy, 22:58 IST** (`M3fvb4DI…`, `YLhNk/U9…` kept as version 3): bias removed from the sweep (see "When
it may enter"). Smoke with real 11-13 data: the full day rewrote the same 9 rows (no bias in any reason), nothing
sent, no error.

**Out-of-order ticks - found by that smoke, FIXED 23:08 IST (`+4qimC9c…`, option A, Amar).** The 09:25 tick of 11-13,
replayed AFTER the full day's register was stored, raised `7 stored row(s) were not re-derived`: the "never shrink"
check could not tell an OLDER tick arriving late (an async retry, a delayed invoke) from a derivation that lost rows.
Now every row carries `derived_through_ts`, the newest candle of the run that wrote it
([migration](../migrations/2026-11-14-playbook-alerts-derived-through.sql)); an update applies only when the
incoming run is derived through the same candle or later, and only rows derived through the incoming run's candle
or earlier count as lost. Checked: offline 8/8 (fake connection, including an older run after a newer one and the
empty-register paths); on Neon against a temp table - newer run 3 written, OLDER run 0 written / 0 lost / 3 newer
kept with the table unchanged, retry 3 written, a planted stale row counted lost; live, the same sequence that failed
at 22:58 (full day → 09:25 → full day): 9 written, then 0 written with `newer_kept` 9, then 9 - no error.

**One more look, deployed 2026-11-16 19:14 IST** (`0NUOBVS0…`, `RaMJeVy2…` kept as version 7; Amar: "Plan B", then
"Yes, build + deploy"). Found on the first live day: both of Amar's 11-16 trades were pushes that failed the
whole-push test on their first close back inside — the ORH short by 2.70 pts (10:00 close 25299.9 vs the push's low
25297.2), the ORL long by 13.25 pts (14:15 close 25195.55 vs 25208.8) — so the playbook called the ORH push a break
and stood down for the day. Three options were replayed (A as live; B one more look on the next candle; C the line
from the push's closes, which always passes, i.e. no test). B was chosen:

| new rules | gate as deployed (42 sessions) | gate off (105 sessions, 06-16 .. 11-16) |
|---|---|---|
| A (before) | 7 entries, +4.40R, 1 full loss | 21 entries, +9.44R, 2 full losses |
| **B (now)** | **9, +6.67R, 1** — adds the 11-13 long signalled at the 14:40 tick (open at the close, +0.75R) and the 11-16 short at the 10:15 tick (T2, +1.53R), loses none | 24, +11.53R, 2 |
| C (not taken) | 20, +14.94R, 2 (≈4.2R of it open trades marked to the 15:25 auction close) | 61, +18.67R, 11 |

Price-only labels (X 0.20%, pushes of 2+ closes): first look passes 91% swept (n 43), second look only 72% (n 25),
both fail 45% (n 84). B catches the 11-16 short one candle after Amar's entry (signal 10:15 tick, at the signal
25302.0); it does not catch the 11-16 long. Current rules unchanged. A break on the second look is recorded on that
candle (the register's `status_ts` and the stand-down move one candle later on such pushes; 48 register rows across
25 sessions change only that and the wording "…, nor did the next candle"). Verified before deploy: repo == the
replayed variant on every trade field (52/52); register vs A: new entries on 11-13 and 11-16, on 10-05 the range no
longer stands down (the ORH alert reads `missed`, two later alerts appear), on 10-27 / 11-06 a retest lands on the
existing alert because the break is now decided a candle later — nothing else; send-once 42/42 every tick,
handler/register checks 9/9. Smoke (the 11-13 full day, real
Neon): 9 rows, the 11-13 long now `entered`, 2 new-logic ideas, nothing sent, no error. The 11-16 rows in the table
stay as the live A code wrote them.

**Rollback, cheapest first:** unset `REGISTER_ENABLED` (no redeploy; the register is then not written). The code
before the 15:15 close is version 8 (`HVmgAGjN…`, main 5eb3ed1) — a code-only rollback (`aws lambda get-function
--qualifier 8` gives its zip). Version 7 (`RaMJeVy2…`, Amar's gate without the one more look) is older still — same
table and columns, also code-only. Older code:
version 4 (no bias, before `derived_through_ts`) needs the column dropped or given a default first - it does not
name the column, and it is NOT NULL; versions 1–3 likewise. The table can stay - nothing reads it.

## It writes nothing but the register, and calls no broker API

The regime context arrives in the invocation payload: the snapshot, the daily
read. It does NOT hand over candles — this function reads its own bars from
Neon, because a playbook knows which bars it needs. That is why it carries
`neon-db-driver` and `neon-access`, and why its role can read
`/algo/neon/connection`.

There is still **no Dhan client and no token** — the only thing the manager
ever called Dhan for was a live price, and this playbook never reads it: a
sweep is confirmed by a *closed* bar reclaiming a level, so an unconfirmed live
tick is exactly what the setup must not act on. The reads are all SELECTs; the
one write is the alert register's upsert (above), when `REGISTER_ENABLED` is on.

`config.py` keeps its own `IST` constant and `levels.py` its own
`ist_datetime`/`ist_time`, from when this function held no layers at all;
`handler.py` takes `ist_midnight_epoch` from `neon-access` like everything
else. `error-notifier` is the one function here that genuinely carries no
layers.

The consequence worth knowing: **reading this function's log is how a session's
candidates are recovered.** There is no `signal` table — the alert register
(when on) records each alert and its trade for analysis, and nothing reads it. The manager's
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

Since 2026-11-14 every 5-min tick arrives here whatever the regime, so `gate.py`
plus the within-levels test in `session.py` is the only gate. **It uses no bias**
(since 2026-11-14 22:58) **and no trend test** (since 2026-11-15 09:33). Before that the manager's `regime|bias` registry
applied a coarse gate first.

| Check | Threshold | Source |
|---|---|---|
| Regime | `sideways` or `trending` — **not** `volatile-expansion` | volatility only (Amar 2026-11-15); `volatile-expansion` is the classifier's high-volatility read (the last 30 min ≥ 2× normal, or the VIX move). Until 2026-11-15 only `sideways` passed |
| Price within the levels | close between min(PDL, 15-min low) and max(PDH, 15-min high) | Amar 2026-11-15 — beyond all of them on one side, the day has broken out. A missing level fails |
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

### 3. The bias-adjusted target — REMOVED 2026-11-14

**Removed** with the rest of the sweep's bias (Amar, 2026-11-14): T2 is always the opposite side
of the opening range. It had never fired live — it compared the classifier's bias against
`"BULLISH"` / `"BEARISH"` while the classifier writes lower case, from 2026-09-13 on — and with
the comparison fixed it would have removed 3 winners and no losers on 41 sessions (10-01, 10-07,
10-19: the cluster target pulled risk-reward to 0.72–0.82, under the 1.5 floor). `sma100`, used
only here, went with it. The history below is kept for the record.

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
| `EXPECTED_CONTEXT_VERSION` | 3 | asserted against the manager (2 until 2026-11-19) |
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
| `ACCEPTANCE_CLOSES` | 2 | playbook |

The rest are not thresholds — they decide when this playbook runs at all, and
what it reads:

| Variable | Default | Source |
|---|---|---|
| `ALLOWED_REGIMES` | `sideways` | the gate. This is a range playbook; firing it on a trend day is the main way it loses money |
| `CANDLE_INTERVAL_MINUTES` | 5 | which candle table the bars come from |
| `HISTORY_5MIN_BARS` | 120 | how many 5-minute bars are read |
| `HISTORY_DAILY_BARS` | 40 | enough for the ATR period with room to spare |
| `ATR_PERIOD` | 14 | Wilder ATR, from the `market-classifier` layer |
| `ROW_VISIBLE_LAG_SECONDS` | 100 | a 15-min row is in force for a candle once written by the candle's close + this |
| `TELEGRAM_ENABLED` | `on` | kill switch: `off` logs every message as `WOULD SEND` instead of sending |
| `REGISTER_ENABLED` | unset (= off) | `on` writes the alert register to `algo.playbook_alerts` after the messages; unset again to stop without a redeploy |

## Deployment shape

Zip the twelve modules at the **zip root**. Handler `handler.lambda_handler`,
set under Code → Runtime settings → Edit. **Three layers:** `neon-db-driver`,
`neon-access` and `market-classifier`. **One environment variable is set:
`REGISTER_ENABLED=on`** — without it the register is not written (unset = off).
Every other tunable has a default in `config.py`. The register needs no IAM
change: it writes through the same `/algo/neon/connection` the reads use.

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

No orders, no position sizing, no strike selection, no database write other
than the alert register (a record nothing reads back), no recommendation to take the trade. The playbook is explicit: "This is analysis of
what price has already done."
