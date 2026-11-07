# pattern-detector v2 — Phase C plan: run it live, record to a table

← [v2 definitions](v2-pattern-definitions.md) · [Phase A/B replay](v2-phase-a-replay.md)

**Status: APPROVED 2026-11-06 (Amar: "Approve as planned"; C1–C6 as recommended).**
- **Done 2026-11-06:**
  - steps 0–3, the code plus the local checks V1 and V2;
  - step 4: `algo.pattern_events` created on Amar's OK, after pre-checks (database
    `Algo`, table absent, `instrument_master.security_id` text). The live columns equal
    `db.COLUMNS` in name and order, the key equals `db.KEY`, it is empty, and none of
    the 2,214 replay events would violate a NOT NULL or CHECK.
  - **V1b** (`replay/live_sql_check.py`), the real handler plus pg8000 SQL against the live
    table, every transaction rolled back:
    - on the stored 11-04 15:25 run, live reads gave the same 19 rows as the replay;
    - the real INSERT and the row-count guard saw exactly 19;
    - `ON CONFLICT` kept `first_seen_*`/`created_at` and updated the rest;
    - `text[]` and numerics round-tripped;
    - the table was still 0 rows afterwards, confirmed independently.
  - The IAM drafts in `iam/` were diffed against the live `pattern-detector-role` and
    mirrored to its shape: the same trust policy, and the same three inline policies
    with the same Sids.
  - **Steps 5–8 done mid-session at Amar's instruction** ("implement now, don't wait for
    close"):
    - **step 5:** Amar created `pattern-recorder-role` and `invoke-pattern-recorder` from
      `iam/`; verified identical to the files.
    - **step 6:** created the log group (30 d) and error-notifier's
      `allow-logs-pattern-recorder` + `error-notifier` subscription filter. Created
      `pattern-recorder` at 09:38:50 IST, sha `NWQC5zzS…`, equal to the built zip. The
      `{}` smoke test raised the expected validation error, and error-notifier alerted
      Telegram.
    - **step 7:** the detector's code was deployed at 09:39 with the switch off, sha
      `1nNqeiLS…`, equal to the built zip. Rollback is `WU75ZJh7…`.
    - **step 8:** the switch was turned on at 09:40:22, with env vars merged
      (`STRATEGY_MANAGER_FUNCTION_NAME` kept).
  - **First live run (09:45 snapshot):**
    - the detector logged "no turn" and then forwarded;
    - the recorder took 3.2 s with 0 errors and wrote 2 rows: C1 up through `orh`
      24540.0 on vol z 5.55, and a C2 wick_reclaim at `orh`;
    - **V3 so far** (`replay/table_check.py`): the table equals an independent read-only
      re-derivation of the session, field by field, and first_seen is the 09:45 run.
  - **Step 9, the full day of 2026-11-06, checked 2026-11-07:**
    - **V3** (`table_check.py 2026-11-06`), up to the 15:25 run (captured 15:30:40): 13
      table rows, 13 re-derived, **ALL MATCH**. Every `first_seen` is the earliest run
      that could read its candle closed.
    - **Run counts** (CloudWatch):
      - `pattern-detector` 24, 0 errors;
      - `pattern-recorder` 25 = 24 live + the morning `{}` smoke test, which was its
        only error;
      - 0 throttles on any function. The chain's 25/25/24/24 (loader, sentiment,
        classifier, detector) is the same as on 11-04 and 11-05.
    - **error-notifier** is subscribed to the recorder's log group.
    - **The detector's decisions equal a replay on all 24 runs:** the 10 runs before
      the 12:07 baseline fix against the old zip, and the 14 after it against the new
      one. All were "no turn", with no alerts, and each forwarded to the recorder.
    - **Capture:**
      - 24 snapshots and 24 sentiments;
      - 75 index and 75 future candles;
      - 71 poller ticks, whose candle volumes all equal the loader's.
- **Waiting:**
  - **step 10:** the commit, **held by Amar** 2026-11-06.

**Goal.** v2 runs on every live 15-min snapshot and records what it recognises to a new
table, so live sessions accumulate the same events the replay produces. **Still no
alerts and no manager dispatch from v2** (B5). Today's detector (the TURN gate,
continuation alerts, Telegram) keeps behaving exactly as it does now.

---

## 1. Found while planning (measured 2026-11-05)

- **B3 needs no code. The "35 unannounced events" were a fault in my replay, not in
  live.**
  - On the first row of every day, `intraday_fno_data.prev_snapshot_ts` points at the
    previous day's 15:25 row. So the first run's freshness window starts at yesterday's
    last run, and everything since is fresh.
  - Even 09-30 (first run 12:00) and 10-08 (12:30) would have announced the morning's
    events, late.
  - The replay simulated the first run as "one interval back". Step 0 fixes the replay
    and the report.
- **The detector has no async-retry config, so Lambda's default of 2 retries applies.**
  The classifier invokes it with `Event`. If v2 ran inside the detector and failed after
  the existing branches, each retry would re-send their Telegram alerts and re-dispatch
  the manager. This is why placement is decision C1.
