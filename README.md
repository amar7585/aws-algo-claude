# aws-algo-claude

The **data layer** of an Indian-index algo trading system, built on AWS Lambda
and Neon Postgres.

It does not place orders. Its job is to keep a correct, replayable picture of
the market in Postgres so the decision layers above it have something
deterministic to read.

## Status

| Component | Status |
|---|---|
| [instrument-master-loader](instrument-master-loader/README.md) | **running** — 15,338 instruments loaded 2026-09-09 |
| [neon-db-driver layer](layers/neon-db-driver/README.md) | **built** |
| History / Regime / Strategy tasks | planned — see [components](docs/components.md#planned) |

## Documentation

| | |
|---|---|
| [**Architecture**](docs/architecture.md) | System context, the two planes, data store, invariants, why it is Lambda and not containers |
| [**Flow charts**](docs/flow.md) | The monthly loader flow end to end, the planned session state machine, failure handling |
| [**Components**](docs/components.md) | Component register, what is built vs planned, shared conventions, source lineage |

### Component docs

| | |
|---|---|
| [instrument-master-loader](instrument-master-loader/README.md) | Configuration, deployment, porting notes, the known `FUTIDXBSE` rule gap |
| [layers/neon-db-driver](layers/neon-db-driver/README.md) | Why pg8000 over psycopg2, build and publish steps, connecting to Neon |

## Layout

```
aws-algo-claude/
├── README.md                     you are here
├── docs/
│   ├── architecture.md           system context, invariants
│   ├── flow.md                   flow charts
│   └── components.md             component register
├── instrument-master-loader/     Lambda: monthly reference-data refresh
│   ├── handler.py
│   ├── requirements.txt
│   └── README.md
└── layers/
    └── neon-db-driver/           Lambda layer: pure-Python Postgres driver
        ├── requirements.txt
        └── README.md
```

## How it fits together

Two planes on different clocks, deliberately uncoupled:

- **Monthly** — `instrument-master-loader` refreshes reference data from Dhan's
  public scrip master. Runs on its own EventBridge cron, outside the trading
  session entirely.
- **Per trading day** *(planned)* — a History → Regime → Strategy state machine
  reads that reference data, fetches candles, classifies the session, and
  evaluates the playbooks valid for that classification.

The loader is not a step in the state machine. Reference data changes when the
exchange lists or expires contracts, roughly monthly; putting a 25 MB CSV fetch
on the critical path of every session would be paying that cost ~20× more often
than the data changes.

## Data store

Neon project **AI Trader APP**, database `Algo`, schema `algo`. Serverless
Postgres, which suits workloads that run monthly or once a day — at the cost of
a compute wake-up on the first connection of each run.

Four conventions hold everywhere, and the rest of the system depends on them:

1. **Every stored time value is epoch seconds.** No `timestamptz`, no timezone
   arithmetic in SQL.
2. **`exchange_segment` holds raw Dhan segment codes** (`NSE_EQ`, `IDX_I`,
   `NSE_FNO`), never human-readable strings.
3. **`(security_id, instrument_type)` identifies an instrument**, with
   `security_id` as `text`.
4. **No compiled dependencies.** Pure Python, or the component gets rethought —
   this is what keeps everything a zip-packaged Lambda rather than a container.

Details and table shapes: [architecture.md](docs/architecture.md#data-store).

## Getting started

Deploying the loader takes two pieces — the layer, then the function:

1. Build and publish the pg8000 layer —
   [instructions](layers/neon-db-driver/README.md#building-the-layer).
2. Zip `handler.py`, deploy it with handler `handler.lambda_handler`, attach the
   layer, and set `NEON_CONNECTION_STRING` —
   [instructions](instrument-master-loader/README.md#deployment-shape).

A healthy run returns `status: success` with ~15,338 rows. `BSE_FNO` will be
zero; that is a [known gap in the rule
set](instrument-master-loader/README.md#known-gap-bse-index-futures-are-never-loaded),
not a failure.
