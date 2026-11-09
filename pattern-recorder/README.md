# pattern-recorder

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md)

**pattern-detector v2.** Every **5 minutes** it runs the v2 pattern library on the
active future and records what it recognises to `algo.pattern_events`. The patterns
are candle patterns at levels, with volume recorded and OI joined at read time.

**v2.1 (plan v3, step 4, LIVE 2026-11-09).** The events that pass the alert rule go to
**Telegram and strategy-manager, once each**. With `ALERTS_ENABLED` unset it runs in
**shadow**: it decides the alerts and logs them as `WOULD SEND`, and sends nothing.

| | |
|---|---|
| Status | **v2.1 LIVE, sending, since 2026-11-09 16:04:08 IST** (`ALERTS_ENABLED=on` and `STRATEGY_MANAGER_FUNCTION_NAME=strategy-manager` set after the close; the first live tick is Wed 2026-11-11 09:15, since 11-10 is a holiday). Code since 13:50:40 IST: CodeSha256 `t7TcsOAiqAMvNjnfXnyCdrIZSqa0DtYzouMieDs9e9s=`, which ran in shadow 13:56 → 15:31 that day. The previous v2.0 code, `NWQC5zzS…`, ran 2026-11-06 09:45 → 2026-11-09 13:45 on pattern-detector's 15-min hand-off. intraday-fno-poller's hand-off switched on at 13:52:21. Redeployed 2026-11-09 22:34 IST, CodeSha256 `iyIJsEeU…` (previous `t7TcsOAi…`): the manager payload adds today's closed index candles (`index_bars`) for strategy-trend-pullback; detection unchanged. See [Switching to v2.1](#switching-to-v21) |
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 11 modules (`handler`, `config`, `db`, `dhan`, `params`, `inputs`, `structure`, `patterns`, `alerts`, `notify`, `dispatch`) |
| Layers | `neon-db-driver` + `neon-access` |
| Schedule | **none.** `intraday-fno-poller` invokes it (`Event`) as its **last** step, 71×/day, behind its `PATTERN_RECORDER_FUNCTION_NAME` (unset = off) |
| Reads | Dhan: today's index and future 5-min candles, in memory, never saved. Neon: earlier sessions' `candle_5min`; `intraday_fno_data` (walls, each day's contract, the latest 15-min snapshot); `intraday_sentiments` (`day_trend`, regime, bias); `fno_5m_snapshot` (the previous tick, each bar's OI change); `daily_market_sentiment` (the daily read for the manager) |
| Writes | `algo.pattern_events` only ([schema](schema.sql)), including its `telegram_sent_at` / `manager_sent_at` marks. **Never** `session_observations` |
| Sends | Telegram (`/algo/telegram/brief`, the same chat as everything else), and strategy-manager (`Event`) |
| Secrets | `/algo/neon/connection`, `/algo/dhan/token`, `/algo/telegram/brief` |

## Where things are

| | |
|---|---|
| [v2-pattern-definitions.md](v2-pattern-definitions.md) | The pattern definitions: levels, candle shapes, the R1 / R2 / C1 / C2 families, volume, OI, and (section 13) v2.1's levels and alert rule. |
| [v2-phase-a-replay.md](v2-phase-a-replay.md) | What the rules find over 88 stored sessions, descriptively: recall against the hand-logged rows, OI at read time, forward moves against a no-pattern baseline. |
| [v2-phase-c-plan.md](v2-phase-c-plan.md) | The plan v2.0 was built from: decisions C1–C6. |
| [replay/](replay/README.md) | Local tooling, never deployed: logic checks, the read-only fetch, the replay, the handler check (V1), the live SQL check (V1b) and the live table check (V3). |

## The flow (v2.1)

```
every 5 min:  intraday-fno-poller ─(writes its 5-min row, then)─Event─► pattern-recorder v2.1
                 today's index + future candles from Dhan · history from candle_5min
                 latest 15-min row (day_trend, regime|bias, walls) · daily read
                 → upsert algo.pattern_events (rules v2.1)
                 → alert = new event at PDH/PDL/ORH/ORL that fits day_trend
                 → Telegram + strategy-manager, once each (claim, send, mark)
                   (the manager payload carries today's closed index candles as
                   `index_bars` since 2026-11-09, for strategy-trend-pullback)

every 15 min: loader → sentiment → market-classifier (day_trend) → pattern-detector (v1, log only)
```

**Timing.** The poller's row lands 99 s after a candle closes (p50; max 107 s over 663
ticks, 10-27..11-09), and this run takes a few seconds more. So an alert reaches the
phone about **2 minutes after its candle closes**, against a median of 6 minutes on the
15-min chain.

## How a run works

1. **Validate the payload:** the poller's `tick` (`security_id`, `instrument_type`,
   `snapshot_ts`, `captured_at`, `fut_security_id`), plus its `index` and `future`
   instruments.
   - pattern-detector's old v2.0 hand-off is **refused by name**.
2. **Today's candles from Dhan**, both instruments.
   - The window starts at 00:00, because `fromDate` is exclusive.
   - Calls are paced 4 s apart, with the DH-904 backoff.
   - **Closed candles only**: `ts + 300 <= captured_at`, the tick's own clock.
