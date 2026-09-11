# intraday-data-loader

AWS Lambda function that keeps the intraday candle tables current for NIFTY and
the current-month NIFTY future, through the trading session.

| | |
|---|---|
| Schedule | every 5 min 10:00–15:35, `Asia/Kolkata` — two rules, see [Deployment](#deployment-shape) |
| Invocations | **68** per trading day |
| Writes | `algo.candle_5min`, `algo.candle_15min`, `algo.candle_1hr` |
| Reads | `algo.instrument_master` |
| Instruments | NIFTY (`13`/`INDEX`) and the current-month future, resolved at run time |
| Database | Neon `AI Trader APP` (`nameless-mountain-15353651`) / `Algo` / `algo` — the only one |

It computes nothing. No regime, no score, no signal — it stores candles and
raises if it cannot. The daily read stays in
[daily-market-sentiment](../daily-market-sentiment/README.md), which owns
`candle_daily` and does not touch these three tables.

## Which intervals a run fetches

One rule decides it:

> fetch interval **I** when `(run time − 09:15)` is a whole multiple of **I** minutes

| Run | Fetches |
|---|---|
| 10:05, 10:35, … | 5-min |
| 10:00, 10:30, 10:45, … | 5-min, 15-min |
| 10:15, 11:15, 12:15, 13:15, 14:15, 15:15 | 5-min, 15-min, 60-min |
| **15:35** (closing sweep) | all three, always |

The rule is written this way rather than as a table of trigger times because it
derives from the bucket boundaries instead of restating them — if the two ever
disagree, the schedule is wrong rather than merely stale. `assert_alignment()`
turns that disagreement into a raised error instead of quietly misaligned rows.

Per trading day that works out to 68 five-minute fetches, 24 fifteen-minute and
7 hourly, per instrument.

**Why 15:35 exists.** The schedule runs to 15:30, but at 15:30 the 15:25
five-minute bar, the 15:15 fifteen-minute bar and the 15:15 hourly bar have only
just closed. With no later run they would sit **partial in the database
forever**, quietly corrupting any end-of-day read. The sweep fetches all three
intervals and finalises them.

**Why a 10:00 start loses nothing.** Each fetch covers the whole session, so the
10:00 run picks up the 09:15/09:30/09:45 fifteen-minute bars and the 10:15 run
picks up the 09:15 hourly bar.

## Numbers from the first run

Cold start of both instruments in one invocation, 2026-09-11: **12,519 rows**,
27.4 s, peak memory 109 MB.

| | 5-min | 15-min | 60-min | Sessions | First bar |
|---|---|---|---|---|---|
| NIFTY (`13`) | 4,800 | 1,600 | 448 | 64 | 2026-06-15 |
| NIFTY-SEP2026-FUT (`68407`) | 3,975 | 1,325 | 371 | 53 | 2026-07-01 |

**Exactly 75.00 / 25.00 / 7.00 bars per session** for both instruments, with
zero variance and **zero misaligned bars**. That is the arithmetic working out:
75 × 5 min = 25 × 15 min = 6 h 15 m = 09:15→15:30, and seven hourly buckets. A
session filter that was off by one bar, or hourly buckets aligned to the clock
instead of 09:15, would both show up here as a fractional average.

The future starts later than the index because the SEP contract was listed
later — index futures list roughly three months out, so a 90-day window reaches
past the contract's own history.

## Partial candles are stored on purpose

Dhan returns the in-progress bucket, and this function writes it. A stored bar
is therefore **correct-so-far, not final**, and is corrected by the primary-key
upsert on a later pass. Only the newest bar per table is ever partial, so
historical reads stay replayable.

The row carries no "is final" column. Consumers do not need one:

```
a bar is closed  iff  candle_ts + interval_seconds <= now
```

Worked: the 15-minute bar stamped 10:15 is written at 10:20 holding five minutes
of range. At 10:22, `10:15 + 900s = 10:30 > 10:22` — still forming. At 10:31 it
is closed and final.

**This matters.** A strategy reading that bar at 10:22 without the test sees
`high`/`low` that look like a complete 15-minute range but cover seven minutes —
enough to call a breakout failed that has not happened yet.

## Resuming

Each `(instrument, interval)` resumes from `MAX(candle_ts)` in its own table.

`fromDate` is **exclusive**, which normally makes resume clean — pass the last
stored timestamp back and only genuinely new bars return, no duplicate and no
gap. But the last stored bar may be partial, and resuming from its own timestamp
would never re-fetch it, leaving it partial forever. So the window starts **one
whole interval earlier**, which re-fetches exactly that bar and nothing more.

Cold start is 90 days, which is also Dhan's per-call ceiling — one call, no
chunking. A stale resume is clamped to the same 90 days. Going deeper would need
the fetch chunked into ≤90-day windows; that is deliberately not built.

## Resolving the current-month future

Nothing about the contract is hardcoded — its `security_id` changes every month.

1. `POST /v2/optionchain/expirylist` for NIFTY (`13`/`IDX_I`) returns option
   expiries, ascending.
2. The **nearest expiry's month** is the current futures month.
3. That becomes `NIFTY-<MON><YYYY>-FUT`, resolved to a `security_id` against
   `instrument_master`.

It rolls itself: once September's monthly expiry has passed, the nearest expiry
is already in October. Verified against the live list (measured 2026-09-11:
nearest `2026-09-15`, September monthly `2026-09-29`, then `2026-10-06`) —
2026-09-29 resolves to SEP, 2026-09-30 to OCT, 2026-10-28 to NOV.

The `NIFTY-` prefix is load-bearing: `NIFTYFPI-SEP2026-FUT` and
`NIFTYNXT50-SEP2026-FUT` are different contracts that a looser pattern eats.

The result is cached per IST date in a module global, so warm containers do not
re-fetch the expiry list on all 68 runs.

## Measured facts about Dhan's v2 charts API

Everything in [daily-market-sentiment's README](../daily-market-sentiment/README.md#measured-facts-about-dhans-v2-charts-api)
still applies. One addition, measured here:

### `interval: 60` is session-aligned, not clock-aligned

Buckets start **09:15, 10:15, 11:15 … 15:15** — seven per session, the last a
15-minute stub. Measured 2026-09-11 against the 2026-09-10 session by shortening
`toDate`:

```
toDate 10:10  ->  1 candle    (09:15 bucket still forming)
toDate 10:20  ->  2 candles   (09:15 closed, 10:15 forming)
```

Clock-aligned bucketing would have returned 2 and 2. A whole-session fetch
returns **7 either way**, which is why the short window was needed to tell them
apart — the obvious test does not discriminate.

The hourly trigger times derive from this, so `assert_alignment()` checks every
bar against it at runtime and raises if Dhan ever changes.

The same test shows Dhan **returns the in-progress bucket**, which is what makes
the partial-candle design work at all.

## Layout

```
handler.py     entry point, the schedule rule, resume window, alignment guard
config.py      this function's tunables
params.py      the Dhan token and client id
dhan.py        charts + expiry client, and the measured facts about them
db.py          Neon access and the upsert
```

Epoch/IST handling, `connect()` and the shared connection-string read come from
the [`neon-access`](../layers/neon-access/README.md) layer. pg8000 comes from
`neon-db-driver`. Both layers are required.

`params.py` is named `params`, not `secrets` — a `secrets.py` at the zip root
shadows the stdlib module of that name, and the zip root is first on `sys.path`,
so it shadows it for boto3 too.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DHAN_CHARTS_BASE` | `https://api.dhan.co/v2/charts/` | |
| `DHAN_OPTIONCHAIN_BASE` | `https://api.dhan.co/v2/optionchain/` | the expiry list |
| `TOKEN_PARAMETER_NAME` | `/algo/dhan/token` | written by `auth-dhan-broker` |
| `DHAN_CLIENT_ID` | — | **not normally set** — fallback only, see below |
| `NIFTY_SECURITY_ID` / `NIFTY_INSTRUMENT_TYPE` | `13` / `INDEX` | always passed together |
| `FUTURES_UNDERLYING_SCRIP` / `_SEG` | `13` / `IDX_I` | for the expiry list |
| `FUTURES_INSTRUMENT_TYPE` | `FUTIDX` | |
| `COLD_START_DAYS` | `90` | also Dhan's per-call ceiling |
| `API_PACING_SECONDS` | `4.0` | measured; tighter than the documented 5/s |
| `HTTP_TIMEOUT_SECONDS` | `60` | |
| `UPSERT_BATCH_SIZE` | `5000` | 8 params/row against Postgres's 65,535 cap |

### The client id comes from the token, not from configuration

The charts endpoints need only `access-token` — that is how
`daily-market-sentiment` runs. The option-chain family, which `expirylist`
belongs to, also wants a `client-id` header.

**There is nothing to configure.** The access token is a JWT whose payload
carries `dhanClientId` next to the `exp` claim that `auth-dhan-broker` already
decodes. Verified against the live parameter on 2026-09-11 — the claims are:

```
dhanClientId, exp, iat, iss, partnerId, tokenConsumerType, userRegion
```

Reading it from there makes the id **authoritative rather than configured**: it
is by construction the client the token was minted for, so it cannot drift out
of step with a rotated token the way a separately-held copy can. It also keeps
the value out of a second place — `architecture.md` moved the Neon connection
string out of per-function environment variables for the same reason.

`DHAN_CLIENT_ID` and a `client_id` key in the token record remain as fallbacks
for a token that somehow lacks the claim. Neither is normally set. The source is
always logged.

> **Still unverified:** whether `expirylist` requires the header at all. It is
> how Dhan's other data APIs behave, but it has not been measured against the
> live endpoint. Since the value now comes free with the token, this no longer
> costs anything either way.

## Local verification

No test suite and no build step. `pg8000` and `boto3` come from the layer and the
runtime and are normally absent locally, so stub both, then put the layer and the
function on `sys.path`:

```python
import sys, types
stub = types.ModuleType("pg8000"); stub.dbapi = types.ModuleType("pg8000.dbapi")
sys.modules["pg8000"] = stub; sys.modules["pg8000.dbapi"] = stub.dbapi
boto3 = types.ModuleType("boto3"); boto3.client = lambda *a, **k: None
sys.modules["boto3"] = boto3
sys.path[:0] = ["layers/neon-access/python", "intraday-data-loader"]
import handler
```

`intervals_due()`, `fetch_window()`, `assert_alignment()`,
`current_future_symbol()` and `dhan.session_candles()` are all pure and testable
without credentials. `current_future_symbol()` takes any object with an
`expiry_list()` method, so the rollover can be checked against a recorded expiry
list at any date. `upsert_candles()` takes a fake connection whose
`cursor().execute()` records `(len(params), sql)` — that is how the batch-size
and parameter-count limits were checked.

## Deployment shape

| | |
|---|---|
| Handler | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package |
| Layers | `neon-db-driver` + `neon-access` |
| Package | the five `.py` files at the zip root — everything else is stdlib |
| Timeout | **180 s** — see below |
| Memory | 256 MB — measured peak 109 MB on a both-instruments cold start |

```
arn:aws:lambda:ap-south-1:709458364771:layer:neon-db-driver:1
arn:aws:lambda:ap-south-1:709458364771:layer:neon-access:1
```

**Attach the layers before uploading the code** — `handler.py` imports
`neon_access` at module load, so the reverse order gives
`Unable to import module 'handler': No module named 'neon_access'`.

**Timeout is 180 s even though a run takes ~27 s.** The Dhan retry backoff is
`4 × 2^(attempt+1)` = 8 + 16 + 32 = **56 s of sleeping on a single
rate-limited call**. 180 s absorbs two such calls alongside the 24 s of normal
pacing (2 × 56 + 24 = 136 s) with room to spare; anything under ~90 s turns a
single `DH-904` into a failed run.

### IAM

| Action | Resource |
|---|---|
| `ssm:GetParameter` | `/algo/dhan/token`, `/algo/neon/connection` |
| `kms:Decrypt` | `Resource: "*"` with `kms:ViaService = ssm.<region>.amazonaws.com` |
| `logs:*` | `AWSLambdaBasicExecutionRole` |

The parameter names carry a leading slash but the ARN does **not** double it —
`parameter/algo/dhan/token`.

A freshly created function's role has only `AWSLambdaBasicExecutionRole`, so the
first test invocation fails on `ssm:GetParameter` with `AccessDeniedException`
before reaching any Dhan call. That is the policy below missing, not a code
fault.

Set the handler under **Code → Runtime settings → Edit**, not Configuration →
General. Upload a zip; do not paste into the console editor — a browser paste of
a long handler has silently truncated before.

Schedules, both `Asia/Kolkata` with the flexible window off:

```
cron(0/5 10-14 ? * MON-FRI *)                     10:00–14:55   60 runs
cron(0,5,10,15,20,25,30,35 15 ? * MON-FRI *)      15:00–15:35    8 runs
```

68 between them, the last being the closing sweep. The obvious single
`cron(0/5 10-15 ? * MON-FRI *)` is **wrong**: it keeps firing to 15:55, well
past the close, and collides with a separate 15:35 sweep rule. Splitting at the
hour boundary is what makes the last run land exactly on 15:35. Hours 10–14 are
complete, so a `0/5` step works; hour 15 stops early, so its minutes are spelled
out.

Deployed as `intraday-data-loader-session` and `intraday-data-loader-close`,
each with **its own execution role**. Reusing another schedule's role does not
work — the console scopes those to the function they were created for, and the
mismatch surfaces only as a schedule that silently never fires.

Retries are the console default (2 attempts, 1-hour maximum event age). Nothing
depends on them: the upserts are idempotent and batched in timestamp order, so a
retry re-writes identical rows, and a run that fails outright is picked up by the
next one five minutes later — the resume window comes from `MAX(candle_ts)`, not
from the schedule.

Test events: `{"intervals": [5]}` forces a single interval,
`{"now": "2026-09-11T10:15:00"}` overrides the clock the schedule rule reads.

## Porting notes

This has no direct ancestor in `trading-algo`. The legacy system fetched
intraday data inside the session loop; here it is a standalone loader on its own
schedule, for the same reason the rest of this repo is: a failure raises its own
alarm rather than taking a session down with it.

Two departures worth recording:

- **`session_candles()` drops the single-day check.** The
  `daily-market-sentiment` version filters to one named day because it fetches
  one day. This one backfills up to 90, so it filters on time-of-day only.
- **`SESSION_END` is exclusive here.** Bars are stamped at the start of their
  bucket, so the last legitimate bar of a session starts before 15:30 — a 15:30
  stamp is post-close, like the zero-volume 19:20 candle Dhan emitted on
  2026-09-10.

## Not in scope

No signals, no strategy, no Telegram. No writes to `candle_daily` or
`instrument_master`. Backfill deeper than 90 days would need chunked windows and
is not built.
