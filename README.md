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
| [auth-dhan-broker](auth-dhan-broker/README.md) | **running** — TOTP token to SSM, weekdays 08:00; also the holiday gate that arms the day's schedules |
| [trading-calendar](trading-calendar/README.md) | **live** — `algo.trading_holiday`, all 16 NSE holidays for 2026 |
| [daily-market-sentiment](daily-market-sentiment/README.md) | **running** — daily candles + daily read + Telegram, weekdays 09:50 |
| [neon-db-driver layer](layers/neon-db-driver/README.md) | **built** — pg8000 |
| [neon-access layer](layers/neon-access/README.md) | **built** — shared epoch/IST, Neon connection, SSM reads |
| [market-classifier layer](layers/market-classifier/README.md) | **built** — ONE classification, run on daily candles and on 5-minute candles |
| [intraday-data-loader](intraday-data-loader/README.md) | **running** — 5/15/60-min candles, every 15 min 10:00–15:35. **The only intraday cron**: it invokes the sentiment function, which invokes the manager |
| [error-notifier](error-notifier/README.md) | **running** — failures from every function to Telegram |
| [intraday-market-sentiment](intraday-market-sentiment/README.md) | **deployed** — basis/OI buildup, VIX, two option chains, **and the stored classification**. Invoked by the loader, 24×/day |
| [strategy-manager](strategy-manager/README.md) | **built** — a **pure router**: reads `regime\|bias` off the snapshot and invokes the playbooks that combination allows |
| [strategy-range-liquidity-sweep](strategy-range-liquidity-sweep/README.md) | **built** — the 15-min opening-range sweep playbook |

## Documentation

| | |
|---|---|
| [**Architecture**](docs/architecture.md) | System context, the two planes, data store, invariants, why it is Lambda and not containers |
| [**Flow charts**](docs/flow.md) | A trading day end to end, each plane in detail, how a failure reaches you |
| [**Components**](docs/components.md) | Component register, what is built vs planned, shared conventions, source lineage |

### Component docs

| | |
|---|---|
| [instrument-master-loader](instrument-master-loader/README.md) | Configuration, deployment, porting notes, the known `FUTIDXBSE` rule gap |
| [auth-dhan-broker](auth-dhan-broker/README.md) | Token refresh flow, why the holiday decision lives here, why `UpdateSchedule` replaces rather than patches, IAM, measured API findings |
| [trading-calendar](trading-calendar/README.md) | Why a row means closed and absence means trading, why `trade_date` is a `date` and not epoch, reseeding, the 2026-12-31 coverage cliff |
| [daily-market-sentiment](daily-market-sentiment/README.md) | The measured Dhan API facts, why `expected_move` comes from VIX, why the score is not a forecast |
| [intraday-data-loader](intraday-data-loader/README.md) | The one-line schedule rule, why partial candles are stored, how the current-month future resolves itself |
| [error-notifier](error-notifier/README.md) | Why a log subscription beats a catch block, the feedback-loop guard, noise suppression |
| [intraday-market-sentiment](intraday-market-sentiment/README.md) | Why the five-minute offset, the one thing it reads back from Postgres, the two strike widths, the `open_interest` naming trap |
| [strategy-manager](strategy-manager/README.md) | Why it is invoked and not scheduled, why it reads nothing at all, the v2 payload contract, the `regime\|bias` registry |
| [strategy-range-liquidity-sweep](strategy-range-liquidity-sweep/README.md) | The gate and why 0.9 became 0.78, three disagreements inside the playbook, why it needs no memory and no layers |
| [layers/neon-db-driver](layers/neon-db-driver/README.md) | Why pg8000 over psycopg2, build and publish steps, connecting to Neon |
| [layers/neon-access](layers/neon-access/README.md) | What is shared and why, and the cost of layer version pinning |
| [layers/market-classifier](layers/market-classifier/README.md) | The taxonomy, the score terms, the swing read, the time-scaled volatility test, and the three decisions deferred until sessions accumulate |

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
├── error-notifier/               Lambda: failures from every function to Telegram
│   ├── handler.py                decode, loop guard, suppression, formatting
│   ├── config.py notify.py       tunables; Telegram
│   └── README.md
├── intraday-data-loader/         Lambda: 5/15/60-min candles through the session
│   ├── handler.py                entry point, schedule rule, resume, alignment guard
│   ├── config.py params.py       tunables; the Dhan token and client id
│   ├── dhan.py db.py             charts + expiry client; the candle upsert
│   ├── requirements.txt
│   └── README.md
├── intraday-market-sentiment/    Lambda: the 15-minute market read
│   ├── handler.py                entry point, alignment, the one baseline read
│   ├── config.py params.py       tunables; the Dhan token and client id
│   ├── dhan.py                   charts + expiry list + option chain
│   ├── expiry.py chain.py        nearest/monthly selection; ATM, PCR, max pain
│   ├── sentiment.py              session stats, the buildup label
│   ├── db.py schema.sql          the two-table write; the schema
│   ├── requirements.txt
│   └── README.md
├── strategy-manager/        Lambda: the regime/bias gate and the dispatch
│   ├── handler.py                entry point, the freshness assertion
│   ├── config.py params.py       tunables and the registry; the Dhan token
│   ├── dhan.py db.py             the live price; the Neon reads - no writes
│   ├── indicators.py             SMA/RSI/ATR/VWAP, pure Python
│   ├── classify.py               bias, regime, score, confidence on 15-min
│   ├── registry.py               regime -> strategies, and the async invoke
│   └── README.md
├── strategy-range-liquidity-sweep/  Lambda: the opening-range sweep playbook
│   ├── handler.py                entry point, the session replay
│   ├── config.py                 every threshold, and which are not from the playbook
│   ├── levels.py                 the liquidity pools, and which of them stack
│   ├── sweep.py                  detection, acceptance, Case A/B/C triage
│   ├── trade.py                  entry, stop, targets, risk-reward, grade
│   ├── gate.py report.py         the fine gate; the playbook's output blocks
│   └── README.md
├── trading-calendar/             reference data: exchange holidays (no Lambda)
│   ├── schema.sql                algo.trading_holiday
│   ├── seed_2026.sql             16 holidays, from NSE's circular
│   ├── generate_seed.py          transcription cross-check — laptop only
│   └── README.md
└── layers/
    ├── neon-db-driver/           Lambda layer: pure-Python Postgres driver
    │   ├── requirements.txt
    │   └── README.md
    ├── neon-access/              Lambda layer: shared epoch/IST, connect, SSM
    └── market-classifier/        Lambda layer: bias, structure, regime, volatility
        ├── python/neon_access/
        └── README.md
