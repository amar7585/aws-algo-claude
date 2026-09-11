# aws-algo-claude

The **data layer** of an Indian-index algo trading system, built on AWS Lambda
and Neon Postgres.

It does not place orders. Its job is to keep a correct, replayable picture of
the market in Postgres so the decision layers above it have something
deterministic to read.

## Status

| Component | Status |
|---|---|
| [instrument-master-loader](instrument-master-loader/README.md) | **running** — 15,463 instruments, monthly |
| [auth-dhan-broker](auth-dhan-broker/README.md) | **running** — TOTP token to SSM, weekdays 08:00 |
| [daily-market-sentiment](daily-market-sentiment/README.md) | **running** — daily candles + daily read + Telegram, weekdays 09:50 |
| [neon-db-driver layer](layers/neon-db-driver/README.md) | **built** — pg8000 |
| [neon-access layer](layers/neon-access/README.md) | **built** — shared epoch/IST, Neon connection, SSM reads |
| Intraday task | planned — see [components](docs/components.md#planned) |

## Documentation

| | |
|---|---|
| [**Architecture**](docs/architecture.md) | System context, the two planes, data store, invariants, why it is Lambda and not containers |
| [**Flow charts**](docs/flow.md) | The monthly loader flow end to end, failure handling |
| [**Components**](docs/components.md) | Component register, what is built vs planned, shared conventions, source lineage |

### Component docs

| | |
|---|---|
| [instrument-master-loader](instrument-master-loader/README.md) | Configuration, deployment, porting notes, the known `FUTIDXBSE` rule gap |
| [auth-dhan-broker](auth-dhan-broker/README.md) | Token refresh flow, why there is no renew path, IAM, measured API findings |
| [daily-market-sentiment](daily-market-sentiment/README.md) | The measured Dhan API facts, why `expected_move` comes from VIX, why the score is not a forecast |
| [layers/neon-db-driver](layers/neon-db-driver/README.md) | Why pg8000 over psycopg2, build and publish steps, connecting to Neon |
| [layers/neon-access](layers/neon-access/README.md) | What is shared and why, and the cost of layer version pinning |

## Layout

```
aws-algo-claude/
├── README.md                     you are here
├── docs/
│   ├── architecture.md           system context, invariants
│   ├── flow.md                   flow charts
│   └── components.md             component register
├── instrument-master-loader/     Lambda: monthly reference-data refresh
│   ├── handler.py                entry point
│   ├── config.py rules.py        tunables; the 9 instrument rules
│   ├── scrip_master.py db.py     CSV download/parse; the upsert
│   ├── requirements.txt
│   └── README.md
├── auth-dhan-broker/             Lambda: keeps a live Dhan token in SSM
│   ├── handler.py
│   └── README.md
├── daily-market-sentiment/       Lambda: daily candles, daily read, Telegram
│   ├── handler.py                entry point
│   ├── config.py params.py       tunables; the Dhan and Telegram parameters
│   ├── dhan.py db.py             charts client; Neon upserts
│   ├── indicators.py sentiment.py  SMA/RSI/ATR; regime, score, the read
│   ├── notify.py                 Telegram
│   ├── schema.sql                algo.daily_market_sentiment
│   └── README.md
└── layers/
    ├── neon-db-driver/           Lambda layer: pure-Python Postgres driver
    │   ├── requirements.txt
    │   └── README.md
    └── neon-access/              Lambda layer: shared epoch/IST, connect, SSM
        ├── python/neon_access/
        └── README.md
```

## How it fits together

Two planes on different clocks, deliberately uncoupled:

- **Monthly** — `instrument-master-loader` refreshes reference data from Dhan's
  public scrip master. Runs on its own EventBridge cron, outside the trading
  session entirely.
- **Each weekday morning** — `auth-dhan-broker` mints a Dhan access token via
  TOTP and writes it to SSM, so no session function authenticates itself and
  nothing is refreshed by hand.
- **Each weekday at 09:50** — `daily-market-sentiment` fetches daily candles for
  NIFTY and INDIA VIX, computes the daily read from them, and pushes it to
  Telegram.
- **Every 5 minutes during market hours** *(planned)* — the intraday task fills
  `candle_5min` and its aggregates.

**Nothing sequences these.** An earlier design had a Step Functions state
machine running History → Regime → Strategy; it was never built. Each component
runs on its own EventBridge cron instead, which keeps failure domains apart — a
failed token refresh raises its own alarm rather than failing a trading session.

The loader in particular is not on any session path: reference data changes when
the exchange lists or expires contracts, roughly monthly, and putting a 25 MB
CSV fetch on the critical path of every session would pay that cost ~20× more
often than the data changes.

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
4. **Prefer pure Python.** Zip-packaged Lambda is the default shape, so stdlib
   or pure-Python packages come first; a compiled dependency needs a reason and
   a measured size rather than being ruled out outright.

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