3. **History from Neon, by epoch range only**, 40 calendar days back:
   - earlier sessions' `candle_5min`;
   - the 15-min snapshots **written by the tick's clock** (`captured_at <=`), so a retry
     minutes later sees the same inputs.
4. **Assemble the session with [`inputs.py`](inputs.py)**, the same code the replay uses.
   It **raises** without a previous index session (no PDH/PDL), or once 09:30 has passed
   without the three opening-range index candles (no ORH/ORL).
5. **Detect and upsert** every event of the session so far, in one transaction.
   - This is idempotent, and `first_seen_*` and `created_at` are kept on conflict.
   - The sent marks are not in the upsert's column list, so it can never clear one.
6. **Fresh events** are those whose trigger candle closed since the **previous tick's**
   `captured_at`, on any day.
   - So the first tick of a session announces the morning.
   - A missed tick widens the next window instead of losing it.
7. **Alerts** ([`alerts.py`](alerts.py)): fresh events at PDH/PDL/ORH/ORL that fit the
   `day_trend` of the latest 15-min row this tick can read.

   | `day_trend` | Alerts on |
   |---|---|
   | `up` | up patterns |
   | `down` | down patterns |
   | `none`, or no row yet (before 09:45) | R1 rejections and R2 sweeps, both ways |

   - An event is judged once, on its first tick, and never re-judged.
   - Volume and OI go in the message as information, never as a gate.
8. **Send once** (`db.claim_and_mark`), Telegram first and then the manager:
   - lock the event's row only if its mark is still NULL (`FOR UPDATE SKIP LOCKED`);
   - send;
   - set the mark and commit.
   - A failed send rolls back and raises, so Lambda's retry sends it again.
   - Before the day's first 15-min row there is nothing to route on, so that alert goes to
     Telegram only.
9. **Fail loudly:** a read, Dhan, upsert or send error raises.

**Replayed** (`replay/out/v21_replay.md`, 90 sessions): 7.5 alerts a day (2.0 from the
trend branch, 5.5 from the no-trend branch), and 3 of the 9 hand-logged trades caught.

## Verified before deploy (2026-11-09)

- **Library:** v2.1 events equal the v2.1 replay's (the replay's monkeypatch on the v2.0
  library) on all 90 sessions, all 2,304 events, field by field.
- **Logic checks** (`replay/checks.py`): 32/32.
  - Two are new for v2.1: an index level priced on the future through a 50-point basis,
    and no key levels without index candles.
  - No look-ahead on 426 simulated 5-min runs.
- **V1** (`replay/handler_check.py`): the real handler over all **1,040** stored poller
  ticks (15 sessions, 2026-10-19..11-09). Dhan and Neon are faked from the pulled data,
  and each historical 15-min row gets the `day_trend` market-classifier's `trend.py`
  would have written. Six checks, all pass on every session:
  - the table equals the library;
  - every `first_seen` is the first tick that could read the candle;
  - Telegram got exactly the expected alerts, once each: 101 alerts, 6.7/day;
  - the manager got them once each (88), and none before the first 15-min row;
  - a re-run sends nothing;
  - an injected send failure raises and leaves no mark, and the re-run sends it once.
- **Per-day alert counts** equal the replay's own method (T1 computed from candles on the
  15-min run grid) on all 15 days.
- **V1b** (`replay/live_sql_check.py`): the real handler and SQL against the **live**
  tables, in one rolled-back transaction, on 2026-11-09's 09:50 tick.
  - Its rows and alert equal the library and the rule.
  - 1 Telegram mark for 1 send, and 1 manager mark for 1 invoke.
  - The re-run sent nothing.
  - `ON CONFLICT` kept `first_seen_*` and the marks.
  - The table was unchanged afterwards.

## Verified live (2026-11-09, shadow)

