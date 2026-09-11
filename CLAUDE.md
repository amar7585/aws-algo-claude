# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## How to work here — read before anything else

This section governs everything below it. Where it conflicts with an instinct to
be helpful by moving fast, this section wins.

### 0. Don't decide alone.

The rule the other four are all forms of. When anything is unclear, blocked, or
not working — an ambiguous requirement, a dependency that won't fit, a limit
you've hit, an approach that isn't panning out, a better design than the one in
place — **stop**. Lay out the options you can see, with their trade-offs, and
bring them to Amar. Decide together, then build.

Being technically right does not make it your call. A question is not approval
to build.

Small, reversible, obviously-in-scope edits don't need this. Anything that
changes behaviour, structure, or dependencies does.

### 1. Do not assume. Surface it and discuss.

If something is unstated — a requirement, a constraint, a trade-off, which of
two readings is meant — **say so and ask**. Do not pick the plausible option and
proceed quietly. An assumption buried in delivered code is far more expensive
than a question asked up front, because it only surfaces once it is load-bearing.

When you do have to proceed under an assumption, state it in plain text as an
assumption, not as a settled fact.

### 2. Never assume the architecture. Flag it.

Architectural choices are not implementation details and are **not yours to
make quietly**. That includes: what runs where (Lambda vs container vs task),
which dependencies are in or out, packaging and deployment shape, how
components are split, what the data model looks like, and error/retry strategy.

If a change touches any of those, stop and raise it — even when the change looks
like an obvious improvement, and even when it is technically better. "It was the
right call" does not make it your call.

### 3. Plan first. Get approval. Then implement.

The default order is **plan → approval → implement**, not implement-then-explain.

A plan means: what you propose to change, why, what it affects, and what you are
explicitly *not* doing. Wait for a yes. Do not treat a question as authorisation
to build, and do not treat silence or a partial reply as approval.

Small, reversible, obviously-in-scope edits do not need this. Anything that
changes behaviour, structure, or dependencies does.

### 4. Present the whole option space, not just one answer.

Lay out the real alternatives with their trade-offs — including options that
depart from the current architecture. Do not silently narrow to whatever fits
what already exists, and do not present a single approach as though it were the
only one.

Recommend one, say why, and make the others visible enough to actually choose
from. The current design is a starting point to argue with, not a constraint to
work around.

### Where this came from

Every rule above is a correction of something that happened in this repo:

- The packaging approach was switched (containers → Lambda, pandas/`dhanhq`
  dropped) and delivered as a finished result rather than proposed first.
- `ExchangeSegment` values were inlined and the `FUTIDXBSE` segment resolution
  was "fixed" — both architectural calls, both made unilaterally, and the
  `FUTIDXBSE` fix turned out to be inert against live data anyway.
- A working handler was rewritten when the ask had been for a folder structure.
- Earlier still: a Lambda packaging limit was treated as a settled conclusion
  rather than a claim to verify. That produced four containerised Fargate
  services, a Step Functions definition and an ECR build pipeline — all thrown
  away. Two cheaper options were never checked: a layer (which is what runs
  today), and simply not needing the dependencies (which is what it turned out
  to be). **"X doesn't fit" is a claim to test with numbers, not a reason to
  change platform.**

The cost was rework and lost trust in the output, not a broken system. Assume
that trade is never worth it here.

## What this repo is

The **data layer** of an Indian-index algo trading system: AWS Lambda functions
that keep a replayable picture of the market in Neon Postgres. It does not place
orders and holds no strategy logic.

Full context is in [README.md](README.md) and [docs/](docs/) — architecture,
flow charts, component register. Read those before changing structure.

## Hard rules

These are load-bearing. Breaking one is silently wrong rather than loudly broken.