- **The detector's limits are ample:** 120 s timeout, 256 MB, layers neon-db-driver:1 +
  neon-access:1. v2 adds about 0.1–0.3 s of compute and a few small reads.

## 2. Decisions (recommendation first)

| # | Decision | Options | Recommendation |
|---|---|---|---|
| **C1** | **Where v2 runs** (architecture) | **A** inside `pattern-detector` · **B** a new function, invoked async by `pattern-detector` as its last step · **C** a new function, invoked by `market-classifier` alongside the detector | **B.** See below. |
| C2 | Name of the new function (if B or C) | `pattern-recorder` · something else | **`pattern-recorder`.** It records; it doesn't gate or alert. |
| C3 | Table schema | the draft in section 3 | **As drafted**, including `rules_version` in the key. |
| C4 | What each run writes | **A** every event of the session so far, upserted · **B** only fresh events | **A.** The events are deterministic, so re-writing is idempotent and a missed run is filled by the next. |
| C5 | Backfill history into the table | none · replay the 76 clean sessions in | **None.** The replay reproduces history any time; the table starts with the first live session. |
| C6 | Docs outside `pattern-detector/` (root README, `docs/architecture.md`, `components.md`, `flow.md`, the runbook) | edit them in this change · list them as a follow-up | **Edit them in this change, with your OK.** A new component and a new table make every chain diagram stale. Your one-function rule says to ask first. |

**C1 in more detail:**

| | A: inside the detector | **B: new function, the detector invokes it** | C: new function, the classifier invokes it |
|---|---|---|---|
| Effect on today's detector | v2 code in its handler; a v2 failure fails the whole invocation | one guarded async invoke **after all existing work**; unset env var = off | none |
| A v2 failure | either 2 retries (duplicate alerts and dispatches) or retries set to 0 (a transient failure loses existing alerts) | isolated in its own log group; its retries are harmless (idempotent upsert, no alerts) | isolated |
| New AWS pieces | none | function, execution role (you, by hand), invoke permission on the detector's role, error-notifier subscription | the same, but the permission goes on the classifier's role |
| Functions changed | detector | detector (one invoke) + new | classifier + new |
| Fits the repo's shape | no (one job, one role) | yes, like the poller and the detector itself | yes |

**B costs more setup but keeps v2 from changing how the live gate behaves. That matters
more than setup while v2 is observation-only.**
- Under B, v2's code moves to `pattern-recorder/` with `git mv`: the two library
  modules, `replay/`, and these three docs.
- `pattern-detector/` goes back to its six modules plus a small `recorder.py` (the
  invoke).

## 3. Table draft — `algo.pattern_events` (C3)

Neon `nameless-mountain-15353651` / `Algo` / `algo`. Epoch-second `bigint` times,
`security_id` text, FK to `instrument_master`.
- **No OI columns.** OI is joined at read time, since the trigger bar's 5-min OI is never
  written when the detector reads: 230 of 230 quarter-hours.
- **No outcome columns.** Forward moves are computed at read time (patterns before
  exits).
- **Never** `session_observations`, which stays hand-logged.

```sql
CREATE TABLE algo.pattern_events (
    security_id            text    NOT NULL,   -- the underlying ('13')
    instrument_type        text    NOT NULL,   -- 'INDEX'
    rules_version          text    NOT NULL,   -- patterns.RULES_VERSION, e.g. 'v2.0'
    trigger_ts             bigint  NOT NULL,   -- the 5-min bar that completed the pattern
    family                 text    NOT NULL,   -- R1 | R2 | C1 | C2
    subtype                text    NOT NULL,   -- plain | wick | close_beyond | dip_reclaim | touch_hold | wick_reclaim | none
    direction              text    NOT NULL,   -- up | down
    fut_security_id        text    NOT NULL,   -- the future whose bars were read (FUTIDX)
    first_ts               bigint  NOT NULL,   -- the pattern's first bar
    trigger_close          numeric NOT NULL,   -- future close of the trigger bar
    extreme                numeric NOT NULL,
    level                  text    NOT NULL,   -- pdh | pdl | orh | orl | swing_high | swing_low | call_wall | put_wall | max_pain
    level_price            numeric NOT NULL,   -- in future prices
    level_strike           numeric,            -- walls / max pain only: the index strike
    distance_pts           numeric NOT NULL,
    distance_pct           numeric NOT NULL,
    other_levels           text[]  NOT NULL,
    candles                text[]  NOT NULL,
    prior_tests            integer,
    chart_pattern          text,               -- double_bottom | triple_top | ...
    structure_15m          text    NOT NULL,   -- up | down | mixed | none
    prior_break_direction  text,
    prior_break_ts         bigint,
    vol_z                  numeric,
    vol_z_max              numeric,
    rvol_tod               numeric,
    retest_vol_ratio       numeric,
    sweep_depth_pct        numeric,
    bars_beyond            integer,
    break_strength         numeric,
    index_twin_offset_pct  numeric,
    first_seen_snapshot_ts bigint  NOT NULL,   -- the run that first wrote it (kept on upsert)
    first_seen_at          bigint  NOT NULL,   -- that run's captured_at
    created_at             bigint  NOT NULL,
    updated_at             bigint  NOT NULL,
    PRIMARY KEY (security_id, instrument_type, rules_version, trigger_ts, family, subtype, direction),
    FOREIGN KEY (security_id, instrument_type) REFERENCES algo.instrument_master (security_id, instrument_type),
    CONSTRAINT pattern_events_family    CHECK (family IN ('R1', 'R2', 'C1', 'C2')),
    CONSTRAINT pattern_events_direction CHECK (direction IN ('up', 'down'))
);
-- ROLLBACK: DROP TABLE algo.pattern_events;
```