- **Smoke test** on the real 13:45 tick (synchronous invoke, the poller's exact payload):
  HTTP 200, 8.0 s, the Dhan token read under the new grant, 2 Dhan calls, day trend
  `down`, 45 v2.1 rows, nothing sent.
- **First handed-off ticks**, 13:55 and 14:00:
  - the poller logged "tick handed to pattern-recorder";
  - the recorder ran in 7.8 s, in shadow, with 0 errors.
- **pattern-detector's 14:00 run** on the new code: "no turn", then "no
  PATTERN_RECORDER_FUNCTION_NAME set". The v2.0 hand-off is off.
- **0 errors** on the detector, recorder, poller and classifier since 13:50.
- **V3** (`table_check.py 2026-11-09`, up to the 14:00 tick):
  - 46 table rows equal 46 re-derived, field by field, and none were sent early;
  - every first-seen mismatch is the mid-session start: 45 rows first written by the
    13:46:39 smoke test, and 1 whose tick (13:51) ran before the hand-off was on.

## Switching to v2.1

The order matters: each step only takes effect once the previous one is in place.

Steps 1-4 done 2026-11-09 (IAM by Amar, verified identical to the files; the detector at
13:50:10, `CBczFaGk…`; this function at 13:50:40; the poller at 13:52:21, `MmdsHFWX…`).
**Step 5 was skipped:** after the close, Amar chose to go live without a full shadow
session. The shadow ran 13:56 → 15:31 on 11-09 only. **Step 6 done at 16:04 IST:** this
function at 16:04:08, the detector at 16:04:11.

1. **IAM, by hand** ([iam/](iam/README.md)):
   - add `/algo/dhan/token` and `/algo/telegram/brief` to the recorder's `algo-ssm-read`;
   - add `invoke-strategy-manager` to `pattern-recorder-role`;
   - add `invoke-pattern-recorder` to `intraday-fno-poller-role`.
2. **pattern-detector:**
   - set `TELEGRAM_ENABLED=on` (the old code ignores it);
   - deploy its new zip (Telegram switch);
   - unset `PATTERN_RECORDER_FUNCTION_NAME`, ending the v2.0 hand-off.
3. **Deploy this zip** (v2.1) with `ALERTS_ENABLED` unset, i.e. shadow.
4. **intraday-fno-poller:**
   - deploy its zip (the hand-off);
   - set `PATTERN_RECORDER_FUNCTION_NAME=pattern-recorder`.
   From the next tick, v2.1 runs every 5 minutes, in shadow.
5. **After one shadow session** (skipped, see above):
   - `table_check.py` (V3) for the day;
   - the `WOULD SEND` lines against the replay rule and v1's alerts.
6. **Go live** (done 2026-11-09 16:04):
   - here, set `ALERTS_ENABLED=on` and `STRATEGY_MANAGER_FUNCTION_NAME=strategy-manager`;
   - on pattern-detector, unset `TELEGRAM_ENABLED` and `STRATEGY_MANAGER_FUNCTION_NAME`
     (v1 becomes log-only).

## IAM

Its own execution role, `pattern-recorder-role`, not a borrowed one:
- `logs:CreateLogStream` / `logs:PutLogEvents` on **its own** log group;
- `ssm:GetParameter` on `/algo/neon/connection`, `/algo/dhan/token` and
  `/algo/telegram/brief`, plus `kms:Decrypt` via SSM;
- `lambda:InvokeFunction` on `strategy-manager`'s ARN only.

`intraday-fno-poller-role` needs `lambda:InvokeFunction` on **this function's ARN**, not
a wildcard. See CLAUDE.md on the silent borrowed-role trap.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `ALERTS_ENABLED` | *(unset)* | **unset = shadow**: alerts are decided and logged `WOULD SEND`, nothing sent, no mark. `on` sends |
| `STRATEGY_MANAGER_FUNCTION_NAME` | *(unset)* | unset = Telegram only. Set to `strategy-manager` to dispatch each alert once |
| `TELEGRAM_PARAMETER_NAME` | `/algo/telegram/brief` | `bot_token` + `chat_id`, shared with the daily brief, error-notifier and pattern-detector |
| `TELEGRAM_TIMEOUT_SECONDS` | `8` | Telegram call timeout |
| `TOKEN_PARAMETER_NAME` | `/algo/dhan/token` | the Dhan token auth-dhan-broker writes |
| `API_PACING_SECONDS` | `4.0` | spacing between Dhan calls (the per-account limit) |
| `HTTP_TIMEOUT_SECONDS` | `60` | Dhan call timeout |
| `HISTORY_DAYS` | `40` | calendar days read back: the previous session plus 20 sessions for time-of-day volume |
| `FUTURES_INSTRUMENT_TYPE` | `FUTIDX` | the future's `instrument_type` in `candle_5min` |
| `UPSERT_BATCH_SIZE` | `500` | rows per INSERT (35 params a row; the Postgres cap is 65,535) |
| `V2_NEAR_PCT`, `V2_MIN_RANGE_PCT`, `V2_RECLAIM_MAX_BARS`, `V2_SWING_K`, `V2_VOLUME_LOOKBACK`, `V2_RVOL_SESSIONS`, `V2_PLAIN_CLOSE_FRACTION` | see the definitions | the pattern rules' settings, **all provisional**. Changing one changes what is found, so bump `patterns.RULES_VERSION` with it |

## Rollback, cheapest first

1. Unset `ALERTS_ENABLED` here: back to shadow at once, no deploy. Unset
   `STRATEGY_MANAGER_FUNCTION_NAME` alone to stop only the dispatches.
2. Unset `PATTERN_RECORDER_FUNCTION_NAME` on `intraday-fno-poller`: v2.1 stops running.
3. Back to v2.0:
   - redeploy the v2.0 zip `NWQC5zzS…` (commit `00b09bf`);
   - set the detector's `PATTERN_RECORDER_FUNCTION_NAME` again;
   - set its `TELEGRAM_ENABLED=on` and `STRATEGY_MANAGER_FUNCTION_NAME` again, so v1 sends
     as before.
4. Delete this function and its role; `DROP TABLE algo.pattern_events`.

v2.0 and v2.1 rows sit side by side in the table (`rules_version` is in the key), so
nothing is lost either way.