1. **Prefer pure Python. A compiled dependency needs a reason and a measured
   size.** Zip-packaged Lambda is the default shape because it stays small and
   needs no container pipeline, so reach for stdlib or a pure-Python package
   first — that is a preference with real weight behind it, not a ban. To add a
   compiled dependency: say what it buys, confirm no pure-Python option does the
   job, and measure the packaged size against Lambda's limits before adding it.
   An earlier iteration moved the whole project to ECS Fargate over `pandas`,
   `psycopg2` and the `dhanhq` SDK and was reverted — the lesson there is that
   "X doesn't fit" is a claim to test with numbers, not that the answer is
   always no. `dhanhq` specifically is an approved fallback if the REST API
   proves unworkable.
2. **Every stored time value is epoch seconds** (`bigint`). No `timestamptz`
   columns, no timezone arithmetic in SQL. Convert to IST at display only.
3. **`exchange_segment` holds raw Dhan segment codes** — `NSE_EQ`, `IDX_I`,
   `NSE_FNO`, `BSE_FNO` — never human-readable strings. Downstream code passes
   them straight back to the broker API.
4. **`(security_id, instrument_type)` is instrument identity.** `security_id` is
   `text`, not an integer.
5. **Fail loudly.** Raise so Lambda records an error; never return a
   `{"statusCode": 500}` shape, which Lambda counts as a *success* — no alarm,
   no retry. Never let a schema change degrade into a silent 0-row write.

## Database — check the project id first

Two Neon projects both have an `algo` schema with **the same table names and
incompatible columns**. Writing to the wrong one is the easiest serious mistake
to make here.

| Project | id | Database | Used by |
|---|---|---|---|
| **AI Trader APP** | `nameless-mountain-15353651` | `Algo` | **this repo** |
| claude algo | `rapid-cherry-81525678` | `neondb` | the separate `AIML` repo — not this one |

`AI Trader APP.algo.instrument_master` has `security_id text` / `updated_at
bigint`; the `AIML` migration defines `security_id integer` /
`updated_at_epoch`. Always confirm the project id before running SQL.

## Local verification

There is no test suite and no build step. `handler.py` imports `pg8000`, which
is supplied by the Lambda layer and is normally not installed locally — so stub
it to exercise the pure filter/parse path:

```python
import sys, types, importlib.util
stub = types.ModuleType("pg8000"); stub.dbapi = types.ModuleType("pg8000.dbapi")
sys.modules["pg8000"] = stub; sys.modules["pg8000.dbapi"] = stub.dbapi
spec = importlib.util.spec_from_file_location("handler", "instrument-master-loader/handler.py")
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
rows = h.fetch_and_filter_instruments(open("master.csv", encoding="utf-8-sig").read())
```

Get real input data — the scrip master is public and needs no credentials:

```bash
curl -sS -o master.csv https://images.dhan.co/api-data/api-scrip-master.csv
```

To test `upsert_instrument_master()` without a database, pass a fake connection
whose `cursor().execute()` records `(len(params), len(sql))`. That is how the
batch-size and parameter-count limits below were verified.

**Any change to the filter must be diffed against the previous output
field-by-field, on the real master — not on a synthetic fixture.** A synthetic
fixture already hid one live-data bug in this repo (see the `FUTIDXBSE` gap
below): it used values that do not occur in the real file, so the test passed
while production dropped every row.

## Deployment facts that cost time to rediscover

- **Handler must be `handler.lambda_handler`.** Set under Code tab → Runtime
  settings → Edit, *not* Configuration → General configuration. Two different
  failures look similar and mean opposite things:
  `Handler 'handler' missing on module 'handler'` — the file was found, the
  function name is wrong. `Unable to import module 'handler': No module named
  'handler'` — the *file* is not there: the console editor's default
  `lambda_function.py` was never renamed to `handler.py`, the rename was never
  **Deploy**ed, or the file sits in a subfolder instead of the package root.
  Console edits do nothing until Deploy is clicked.
- **Upload a zip; do not paste into the Lambda console editor.** A browser
  paste of a 350-line handler silently truncated at line 44, and the editor
  accepted the partial file without complaint — surfacing later as
  `unterminated triple-quoted string literal`. A zip upload cannot truncate.
- **Layer zip needs packages under `python/`** at the zip root, or the import
  fails at runtime with `No module named 'pg8000'`.
