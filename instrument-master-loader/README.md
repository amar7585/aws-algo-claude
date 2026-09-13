# instrument-master-loader

AWS Lambda function that refreshes `algo.instrument_master` on Neon from Dhan's
scrip master.

It sits **outside** the History → Regime → Strategy state machine, on its own
monthly EventBridge Scheduler cron. The monthly cadence comes from
`should_load_instrument_master()` in the legacy
`trading-algo/data/database_service.py`, which reloads whenever no row has
`updated_at >=` the first of the current month.

## What it does

1. Downloads Dhan's public scrip master CSV (no credentials required).
2. Keeps NSE rows, plus BSE rows whose trading symbol starts with `SENSEX`.
3. Applies all **9** `INSTRUMENT_RULES` ported from
   `trading-algo/brokers/implementations/dhan/dhan_broker.py` — equities and
   the named equity symbols, index, InvIT, REIT, ETF, index and stock futures,
   BSE index futures, and index options. Scope is deliberately *not* narrowed
   to Nifty-only.
4. Upserts the result into `algo.instrument_master`, keyed on the primary key
   `(security_id, instrument_type)`.

## Target

| | |
|---|---|
| Neon project | **AI Trader APP** (`nameless-mountain-15353651`) |
| Database | `Algo` |
| Table | `algo.instrument_master` |

Columns written: `security_id` (text), `trading_symbol`, `exchange_segment`,
`instrument_type`, `lot_units` (integer), `updated_at` (bigint).

Two conventions the rest of the system depends on:

- `exchange_segment` holds the **raw Dhan segment code** (`NSE_EQ`, `IDX_I`,
  `NSE_FNO`, `BSE_FNO`, …), never a human-readable string.
- `updated_at` is **epoch seconds**. Every time value in this database is.

## Layout

```
handler.py        entry point and orchestration
config.py         this function's tunables
rules.py          ExchangeSegment, the 9 instrument rules, row predicates
scrip_master.py   download and parse the CSV
db.py             the upsert
```

`rules.py` is kept apart from the other two because it is the piece with real
behaviour to argue about — the download and the upsert are plumbing.

`connect()` and the shared connection-string read come from the
[`neon-access`](../layers/neon-access/README.md) layer; pg8000 comes from
`neon-db-driver`. **Both layers are required.**

## Configuration

| Environment variable | Required | Default |
|---|---|---|
| `NEON_CONNECTION_STRING` | no | *unset* — read from `/algo/neon/connection` |
| `DHAN_SCRIP_MASTER_URL` | no | `https://images.dhan.co/api-data/api-scrip-master.csv` |
| `UPSERT_BATCH_SIZE` | no | `5500` |
| `DOWNLOAD_TIMEOUT_SECONDS` | no | `120` |

The connection string lives in the `/algo/neon/connection` `SecureString`, so
one parameter serves every function that talks to Neon and rotating the
database password is a single edit. `NEON_CONNECTION_STRING` still wins when
set — useful for local testing and as an escape hatch — and the log line says
which source was used, so a stale environment variable cannot quietly shadow a
rotated parameter.

That means the role needs `ssm:GetParameter` on
`arn:aws:ssm:<region>:<account>:parameter/algo/neon/connection` plus
`kms:Decrypt` via `kms:ViaService`. Note the leading slash is **not** doubled in
the ARN.

No Dhan credentials are needed: the scrip master is a public file.

## Deployment shape

- Runtime: Python 3.14, handler `handler.lambda_handler`.
- Layers: `neon-db-driver` (pg8000) **and** `neon-access` (shared helpers).
  Attach both **before** uploading the code — `handler.py` imports
  `neon_access` at module load, so the reverse order gives
  `Unable to import module 'handler': No module named 'neon_access'`.
- The function package is the five modules above and nothing else. No
  third-party code is bundled, so it stays a small zip with no Docker build.
- Memory 256 MB and a timeout of ~50 s are what it runs on today: a live run
  downloads ~25 MB and completes in about 22 s.
- Because the loader reaches the public internet, either run it outside a VPC
  or give it a NAT path.

The whole load commits once. A partially written instrument master is worse
than a stale one — downstream lookups would silently miss contracts rather
than fail loudly.

## Porting notes

Three deliberate departures from the legacy `load_instrument_master()`:

1. **No pandas, no `dhanhq`.** The filter pipeline is stdlib `csv` and plain
   dict/set logic, and the CSV is fetched with `urllib` instead of
   `dhanhq.fetch_security_list()`. Both of those dependencies pull in
   pandas/numpy, which is what pushed an earlier iteration of this project onto
   ECS Fargate. Keeping the function pure-Python is what lets it be a Lambda.
2. **`ExchangeSegment` values are inlined** rather than read off the `dhanhq`
   module constants, for the same reason. They are the Dhan API v2 segment
   codes; keep them in step with `trading-algo/bin/enums/dhan.py`.
3. **`exchange_segment` comes from the matched rule.** The legacy code derived
   it by mapping `SEM_INSTRUMENT_NAME` onto the rule *keys*, which could never
   resolve `FUTIDXBSE`. Since `FUTIDX` and `FUTIDXBSE` carry identical
   conditions and differ only by exchange, each rule now also declares the
   `SEM_EXM_EXCH_ID` it applies to.

## Known gap: BSE index futures are never loaded

`FUTIDXBSE` matches nothing against the live scrip master, in this loader and
in the legacy one alike. Its condition requires
`SEM_EXCH_INSTRUMENT_TYPE = 'FUT'`, but BSE derivatives in the CSV carry
`'FUTIDX'` there — only NSE uses `'FUT'`. The rule is wrong at source, so
fixing how the segment is *resolved* does not make it fire.

Measured on the master of 2026-09-09: 6 SENSEX/BANKEX/SENSEX50 futures
contracts are present and all are dropped. Total output was 15,338 rows —
120 `IDX_I`, 2,675 `NSE_EQ`, 12,543 `NSE_FNO`, and zero `BSE_FNO`.

Re-measured on 2026-09-11, after the module split, as the regression test for
that refactor — output is **row-for-row identical** to the pre-split version on
the same CSV, in the same order:

| | |
|---|---|
| `IDX_I` / `INDEX` | 120 |
| `NSE_EQ` / `EQUITY` | 2,669 |
| `NSE_FNO` / `FUTIDX` | 18 |
| `NSE_FNO` / `FUTSTK` | 647 |
| `NSE_FNO` / `OPTIDX` | 12,002 |
| **`BSE_FNO`** | **0** |
| **total** | **15,456** |

The drift from 15,338 is the exchange listing and expiring contracts over two
days, not a behaviour change. `BSE_FNO` stays zero.

Relaxing the condition to `{"FUT", "FUTIDX"}` would pick them up, but that is
a scope change from the agreed "replicate the 9 rules as they are", so it is
left alone deliberately. Note the BSE carve-out in `keep_row()` also only
admits `SENSEX*` symbols, so BANKEX would need widening too.
