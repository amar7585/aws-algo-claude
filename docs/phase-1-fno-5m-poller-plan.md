# Phase 1 plan — 5-minute F&O poller (store only)

← [Back to root README](../README.md) · [Architecture](architecture.md) · [Flow charts](flow.md) · [Components](components.md)

**Status: APPROVED 2026-10-16. Code written and locally verified; schema APPLIED 2026-10-16
(project `nameless-mountain-15353651` / `Algo`, columns verified against the code); NOT
deployed** — no AWS resource exists yet. See
[intraday-fno-poller](../intraday-fno-poller/README.md).

Decisions taken at approval: **D1 = offset +1 minute** (not +2, as recommended); D2 VIX out,
D3 names as written, D4 `pcr_volume` + leg volumes only, D5 duplicate `chain.py`/`expiry.py` —
all as recommended.

**Where the build departs from this plan** (small, flagged for review):

- **8 modules, not 7:** a pure `reading.py` (closed bar vs forming bar, deltas, roll guard)
  was split out of the handler so it can be exercised without a database.
- **The first tick of a session has NULL deltas.** The plan said only "previous 5-min row".
  The slow row's baseline reaches back across the overnight gap; here it is restricted to the
  **same IST day**, because a "five-minute" delta that silently covers sixteen hours is a trap.
  One line in `db.previous_tick` to reverse.
- **Schedule is +1 minute**, so the cron strings below are the +1 variants.

## Goal

Capture the futures and option-chain readings **every 5 minutes** alongside the existing
15-minute snapshot, so a 5-minute reading can later be compared against a 15-minute
reference (volume, OI, PCR) at key levels (opening range, PDH/PDL, OI walls / max pain).

Dhan serves only a **live** option chain, so any 5-minute reading not taken today is
unrecoverable. That is the reason to start storing before any consumer exists.

**Phase 1 stores only. Nothing reads the new tables.** The 15-minute chain
(loader → sentiment → classifier → detector → manager → playbook) is untouched.

## Not doing in this phase

- No change to `intraday-data-loader`, `intraday-market-sentiment`, `market-classifier`,
  `pattern-detector`, `strategy-manager`, or the playbook.
- No detector or playbook reading the new data (phase 2).
- No thresholds, gates, signals table or Telegram alert (phases 2–3).
- No SMA/RSI, breadth, structure or regime on the fast row — that stays on the 15-minute row.
- No order placement.
- No backfill. (A one-off futures per-bar OI backfill via Dhan REST is a separate, optional
  step; it needs a fresh token from Amar.)
- No change to `candle_5min` (it has no OI column and keeps none).

## Shape

New Lambda **`intraday-fno-poller`**, its own EventBridge schedule, no invokes in or out.

```
EventBridge (every 5 min, 09:35-15:30)
  -> intraday-fno-poller
       reads : /algo/dhan/token, /algo/neon/connection, instrument_master,
               its own previous row
       calls : Dhan - index 5-min bars, the future's 5-min bars with OI,
               expiry list (cached per day), nearest + monthly option chain
       writes: algo.fno_5m_snapshot + algo.fno_5m_leg  (one transaction)
```

Pure Python (stdlib + `pg8000` from the existing layer) — satisfies hard rule 1. Layers:
`neon-db-driver` + `neon-access`. No classifier layer.

## Tables (additive migration; nothing existing is altered)

Both keyed on the instrument-identity convention; all times epoch seconds; `security_id`
is `text`.

### `algo.fno_5m_snapshot` — one row per tick, ~72 a session

Key `(security_id, instrument_type, snapshot_ts)`, FK to `instrument_master`.