- **Postgres caps bind parameters at 65,535 per statement** (int16 in the wire
  protocol). At 6 params per row that is a hard ceiling of **10,922 rows per
  batch**; `UPSERT_BATCH_SIZE` is 5500. Exceeding it fails as a confusing
  protocol error that never names the batch size.
- **`pg8000.dbapi` uses `format` paramstyle** (`%s` placeholders) and has **no
  `execute_values` equivalent** — multi-row upserts build their own `VALUES`
  tuples.
- **pg8000 takes discrete kwargs, not a DSN.** Connection strings are parsed
  with `urllib.parse` and passed with an explicit SSL context; Neon requires TLS.
- **`/v2/RenewToken` cannot renew a TOTP-minted token.** It answers
  `HTTP 500 DH-905 INVALID_REQUEST "Renewal of token not allowed for this token
  type"` — renewal is offered only for tokens generated by hand from Dhan Web.
  Seeding from the portal and renewing onward does not help either: the chain
  breaks each weekend and Monday's TOTP token is unrenewable. The renew branch
  was built, tested live and deleted; do not rebuild it without new evidence.
  Measured 2026-09-10.
- **Dhan's auth endpoints take no request body.** Sending `Content-Type:
  application/json` with a zero-length body earns
  `DH-905 "Missing required fields, bad values for parameters"`, which looks
  like a rejected credential but is a malformed request. Dhan's prose calls
  `RenewToken` a POST while its own worked curl sends neither `-d` nor
  `-X POST` — i.e. a GET. Trust the curl over the prose.
- **Dhan's `expiryTime` is IST with no timezone marker.** `generateAccessToken`
  returns e.g. `2026-09-11T19:19:40.019` alongside a JWT whose `exp` claim is
  `1789134580` (2026-09-11 13:49:40 UTC). Parsing that string as UTC is exactly
  5.5 hours too late, so a dead token reads as live and nothing alarms. Take
  expiry from the JWT `exp` claim, which is unambiguous epoch seconds. Measured
  2026-09-10; token lifetime is exactly 86,400 s.
- **Neon autosuspends.** A monthly cron always hits a cold compute, and the
  wake-up lands inside `connect()` before any SQL runs. Expect noisy first-run
  timings; it is not a code regression.

## Known gap — do not "fix" without deciding scope

`FUTIDXBSE` matches nothing against the live scrip master, here and in the
legacy system alike: its condition requires `SEM_EXCH_INSTRUMENT_TYPE = 'FUT'`,
but BSE derivatives carry `'FUTIDX'` there (only NSE uses `'FUT'`). Six SENSEX
/ BANKEX futures are present in the file and all are dropped, so `BSE_FNO`
counts are legitimately zero. Widening it is a scope change from the agreed
"replicate the 9 rules as they are" — raise it rather than silently fixing it.

## Source lineage

This is a port, not a greenfield design. Two repos outside this one are the
source of truth for behaviour that is not derivable from the code here:

- **`trading-algo`** — the legacy production system. Owns `INSTRUMENT_RULES`
  (`brokers/implementations/dhan/dhan_broker.py`), `ExchangeSegment`
  (`bin/enums/dhan.py`), and the monthly reload rule
  (`data/database_service.py`). Agreed scope is to replicate **all 9** rules,
  not narrow them to Nifty-only, even though most are irrelevant to index
  trading.
- **`AIML`** — architecture notes and the component register this naming
  follows. Its `trading_bot/migrations/` target a **different** Neon project;
  do not apply them here.

Where this repo deliberately departs from the legacy code, it is recorded under
"Porting notes" in
[instrument-master-loader/README.md](instrument-master-loader/README.md).

## Conventions

- Keep the docs in [docs/](docs/) current when component boundaries change; the
  root README indexes them and each component has its own README.
- Numbers quoted in docs are measured against live data and dated. If you
  change behaviour that moves them, re-measure rather than editing the prose.
- Secrets come from environment variables (`NEON_CONNECTION_STRING`), sourced
  from Secrets Manager or encrypted SSM at deploy time.
