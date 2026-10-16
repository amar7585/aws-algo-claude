# intraday-fno-poller

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md) · [Phase-1 plan](../docs/phase-1-fno-5m-poller-plan.md)

AWS Lambda function that reads the **F&O side of the market every five minutes**
and **stores** it: the futures price, open interest and volume (including the last
closed bar's own OI change), the option-chain aggregates for the nearest and
monthly expiries, and the raw ATM ±2 legs.

**Status: built and locally verified; schema APPLIED 2026-10-16 (both tables live, columns
verified against the code). Not yet deployed — no AWS resource exists.** See
[Deployment](#deployment-shape).

| | |
|---|---|
| Schedule | three rules, `Asia/Kolkata`, **72 runs a day**, one minute past each five-minute mark — see [Schedule](#schedule) |
| Writes | `algo.fno_5m_snapshot`, `algo.fno_5m_leg` — one transaction |
| Reads | `algo.instrument_master`, and its own previous row |
| Invokes / invoked by | **nothing.** No consumer reads its tables yet (phase 1) |
| Instruments | NIFTY (`13`/`INDEX`), the current-month future, resolved at run time |
| Runtime | Python 3.14, zip package, 8 modules, pure Python |
| Layers | `neon-db-driver` + `neon-access` |
| Database | Neon `AI Trader APP` (`nameless-mountain-15353651`) / `Algo` / `algo` — the only one |

## Why it exists

Dhan serves only a **live** option chain — there is no historical-chain endpoint —
so a five-minute reading not taken today cannot be recovered tomorrow. The 15-minute
chain (loader → `intraday-market-sentiment` → classifier → detector → manager) keeps
running unchanged; this stores the in-between readings so a five-minute OI and volume
move can later be compared against a 15-minute reference at key levels.

**Phase 1 stores only.** It classifies nothing, scores nothing, reads no daily row,
computes no SMA/RSI, breadth or VIX, and no function reads what it writes. Those stay
on the 15-minute row; the two grains join at read time on `snapshot_ts` (the join is
in `schema.sql`).

## Two bars per tick, and they are not the same bar

| | |
|---|---|
| **Snapshot bar** | the newest bar Dhan returned, **forming**. Names the row (`snapshot_ts`) and supplies the live `spot`, `fut_price` and `fut_oi`. |
| **Closed bar** | the newest bar that has **finished** — `candle_ts + 300 <= now`. Supplies `idx_bar_*` and `fut_bar_*`: final volume and final OI. **These are what a five-minute reading compares.** |

The forming bar's own volume and OI are seconds old and near-empty. Comparing them to a
reference would read as "volume collapsed" on every tick.

At the 15:31 run the newest bar is 15:25 and it **is** closed (the market shut at
15:30), so both bars are the same one — no special case. The first run is 09:36, whose
closed bar is 09:30.

### `fut_bar_oi_change` — the column the observations turned on

The closed bar's open interest minus the bar before it, **both from the same Dhan
response**. It does not depend on a previous row, so a missed tick cannot corrupt it. It
is `NULL` when the two bars are not adjacent (a gap would make it a multi-bar change under
a single-bar name).

The logged sessions show why: at the 2026-09-18 lows per-bar OI **rose** (+13,325 and
+6,760 — shorts pressing) and the up-thrust came on OI **falling** (−2,340, −3,445 —
short covering). The 15-minute `buildup` read `SHORT_BUILDUP` at the event and
*contradicted* the reversal. See [session-observation-runbook](../docs/session-observation-runbook.md).

> Per-bar `open_interest` semantics are **checked, not assumed**: spot-check a few bars
> against Dhan REST `oi=true` once live (acceptance checks in the plan).

## Deltas, and when they are NULL

Every `*_change` / `*_change_pct` is versus `prev_snapshot_ts`, the previous tick **on
the same IST day**.

- **The first tick of a session has no baseline — its deltas are `NULL`.** This differs
  from the 15-minute row, whose first snapshot spans the overnight gap. At 15-minute
  grain that is a documented read; at five minutes a `*_change_pct` that silently covers
  sixteen hours is a trap, because every other row's covers five minutes.
- A **missed tick widens** the window rather than nulling it; `prev_snapshot_ts` shows
  which window each delta covers.
- **Across a contract roll the deltas are `NULL`, not computed.** A monthly futures roll
  nulls `fut_price_change_pct` and the `fut_oi_change*` columns; an option expiry roll
  nulls that expiry's OI changes. Measured 2026-09-30 on the slow row, the first snapshot
  after the September expiry compared `NIFTY-OCT2026-FUT` with the expired September
  contract and read **+146% OI**. **The next roll is 2026-10-28** and falls inside phase
  1 — it is an acceptance check.

No reference pointer is stored. The 15-minute reference is joined at read time, and a
15-minute delta of anything here is the row three ticks back.

## Schedule

Three rules, all `Asia/Kolkata`, flexible window off:

```
intraday-fno-poller-open      cron(36,41,46,51,56 9 ? * MON-FRI *)        5 runs   09:36-09:56
intraday-fno-poller-session   cron(1/5 10-14 ? * MON-FRI *)             60 runs   10:01-14:56
intraday-fno-poller-close     cron(1,6,11,16,21,26,31 15 ? * MON-FRI *)  7 runs   15:01-15:31
```

**72 runs.** One minute past each mark so it does not start on the same second as
`intraday-data-loader`, which fires on the quarter hour. The last run (15:31) stamps the
15:25 bar, the day's final one, so no 15:35 sweep is needed. A single `cron(1/5 10-15 …)`
would run past the close.

> **A one-minute offset is thinner than a two-minute one.** The loader (~15–27 s) and then
> `intraday-market-sentiment` (~22 s) run back to back from the quarter hour, so on every
> third tick this function can overlap the tail of that chain. Dhan's limit is per account;
> each function paces itself but neither knows about the other. The client retries `DH-904`
> with backoff, so an overlap should slow a run rather than fail it — watch the first
> sessions. If it does bite, moving to +2 minutes changes only the three cron strings.

`FIRST_RUN` (09:35) and `LAST_RUN` (15:35) are a second line of defence against a manual
invocation, not the mechanism.

## Holiday gate

All three schedules are created **DISABLED**. `auth-dhan-broker` **enables** the
schedules in `MANAGED_SCHEDULE_NAMES` after minting the token and **disables** them on a
holiday, so they come alive on the first trading morning after the gate is extended.
Adding them takes **two edits, not one** — the name in `MANAGED_SCHEDULE_NAMES` *and* the
ARN in the `ToggleSessionSchedules` statement of `holiday-gate-access` — see
[auth-dhan-broker](../auth-dhan-broker/README.md#the-four-schedules-and-the-one-that-is-never-touched).
The 2026-09-18 retime missed this once and the 09:45 loader run fired on a holiday.

There is **no holiday check inside this handler**, deliberately — same reasoning as the
other session functions. The weekend guard stays.

## Handler flow

1. Weekend / outside-window guard. Read the token (raises if expired) and the client id.
2. Resolve NIFTY on the **full identity** `(13, INDEX)`.
3. Fetch the index's 5-min bars for **today only** — no 10-day history. `snapshot_ts` =
   the newest bar; the closed bar is selected by `candle_ts + 300 <= now`.
4. Expiry list (cached per IST date in a warm container) → nearest + monthly; resolve the
   current-month future by symbol `NIFTY-<MON><YYYY>-FUT` from the **nearest** expiry's month.
5. Fetch the future's bars with `oi: true` — **raise** if `open_interest` is absent.
6. Both chains, centred on the **same** spot.
7. Read the previous tick (same IST day); guard the deltas against a contract roll.
8. Write the row and the legs in **one transaction**, upsert by key. A re-run overwrites.
   A missing column, a partial leg, or a chain that yields **no legs** raises.

Raises on any failure — never a `{"statusCode": 500}` shape. No invoke, so nothing
downstream can be failed by it and it cannot be failed by anything downstream.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DHAN_CHARTS_BASE` | `https://api.dhan.co/v2/charts/` | |
| `DHAN_OPTIONCHAIN_URL` | `https://api.dhan.co/v2/optionchain` | flat — see `dhan.py` |
| `DHAN_EXPIRYLIST_URL` | `https://api.dhan.co/v2/optionchain/expirylist` | nested |
| `TOKEN_PARAMETER_NAME` | `/algo/dhan/token` | written by `auth-dhan-broker` |
| `NIFTY_SECURITY_ID` / `_INSTRUMENT_TYPE` | `13` / `INDEX` | always passed together |
| `FUTURES_UNDERLYING_SCRIP` / `_SEG` | `13` / `IDX_I` | |
| `FUTURES_INSTRUMENT_TYPE` | `FUTIDX` | |
| `FUTURES_SYMBOL_TEMPLATE` | `NIFTY-{month}{year}-FUT` | the `NIFTY-` prefix is load-bearing |
| `AGGREGATE_STRIKES_PER_SIDE` | `20` | PCR, OI totals, max pain, walls |
| `RAW_STRIKES_PER_SIDE` | `2` | 5 strikes → 10 stored legs |
| `API_PACING_SECONDS` | `4.0` | measured; tighter than the documented 5/s |
| `HTTP_TIMEOUT_SECONDS` | `60` | |
| `UPSERT_BATCH_SIZE` | `500` | 24 params per leg row against Postgres's 65,535 cap |

The Dhan client id comes from the access token's own `dhanClientId` claim, not from
configuration. No environment variables are required.

## Layout

```
handler.py    entry point, the guards, the row assembly
config.py     this function's tunables and the schedule
params.py     the Dhan token and client id          (copy of sentiment's)
dhan.py       charts + expiry list + option chain   (trimmed copy of sentiment's)
expiry.py     nearest / monthly selection           (verbatim copy)
chain.py      ATM, PCR, OI walls, max pain, legs    (verbatim copy)
reading.py    which bar is which, what changed      (pure; new)
db.py         the two-table write, previous-tick read
schema.sql    algo.fno_5m_snapshot, algo.fno_5m_leg
```

`chain.py`, `expiry.py` and `params.py` are **copies** of
`intraday-market-sentiment`'s. A fix in one must be carried to the other. The repo copies
rather than shares because a shared module would live in the `neon-access` layer, where
every change means republishing it and repointing every function's pinned ARN.

## Local verification

No test suite and no build step. `pg8000` and `boto3` come from the layer and the runtime
and are normally absent locally, so stub both, put the `neon-access` layer and this folder
on `sys.path`, and replace `handler.DhanClient`, `read_token_record`, `read_client_id`,
`read_neon_connection_string` and `connect`:

```python
import sys, types
stub = types.ModuleType("pg8000"); stub.dbapi = types.ModuleType("pg8000.dbapi")
sys.modules["pg8000"] = stub; sys.modules["pg8000.dbapi"] = stub.dbapi
boto3 = types.ModuleType("boto3"); boto3.client = lambda *a, **k: None
sys.modules["boto3"] = boto3
sys.path[:0] = ["layers/neon-access/python", "intraday-fno-poller"]
import handler
handler.lambda_handler({"now": "2026-10-14T10:01:00"}, None)   # `now` overrides the clock
```

Verified this way (77 checks): the schema columns equal the code's column tuples; the
`fno_5m_leg` columns equal `option_chain_snapshot`'s; the closed bar versus the forming bar
at 09:36, 10:01 and 15:31; `fut_bar_oi_change`; the same-day baseline and every delta; the
futures-roll and expiry-roll guards; a gap in the series; a future without `open_interest`;
a chain with no legs; rollback on a failed legs write; the weekend and outside-window
guards; IV 0 → `NULL`; the expiry cache; and the cron arithmetic (72 runs, 300 s apart,
none on a loader minute).

> **Not yet verified against real Dhan data.** The payloads used are synthetic, built in
> the measured shapes documented in `dhan.py` and `chain.py`. That proves the code's
> arithmetic and guards, not that Dhan still sends those shapes. A real capture needs a
> live token; do it at the smoke test before the schedules are enabled.

## Deployment shape

| | |
|---|---|
| Handler | `handler.lambda_handler` — set under **Code → Runtime settings**, not Configuration → General |
| Runtime | Python 3.14, zip package |
| Layers | `neon-db-driver` + `neon-access` — attach **before** uploading code |
| Memory / timeout | 256 MB (to be measured) / **120 s** — absorbs a `DH-904` backoff of 56 s |
| Secrets | `/algo/dhan/token`, `/algo/neon/connection` — no environment variables |

**Upload a zip; do not paste into the console editor** — a browser paste of a long handler
silently truncated before.

### IAM — its own pair

Neither existing role can be reused; both fail **silently** when borrowed.

- **`intraday-fno-poller-role`** (execution): `logs:CreateLogStream` / `PutLogEvents` on
  **its own** log group only; `ssm:GetParameter` on `/algo/dhan/token` and
  `/algo/neon/connection`; `kms:Decrypt` with `kms:ViaService = ssm.<region>.amazonaws.com`.
  Borrowing another function's policy gives a function that runs and writes rows but
  produces **no logs at all** — and with no logs `error-notifier` can never report it.
- **`EventBridge-Scheduler-intraday-fno-poller`**: `lambda:InvokeFunction` scoped to this
  function's ARN. Reusing another schedule's role creates a schedule that shows `ENABLED`
  and never fires.

Then subscribe `error-notifier` to the new log group, and extend the holiday gate (above).

## Rollback

Everything is additive. Disable and delete the three schedules, delete the function, drop
the two tables (`DROP TABLE algo.fno_5m_leg; DROP TABLE algo.fno_5m_snapshot;`), and remove
the three names/ARNs from the gate. The 15-minute chain is unaffected at every step.

## Not in scope

No classification, no signals, no Telegram, no thresholds, no consumer, no order placement.
No VIX, SMA/RSI, breadth, opening range, VWAP or day high/low on the fast row. No backfill.
No change to `candle_5min` (it has no OI column and keeps none).