```

## How it fits together

Two planes on different clocks, deliberately uncoupled:

- **Monthly** — `instrument-master-loader` refreshes reference data from Dhan's
  public scrip master. Runs on its own EventBridge cron, outside the trading
  session entirely.
- **Each weekday morning** — `auth-dhan-broker` decides whether the day happens.
  It reads `algo.trading_holiday`: on a holiday it **disables** the daily and
  intraday schedules and stops without minting anything; otherwise it mints a
  Dhan access token via TOTP, writes it to SSM, and **enables** those schedules.
  So no session function authenticates itself, nothing is refreshed by hand, and
  nothing is invoked on a holiday at all.
- **Each weekday at 09:50** — `daily-market-sentiment` fetches daily candles for
  NIFTY and INDIA VIX, computes the daily read from them, and pushes it to
  Telegram.
- **Every 15 minutes from 10:00 to 15:30, plus a 15:35 closing sweep** —
  `intraday-data-loader` fills `candle_5min`, `candle_15min` and `candle_1hr`
  for NIFTY and the current-month future. 24 invocations a trading day, and
  **the only cron in the intraday plane**: it invokes the sentiment function,
  which invokes the manager, which invokes the playbooks.
- **After each loader run, 24×/day** — `intraday-market-sentiment` writes
  one row describing the market: futures basis and OI buildup, INDIA VIX, and
  the option-chain read for the nearest and monthly expiries at once. It shares
  no tables with the loader — it fetches its own candles and chains, so a
  stalled loader cannot feed it stale inputs. 24 invocations a trading day.
- **On each snapshot** — `intraday-market-sentiment` invokes
  `strategy-manager` asynchronously with the row it just wrote. The
  manager classifies the 15-minute regime, shortlists the playbooks valid
  for it, and invokes those — today `strategy-range-liquidity-sweep` on a range
  day, and nothing at all on a trending one. Neither function writes to
  Postgres.
- **On failure, and only on failure** — `error-notifier` picks errors out of
  every function's CloudWatch log group and pushes them to Telegram. It catches
  timeouts and import errors too, which no `try/except` inside a function can
  see.

**Nothing sequences these, with one deliberate exception.** The strategy plane
is chained rather than scheduled, because the manager's input *is* the
snapshot — see [architecture.md](docs/architecture.md). Every invoke in that
chain is asynchronous, so no function can be failed by something downstream of
it. An earlier design had a Step Functions state
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
