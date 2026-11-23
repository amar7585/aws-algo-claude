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
| [auth-dhan-broker](auth-dhan-broker/README.md) | **running** — TOTP token to SSM, weekdays 07:30 Mon → 08:30 Fri; also the holiday gate that arms the day's schedules |
| [trading-calendar](trading-calendar/README.md) | **live** — `algo.trading_holiday`, all 16 NSE holidays for 2026 |
| [daily-market-sentiment](daily-market-sentiment/README.md) | **running** — daily candles + daily read + the morning brief, weekdays 09:35; the evening brief, 15:45 (since 2026-11-23) |
| [neon-db-driver layer](layers/neon-db-driver/README.md) | **built** — pg8000 |
| [neon-access layer](layers/neon-access/README.md) | **built** — shared epoch/IST, Neon connection, SSM reads |
| [market-classifier layer](layers/market-classifier/README.md) | **built** — ONE classification, run on daily candles and on 5-minute candles |
| [intraday-data-loader](intraday-data-loader/README.md) | **running** — 5/15/60-min candles, every 15 min 09:45–15:35. **The only intraday cron**: it invokes the sentiment function, which chains to the classifier, detector and manager |
| [error-notifier](error-notifier/README.md) | **running** — failures from every function to Telegram |
| [intraday-fno-poller](intraday-fno-poller/README.md) | **running** (live since 2026-10-19) — stores the 5-minute F&O reading (futures price/OI/volume, per-bar OI change, both option chains, ATM ±2 legs) → `fno_5m_snapshot` / `fno_5m_leg`. Own cron, 71×/day. Since 2026-11-09 it hands each committed tick to `pattern-recorder` (unset = off) ([plan](docs/phase-1-fno-5m-poller-plan.md)) |
| [intraday-market-sentiment](intraday-market-sentiment/README.md) | **deployed** — **measures** basis/OI, VIX, two option chains and SMA/RSI → `intraday_fno_data`. Invoked by the loader, 25×/day |
| [market-classifier](market-classifier/README.md) | **deployed** — **judges**: runs the shared layer over the measurement → regime/structure/buildup/day_trend on `intraday_sentiments`, invokes the detector |
| [pattern-detector](pattern-detector/README.md) | **deployed, log-only since 2026-11-09 16:04** — the two-clock turn rule (abnormal-volume reversal + buildup/OI confirm). It still decides and logs its alerts, but sends no Telegram and invokes no manager |
| [pattern-recorder](pattern-recorder/README.md) | **v2.1 sending since 2026-11-09 16:04, rules v2.2 since 2026-11-19 18:07** (v2.0 recorded from 2026-11-06) — pattern-detector v2: candle patterns at levels (index PDH/PDL and opening range, 15-min swings, option walls) on the future → `pattern_events`. On each 5-min poller tick; key-level alerts filtered by the classifier's `day_trend` go to Telegram and on to the strategy plane, once each. Since 2026-11-14 it also hands **every** tick on (`TICK_HANDOFF_ENABLED=on`). Both went to strategy-manager until 2026-11-23 17:22; since then they go to **risk-manager** |
| [risk-manager](risk-manager/README.md) | **live since 2026-11-23 17:22 IST** (P2b; first session Wed 2026-11-25) — **the one risk manager**, in front of strategy-manager on every alert and tick. It reads `risk_control` (the kill switch, which resets each morning; the O1 limits since P3, 2026-11-23 18:42 IST: max open 4, 8 trades a day, daily loss 500 point-lots) and today's `position_master`. Kill switch on: it closes every open position through order-manager and no playbook runs. Otherwise it sets each playbook's mode — `entry_exit`, `exit_only` (after 15:00, or a limit), `force_exit` (the 15:15 time exit at the 15:10 close, or the daily-loss exit) — and hands the payload on with it as `run`. Writes no table |
| [strategy-manager](strategy-manager/README.md) | **built** — a **pure router**, behind risk-manager since 2026-11-23 (context v4: it refuses a payload without `run`, and each playbook gets its own mode), on each v2.1 alert: `day_trend` up/down → `TREND_PLAYBOOKS` (`strategy-trend-pullback`); `none` → nothing (the regime\|bias registry went with bias, 2026-11-19); on **every 5-min tick** (since 2026-11-14) → `TICK_PLAYBOOKS` (`strategy-range-liquidity-sweep`) whatever the regime |
| [strategy-range-liquidity-sweep](strategy-range-liquidity-sweep/README.md) | **sending since 2026-11-14 18:25** (first live session 2026-11-16) — the 15-min opening-range sweep playbook, run on **every 5-min tick**: re-derives the session (one rule set since 2026-11-19 — the old `current` set was removed), follows each trade after entry (exit, watch, re-entry; out at 15:15 before the closing auction since 2026-11-20 22:48; since 2026-11-22 15:44, 3 lots with 2 booked at T1, the 15-min range midpoint, and the last lot to T2 on an unmoved stop), takes entries only when price is **within the levels** (PDH/PDL, 15-min high/low) and volatility is not high (regime not `volatile-expansion`) — no bias, no trend test (since 2026-11-15 09:33) — and records its **alert register** to `playbook_alerts` (since 2026-11-14 22:01). Since P2a (2026-11-22 22:16) every entered trade is recorded in `position_master` through order-manager; since P2b (2026-11-23) its messages go through order-manager too, and after 15:00 / at 15:15 it acts on the risk manager's mode |
| [strategy-trend-pullback](strategy-trend-pullback/README.md) | **sending since 2026-11-11 16:56** (shadow from 2026-11-09 22:33) — the trend-day hold playbook: a v2 hold (C2/R1) at PDH/PDL/ORH/ORL — or, since C1 (2026-11-23), at the call / put wall — with the day trend → entry, stop on the trend-off line, to Telegram and its log. **Since P2b (2026-11-23) it follows its trades:** the entry goes through order-manager (3 lots, T1 = entry + 2R with 2 sold there), and on every tick while it holds a position it applies the decided "Previous-candle trail" (stop to entry at +1R, then the previous 5-min candle). Reads and writes no table itself |
| [order-manager](order-manager/README.md) | **running** — deployed 2026-11-22 21:16 IST (P1); called by the sweep since P2a (2026-11-22 22:16), by trend and risk-manager since P2b (2026-11-23) — the one common order manager. One signal per synchronous call → `position_master` (paper mode: no order is saved, the step updates the position) + the caller's own message, sent once in the same transaction. The only writer of `position_master` and, since P2b, the only sender of trade messages |

