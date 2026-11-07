# pattern-recorder

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md)

**pattern-detector v2.** It runs the v2 pattern library on every live 15-min
snapshot and records what it recognises to `algo.pattern_events`. The patterns are
candle patterns at levels on the active future, with volume recorded and OI joined
at read time.

**Recognition only.** It sends no alerts, dispatches no strategy, and has no say in
`pattern-detector`'s own decisions.

| | |
|---|---|
| Status | **live since 2026-11-06 09:45 IST** (created 09:38:50, CodeSha256 `NWQC5zzSwXe73/uFYH+IcRTaNA9XKv5dBmcnwB4TMHg=`, role `pattern-recorder-role`; hand-off switched on 09:40:22). [Phase C plan](v2-phase-c-plan.md) |
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 6 modules (`handler`, `config`, `db`, `inputs`, `structure`, `patterns`) |
| Layers | `neon-db-driver` + `neon-access` |
| Schedule | **none.** `pattern-detector` invokes it (`Event`) as its **last** step, ≤25×/day, behind `PATTERN_RECORDER_FUNCTION_NAME` (unset = off) |
| Reads | `algo.candle_5min` (the active future's and the index's closed bars, 40 days back), `algo.intraday_fno_data` (option walls, each day's active contract, the previous run's `captured_at`) |
| Writes | `algo.pattern_events` only ([schema](schema.sql)). **Never** `session_observations` |
| Secrets | `/algo/neon/connection` |

## Where things are

| | |
|---|---|
| [v2-pattern-definitions.md](v2-pattern-definitions.md) | The pattern definitions: levels, candle shapes, the R1 / R2 / C1 / C2 families, volume, OI. Accepted 2026-11-05. |
| [v2-phase-a-replay.md](v2-phase-a-replay.md) | What the rules find over 88 stored sessions, descriptively: recall against the hand-logged rows, OI at read time, forward moves against a no-pattern baseline. |
| [v2-phase-c-plan.md](v2-phase-c-plan.md) | The plan this function was built from: decisions C1–C6. |
| [replay/](replay/README.md) | Local tooling, never deployed: logic checks, the read-only fetch, the replay, and the handler check (V1). |

## Why a separate function, invoked last

`pattern-detector` has no `EventInvokeConfig`, so Lambda retries a failed async
invocation of it twice.
- **Inside the detector,** a v2 failure after the existing branches would re-send
  their Telegram alerts and re-dispatch the manager on every retry.
- **Here, invoked last and asynchronously,** a failure stays in this function's own
  log group, and `error-notifier` reports it.
- **Its own retries are harmless:** the write is an idempotent upsert, and nothing
  is announced.

## How a run works

1. **Validate the payload.** It is the one `market-classifier` gave the detector:
   `fno` must carry `security_id`, `instrument_type`, `snapshot_ts`, `captured_at` and
   `fut_security_id`.
2. **Read, by epoch range only** (no timezone arithmetic in SQL), 40 calendar days
   back:
   - the 15-min snapshots;
   - the closed 5-min bars of every future those snapshots named;
   - the index's closed 5-min bars.
3. **Assemble the session with [`inputs.py`](inputs.py)**, the same code the replay
   uses:
   - today's closed bars of the active future, and the previous session's bars of the
     same contract;
   - the index for the same two sessions;
   - today's snapshots;
   - the same-clock bar of the previous 20 sessions, each on its own active contract.
4. **Detect and upsert.** `patterns.detect_session` re-derives **every** event of the
   session so far, and all of them are upserted in one transaction.
   - The events are a deterministic function of closed bars, so this is idempotent,
     and a missed run is filled by the next.
   - `first_seen_*` and `created_at` are kept on conflict.
5. **Fail loudly.**
   - The upsert must affect exactly one row per event, or it raises.
   - No bars for the active future raises.
   - Any read or write error raises.
6. **Log** one line per **fresh** event (trigger bar closed since the previous run's
   `captured_at`) and a per-run summary.
   - On a session's first run, the previous row is the previous day's last, so the
     morning is fresh even on a late start.

## Verified before deploy (2026-11-06)

- **Logic checks** (`replay/checks.py`): 32/32, including no look-ahead on 138
  simulated runs.
- **V1** (`replay/handler_check.py`): the real handler, on all 816 stored live runs
  (35 sessions), against a fake database serving the pulled data.
  - The table after each day's last run equals the replay's events field by field:
    1,063 rows.
  - Every run's fresh count matches.
  - Every row's `first_seen` is the run that first could read it.
- **V2:** `pattern-detector` with the hand-off, old code against new, on the same 816
  payloads.
  - With the env var unset, decisions and every outward call are identical: 29 turns,
    29 manager dispatches, 43 Telegram alerts.
  - With it set, the same, plus the hand-off as the last call on every run, carrying
    the exact payload.
- **V1b** (`replay/live_sql_check.py`): the real handler and pg8000 SQL against the live
  table, rolled back. 19 rows equal the replay, `ON CONFLICT` keeps `first_seen_*`, and
  the table was unchanged.

## Verified live (2026-11-06)

- **First run, the 09:45 snapshot:**
  - the detector logged "no turn", then "snapshot forwarded to pattern-recorder";
  - the recorder took 3.2 s including the cold start, with 0 errors, and wrote 2 rows:
    C1 up through `orh` 24540.0 at 09:30 on vol z 5.55 (rvol 3.6×), and a C2
    wick_reclaim at `orh` at 09:35 on a doji.
- **V3, the whole first day** (`replay/table_check.py 2026-11-06`, checked 2026-11-07):
  - 13 table rows equal 13 re-derived, field by field;
  - every `first_seen` is the earliest run that could read its candle closed;
  - 24 live invocations, one per detector run, with 0 errors.

## IAM

Its own execution role, `pattern-recorder-role`, not a borrowed one:
- `logs:CreateLogStream` / `logs:PutLogEvents` on **its own** log group;
- `ssm:GetParameter` on `/algo/neon/connection`, plus `kms:Decrypt` via SSM.

`pattern-detector-role` additionally needs `lambda:InvokeFunction` on **this
function's ARN**, not a wildcard. See CLAUDE.md on the silent borrowed-role trap.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `HISTORY_DAYS` | `40` | calendar days read back: the previous session plus 20 sessions for time-of-day volume |
| `FUTURES_INSTRUMENT_TYPE` | `FUTIDX` | the future's `instrument_type` in `candle_5min` |
| `UPSERT_BATCH_SIZE` | `500` | rows per INSERT (35 params a row; the Postgres cap is 65,535) |
| `V2_NEAR_PCT`, `V2_MIN_RANGE_PCT`, `V2_RECLAIM_MAX_BARS`, `V2_SWING_K`, `V2_VOLUME_LOOKBACK`, `V2_RVOL_SESSIONS`, `V2_PLAIN_CLOSE_FRACTION` | see the definitions | the pattern rules' settings, **all provisional**. Changing one changes what is found, so bump `patterns.RULES_VERSION` with it |

## Rollback, cheapest first

1. Unset `PATTERN_RECORDER_FUNCTION_NAME` on `pattern-detector`: recording stops at
   once, with no deploy.
2. Delete this function and its role.
3. `DROP TABLE algo.pattern_events`.