| Group | Columns |
|---|---|
| Grain | `snapshot_ts` (newest bar Dhan returned, forming, as on the slow row), `captured_at`, `prev_snapshot_ts` (previous 5-min row) |
| Index | `spot` (live), `chain_spot`, `spot_change_pct`, `idx_bar_ts` (last **closed** bar), `idx_bar_volume` |
| Future | `fut_security_id`, `fut_symbol`, `fut_price`, `fut_oi` (live), `basis`, `basis_pct`, `fut_price_change_pct`, `fut_oi_change`, `fut_oi_change_pct` (vs previous row, null across a contract roll), **`fut_bar_ts`, `fut_bar_volume`, `fut_bar_oi`, `fut_bar_oi_change`** (last closed bar, and its change vs the bar before it, both taken from Dhan's **same response**, so they do not depend on the previous row existing) |
| Nearest / monthly expiry | The **same `near_*` / `mth_*` aggregate columns as `intraday_fno_data`** (identical names, so one query joins both grains): ATM, LTPs, straddle, PCR (OI and volume), CE/PE OI totals and `_change_pct`, max-OI strikes, max pain, IV and skew. Plus absolute `near_ce_oi_change`, `near_pe_oi_change`, `mth_ce_oi_change`, `mth_pe_oi_change`, null across an expiry roll |
| Housekeeping | `created_at` |

Per-bar `fut_bar_oi_change` is the signal the logged observations turned on (OI rising at a
low = shorts pressing; falling on the thrust = short covering). It is stored as an absolute
number, not only a percentage.

**No reference pointer is stored.** The 15-minute reference is joined at read time —
`MAX(snapshot_ts) <= tick` on `intraday_fno_data` — rather than written, so the poller does
not depend on the slow chain's timing. 15-minute deltas are likewise derived (the row three
ticks back).

### `algo.fno_5m_leg` — raw legs, nearest expiry, ATM ±2 → 10 rows a tick

Same columns and key as `algo.option_chain_snapshot`. A **separate table**, not extra rows
in that one: both writers would otherwise upsert the same `snapshot_ts` key at the 15-minute
marks (last writer wins), and any reader assuming "10 legs per snapshot" would see 30.
IV and greeks are `NULL` where Dhan sent `0`, as today.

Volume: ~72 rows + 720 legs a session, a few tens of MB a year.

## Schedule

Three rules, `Asia/Kolkata`, flexible window off, **72 runs a day**, created **DISABLED**,
**one minute past each five-minute mark (D1)**:

```
intraday-fno-poller-open      cron(36,41,46,51,56 9 ? * MON-FRI *)        5 runs   09:36-09:56
intraday-fno-poller-session   cron(1/5 10-14 ? * MON-FRI *)             60 runs   10:01-14:56
intraday-fno-poller-close     cron(1,6,11,16,21,26,31 15 ? * MON-FRI *)   7 runs   15:01-15:31
```

The last run (15:31) stamps the 15:25 bar, the day's final one, so no 15:35 sweep is needed.
A single `cron(1/5 10-15 …)` would run past the close, as with the loader.

Created disabled on purpose: `auth-dhan-broker` **enables** schedules after minting the token
and **disables** them on a holiday, so they come alive on the first trading morning after
the gate is extended. Handler also keeps the weekend and outside-session guards.

## Handler flow

1. Weekend / outside-session guard (09:35-15:30). Read the token record (raises if expired).
2. Resolve NIFTY (`13`/`INDEX`) from `instrument_master` on the **full identity**.
3. Fetch the index's and the future's bars for **today only** (no 10-day history). The future
   is fetched with `oi: true`; **raise** if OI is absent.
4. `snapshot_ts` = newest bar; align the future to it, log how stale a fallback is.
5. Expiry list (cached per IST date) → nearest + monthly; resolve the current-month future by
   symbol (`NIFTY-<MON><YYYY>-FUT`). Both chains centred on the **same** spot.
6. Read the previous 5-min row **on the same IST day** for the baseline (the first tick of a
   session has none, so its deltas are null); null the future and OI deltas across a
   contract/expiry roll (same guards as the slow function).
7. Write the row and legs in **one transaction**, upsert by key (re-run overwrites). Missing
   columns raise rather than writing a partial row.

Raises on any failure (never a `statusCode` shape). No invoke, so nothing downstream can be
failed by it and it cannot be failed by anything downstream.

## Packaging

`handler.py, config.py, params.py, dhan.py, expiry.py, chain.py, reading.py, db.py` — all but
`reading.py` copied from `intraday-market-sentiment` and trimmed, per the repo's
duplicate-rather-than-share convention for the zip root. Known cost: `chain.py` and `expiry.py` now live in two places,
and a fix in one must be carried to the other. Moving them into a layer is a separate
decision (republishing a layer means repointing every pinned function ARN).