## Documentation

| | |
|---|---|
| [**Architecture**](docs/architecture.md) | System context, the two planes, data store, invariants, why it is Lambda and not containers |
| [**Flow charts**](docs/flow.md) | A trading day end to end, each plane in detail, how a failure reaches you |
| [**Components**](docs/components.md) | Component register, what is built vs planned, shared conventions, source lineage |
| [**Future implementation**](docs/future-implementation.md) | Agreed 2026-11-20: the closing-auction rule for every playbook, the risk manager (open trades, new-trade limits, kill switch), trade management. Since 2026-11-22: the paper-position plan decided. P1 (`order-manager` + `position_master`), P2a (the sweep records its trades) and P2b (`risk-manager` in front, trend follows its trades, messages through `order-manager`) are live by 2026-11-23 |

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
| [strategy-manager](strategy-manager/README.md) | Why it is invoked and not scheduled, why it reads nothing at all, the payload contract (v4 since P2b, 2026-11-23: the risk manager's `run`; v3 since bias was removed, 2026-11-19) |
| [strategy-range-liquidity-sweep](strategy-range-liquidity-sweep/README.md) | The gate and why 0.9 became 0.78, three disagreements inside the playbook, why it needs no memory, every-tick runs with the trade after entry (one rule set since 2026-11-19; 2 of 3 lots booked at the range midpoint since 2026-11-22, with the split replay behind it), and the alert register (`playbook_alerts`) |
| [strategy-trend-pullback](strategy-trend-pullback/README.md) | The 2026-11-06 reference session, the rules (setup, window, S3 stop, T1/T2, no RR gate), the replay behind them, the shadow switch, and since P2b the trade after entry (the decided trail, reproduced on all 166 replayed trades) |
| [order-manager](order-manager/README.md) | The contract (one signal per synchronous call), the state machine (a step applies only if it moves the position forward), units = lots × the near-month NIFTY future's lot size, the offline and Neon verification, the measured time per call (~1.5 s at P1; 1.33 s warm with the connection kept, P2b) |
| [risk-manager](risk-manager/README.md) | The hard rules and limits it owns, the kill switch that resets each morning, the `run` block, the INSERTs that set them, the deploy and its rollbacks |
| [layers/neon-db-driver](layers/neon-db-driver/README.md) | Why pg8000 over psycopg2, build and publish steps, connecting to Neon |
| [layers/neon-access](layers/neon-access/README.md) | What is shared and why, and the cost of layer version pinning |
| [layers/market-classifier](layers/market-classifier/README.md) | The taxonomy, the swing read, the volatility tests, the score terms removed with bias (2026-11-19, kept as the record), and the decisions deferred until sessions accumulate |

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
├── daily-market-sentiment/       Lambda: daily candles, daily read, morning + evening brief
│   ├── handler.py                entry point
│   ├── config.py params.py       tunables; the Dhan and Telegram parameters
│   ├── dhan.py db.py             charts client; Neon upserts
│   ├── indicators.py sentiment.py  SMA/RSI/ATR; regime, the read
│   ├── width_profile.py          the normal 5-min width per clock slot
│   ├── notify.py                 Telegram
│   ├── schema.sql                algo.daily_market_sentiment, algo.intraday_width_profile
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
├── risk-manager/            Lambda: the one risk manager, in front of strategy-manager (P2b)
│   ├── handler.py                entry point: kill switch -> close all; else hand on with `run`
│   ├── rules.py                  the decision (pure): kill reset, 15:00, the time exit, limits
│   ├── db.py ordermgr.py         the reads (risk_control, positions, holidays); the sync call
│   ├── config.py schema.sql      the run version, the hard-rule times; algo.risk_control
│   ├── iam/                      its own role's policies
│   └── README.md
├── order-manager/           Lambda: the one common order manager - the only writer of position_master
│   ├── handler.py                one signal per call, one transaction, the kept connection
│   ├── positions.py              the state machine (pure)
│   ├── db.py notify.py           the row lock + insert / update; the Telegram send
│   ├── config.py schema.sql      the contract version; algo.position_master
│   ├── iam/                      its own role's policies
│   └── README.md
├── strategy-manager/        Lambda: a pure router - reads, computes and writes nothing
│   ├── handler.py                entry point: tick -> TICK_PLAYBOOKS, alert -> day_trend
│   ├── config.py                 the payload contract, TREND_PLAYBOOKS, TICK_PLAYBOOKS
│   ├── registry.py               the trend shortlist and the async invoke
│   └── README.md
├── strategy-range-liquidity-sweep/  Lambda: the opening-range sweep playbook, every 5-min tick
│   ├── handler.py                entry point: reads, derive, messages, then the register write
│   ├── session.py                the session candle by candle, the messages
│   ├── manage.py                 the new rules' trade after entry: exit, watch, re-entry
│   ├── register.py               the alert register: what became of every level touched
│   ├── config.py                 every threshold, which are not from the playbook, REGISTER_ENABLED
│   ├── levels.py                 the liquidity pools, and which of them stack
│   ├── sweep.py                  detection, acceptance, push sweeps, Case A/B/C triage
│   ├── trade.py                  entry, stop, targets, risk-reward, grade
│   ├── gate.py report.py         the fine gate; the playbook's output blocks
│   ├── db.py                     the Neon reads (and today's positions) + the register upsert
│   ├── record.py ordermgr.py     the signals for order-manager (P2a / P2b); the sync call
│   ├── notify.py                 the Telegram send - unused since P2b (order-manager sends)
│   ├── schema.sql                algo.playbook_alerts, for a fresh database
│   └── README.md
├── strategy-trend-pullback/     Lambda: the trend-day hold playbook
│   ├── handler.py                entry point, shadow vs send
│   ├── config.py                 every tunable, TELEGRAM_ENABLED
│   ├── pullback.py               the gate, levels, entry, stop
│   ├── exits.py record.py        the decided trail after entry; the signals (P2b)
│   ├── report.py ordermgr.py     the message; the sync call to order-manager
│   ├── notify.py                 the Telegram send - unused since P2b
│   ├── iam/                      its role's policies, and the manager's invoke grant
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
    └── market-classifier/        Lambda layer: structure, regime, volatility
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
- **Each weekday at 09:35** — `daily-market-sentiment` fetches daily candles for
  NIFTY and INDIA VIX, computes the daily read from them, and sends the morning
  brief to Telegram (since 2026-11-22: the gap, the open daily levels, today's
  option-chain pain points read live, the VIX move). Then it builds today's
  `intraday_width_profile` — the normal 5-min candle width at each clock slot
  over the previous 20 sessions (2026-11-12).
- **Each weekday at 15:45** (since 2026-11-23) — the same function, on a second
  schedule, sends the evening brief: the close, VIX, the levels filled, the
  option chain, the day's trend, the alerts and the paper trades, tomorrow's
  levels. It only reads. Both schedules are in `auth-dhan-broker`'s holiday gate.
- **Every 15 minutes from 09:45 to 15:30, plus a 15:35 closing sweep** —
  `intraday-data-loader` fills `candle_5min`, `candle_15min` and `candle_1hr`
  for NIFTY and the current-month future. 25 invocations a trading day, and
  **the only cron in the intraday plane**: it invokes the sentiment function,
  which chains to the classifier, the detector and the manager.
- **After each loader run, 25×/day** — `intraday-market-sentiment` **measures**
  the market into `intraday_fno_data`: futures basis and OI, INDIA VIX, the
  option-chain read for the nearest and monthly expiries, and the SMA/RSI. It
  shares no tables with the loader — it fetches its own candles and chains, so a
  stalled loader cannot feed it stale inputs.
- **Then `market-classifier`** scores that measurement with the shared layer
  and writes the **judgement** — regime, structure, buildup, day_trend — to
  `intraday_sentiments` (including `day_trend`), and invokes `pattern-detector`.
  Its volatility test judges the last 30 minutes against the morning's width
  profile (2026-11-12; see its README).
- **Then `pattern-detector`** applies the two-clock turn rule (an abnormal-volume
  reversal on the future, confirmed a tick later by futures buildup or option
  OI). **Since 2026-11-09 16:04 it is log-only:** it decides and logs, but sends
  no Telegram and invokes no manager.
- **Every 5 minutes, on its own clock** — `intraday-fno-poller` writes the F&O
  reading and, as its last step, hands the tick to `pattern-recorder`
  (asynchronously, unset = off). v2.1 re-derives the session's pattern events
  and upserts them to `pattern_events`. Each new event at a key level (on a trend
  day also at the call / put wall, since C1 2026-11-23) that the
  `day_trend` rule allows goes to Telegram and — through `risk-manager` since
  2026-11-23 — to `strategy-manager`, once each.
  The manager routes on the alert's `day_trend` first: up or down goes to
  `strategy-trend-pullback`, the trend-day hold playbook (sending to Telegram
  since 2026-11-11). On `none` it routes nothing: the range playbook, the
  sweep, runs on every tick instead (the `regime|bias` registry that used to
  decide this dispatched nothing, and went with bias on 2026-11-19). **Since 2026-11-14 the
  recorder also hands every tick to the manager**, which sends it to
  `strategy-range-liquidity-sweep` whatever the regime: the sweep re-derives the
  session, hands the newest candle's entries and trade steps to `order-manager`
  (which records them in `position_master` and sends them to Telegram), and
  upserts its alert register to `playbook_alerts`. **Since 2026-11-23 (P2b)
  `risk-manager` sits in front of all of it:** it reads the kill switch and the
  limits (`risk_control`) and today's positions, and gives each playbook its
  mode — no new trade after 15:00, every trade out at the 15:10 close. Trend
  follows its own trades on the ticks while it holds one. `pattern_events`, that
  register and `position_master` (written only by `order-manager`) are the
  tables the strategy plane writes.
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