- **Why `rules_version` is in the key:** when the definitions change, the new rules
  write new rows beside the old ones instead of overwriting them, and a session can be
  replayed under either.
- **Size:** about 25 rows a session, roughly 6,000 a year.

## 4. How a run works (under C1 = B)

1. `pattern-detector` finishes everything it does today. As its **last** step, if
   `PATTERN_RECORDER_FUNCTION_NAME` is set, it invokes `pattern-recorder` with `Event`
   and the payload it received. Unset means off, the same pattern as the manager gate.
2. `pattern-recorder` reads, all keyed by epoch range, with no timezone arithmetic in SQL:
   - the active future's closed 5-min bars for today and the previous session of the
     same contract;
   - the index's bars for the same two sessions;
   - today's `intraday_fno_data` rows;
   - the same-clock bars of the previous 20 sessions' active contracts, for `rvol_tod`.
3. It runs `detect_session` and upserts **every** event of the session so far (C4).
   `first_seen_*` is kept on conflict.
4. **Fail loudly:**
   - The upsert's row count must equal the number of events, or it raises: no silent
     0-row write.
   - Any read or write error raises, and error-notifier reports it from the function's
     own log group.
5. It logs one line per fresh event (trigger bar closed since the previous run) and a
   per-run summary.

**One assembly path.** Building a session's inputs (contract, previous session, rvol
history) moves into a shared pure module that both the Lambda and `replay/` call. The
Lambda feeds it SQL rows and the replay feeds it the pulled JSON, so "live == replay"
is checked on the same code rather than two copies of it.

## 5. Steps (after approval; each AWS/Neon change only at its step)

| Step | What | Who |
|---|---|---|
| 0 | Fix the replay's first-run freshness (use the previous day's last run). Re-run and correct the report. Add `RULES_VERSION` to `patterns.py`. | me |
| 1 | If B: `git mv` the v2 files to `pattern-recorder/`. Write its `handler.py`, `db.py`, `config.py`, the shared assembly module, `schema.sql` and README. Add `recorder.py` + one call to `pattern-detector`. | me |
| 2 | Local **V1**: run the new handler over the 35 sessions with stored run clocks, using a fake DB fed from `replay/data`. The rows it would upsert after each day's last run must equal the replay's events exactly, and its per-run fresh logs must equal the replay's. | me |
| 3 | Local **V2**: `pattern-detector` with the added invoke. Its decisions on stored payloads must be unchanged. With the env var unset nothing is invoked; with it set, a fake client sees the exact payload, after all existing work. | me |
| 4 | Apply `schema.sql` to Neon, after confirming the project id and checking the table doesn't exist. | me, on your OK |
| 5 | Create `pattern-recorder-role` (logs on its own group; `ssm:GetParameter` on `/algo/neon/connection` + `kms:Decrypt` via SSM). Add `lambda:InvokeFunction` on the new ARN to `pattern-detector-role`. I'll give the exact JSON. | **you, by hand** |
| 6 | Create the function (py3.14, the same two layers, 120 s, 256 MB), its log group (30-day retention) and the error-notifier subscription. Smoke test with `{}`: expect its own validation error, not an ImportError. | me, local aws CLI |
| 7 | Deploy `pattern-detector` with the env var **unset**: behaviour identical, and the new zip's hash recorded. | me, after close |
| 8 | Set `PATTERN_RECORDER_FUNCTION_NAME=pattern-recorder` on the detector: on from the next session. | me, on your OK |
| 9 | Live **V3**, after the first session's close (detailed below). | me |
| 10 | Commit on `pattern-detector-v2`. Push or merge only on your word. | me |

**Step 9 checks:**
- the day's table rows equal a fresh replay of the day;
- invocations 24/24 (or 25), 0 errors, error-notifier subscribed;
- the detector's decisions are unchanged against a replay.

**Rollback**, cheapest first:
1. Unset the env var: v2 stops at once, with no deploy.
2. Put the detector's zip back to `WU75ZJh7…` (`f17e46c`).
3. Delete `pattern-recorder` and its role.
4. `DROP TABLE algo.pattern_events`.

## 6. Not in Phase C

- Alerts or manager dispatch from v2.
- A 5-min cadence.
- Threshold changes.
- Any change to the existing detection code.
- A backfill (C5).
- ML.
- Anything in the loader, poller, sentiment or classifier.