Python 3.14, 256 MB (to be measured), timeout 120 s (absorbs Dhan's `DH-904` backoff of 56 s).

## AWS and IAM — what is new

| Item | Detail |
|---|---|
| Execution role | `intraday-fno-poller-role`: logs on **its own** log group only; `ssm:GetParameter` on `/algo/dhan/token` and `/algo/neon/connection`, `kms:Decrypt` via SSM |
| Scheduler role | `EventBridge-Scheduler-intraday-fno-poller`: `lambda:InvokeFunction` on this function's ARN only |
| Log subscription | `error-notifier` subscribed to the new log group |
| Holiday gate | the three schedule names added to `MANAGED_SCHEDULE_NAMES` (last in the list), their ARNs to the `ToggleSessionSchedules` statement of `holiday-gate-access`, **and** the new Scheduler role to its `PassScheduleTargetRoles` statement — **three edits** (the plan originally said two; `UpdateSchedule` re-passes each schedule's own role, and this one is new). Done and verified 2026-10-16 |

Both existing roles are single-function scoped and fail **silently** if borrowed, so this
function gets its own pair.

## Order of work

1. **Review this plan**; settle the decisions below.
2. Code and `schema.sql`, README, local verification against captured payloads (stubbed
   `pg8000`, fake connection, real Dhan payloads — not a synthetic fixture).
3. Apply `schema.sql` to Neon (**confirm project id `nameless-mountain-15353651` first**).
4. Create the function, layers and **disabled** schedules; supply the exact policy documents.
5. **Amar, by hand:** the two IAM roles, the `holiday-gate-access` ARNs and the
   `MANAGED_SCHEDULE_NAMES` env var (these edits are blocked for Claude).
6. Subscribe `error-notifier`; invoke once with a `now` override as a smoke test.
7. First trading morning: the 08:00 gate enables the schedules. Watch the session.
8. Docs: component README, root README status row, `docs/components.md`, `docs/flow.md`,
   the `auth-dhan-broker` README and its config default.

## Acceptance checks

- ~72 rows per session spaced exactly 300 s; 10 legs per tick; no `0` in any IV/greek column.
- At a 15-minute mark, the fast row and the slow `intraday_fno_data` row agree on
  `fut_security_id` and are within a bar's movement on `spot`/`fut_oi`.
- `fut_bar_oi_change` spot-checked against Dhan REST `oi=true` for a few bars, as the
  observations runbook does by hand.
- **Futures roll 2026-10-28:** the first tick on the new contract has null futures/OI deltas,
  not a spurious +146%.
- **Dussehra 2026-10-20 (holiday):** the three schedules are `DISABLED` after 08:00 and
  nothing fires.
- Cost: measured against the CloudWatch `EstimatedCharges` metric, not Cost Explorer.

## Rollback

Everything is additive. To remove: disable and delete the three schedules, delete the
function, drop the two tables, remove the three names/ARNs from the gate. The 15-minute
chain is unaffected at every step.

## Risks

- **Dhan rate limit.** The poller fires on the same minutes as the loader (every third tick).
  Both call Dhan and the limit is per account. The client retries `DH-904` with backoff, but
  an overlap can slow either. Decision D1.
- **Neon active time roughly doubles** (~15 → ~32-35 CU-hours a month estimated) because a
  5-minute cadence keeps the compute awake. Free-plan allowance not verified.
- **Contract roll** 2026-10-28 falls inside phase 1 — covered by the acceptance check.
- **Runtime unmeasured.** Estimated well under the 22 s sentiment run (no history, no
  breadth, no VIX); to be measured on the first live tick.

## Decisions needed

| # | Decision | My recommendation |
|---|---|---|
| D1 | Run on the exact 5-minute marks (09:35, 09:40 …), or offset to clear the loader → sentiment run? | **DECIDED: +1 minute.** (I had recommended +2; +1 is tighter — the loader and sentiment run back to back for roughly a minute from each quarter hour, so one tick in three may overlap the tail of that chain. Moving to +2 changes only the cron strings.) |
| D2 | Include INDIA VIX on the fast row? | **Leave out** for phase 1 (it is on the 15-minute row). Cheap to add later. |
| D3 | Names: function `intraday-fno-poller`, tables `fno_5m_snapshot` / `fno_5m_leg` | As written. |
| D4 | Store option **volume** beyond PCR and per-leg volume (e.g. CE/PE volume totals)? | Not in phase 1; `pcr_volume` plus leg volumes are enough to start. |
| D5 | Duplicate `chain.py`/`expiry.py` (convention) or move to a layer? | **Duplicate**, as written. |
