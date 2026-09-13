# strategy-manager

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Components](../docs/components.md)

Decides which playbooks are valid for the market as it stands, and invokes
them. It evaluates no playbook itself, emits no signal, and writes nothing.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 7 modules |
| Layers | `neon-db-driver` + `neon-access` |
| Trigger | **asynchronous invoke from `intraday-market-sentiment`** — no schedule |
| Reads | `algo.candle_15min`, `algo.candle_5min`, `algo.candle_daily`, `algo.daily_market_sentiment`, `algo.instrument_master` |
| Writes | **nothing** |
| Calls | `/v2/charts/intraday` once, for the live price |
| Secrets | `/algo/dhan/token`, `/algo/neon/connection` |

## The chain

```
intraday-market-sentiment  --(Event: the snapshot it just wrote)-->  strategy-manager
strategy-manager      --(Event: the context)-->                 strategy-range-liquidity-sweep, ...
```

**There is no cron here, and that is the design.** The manager's input
*is* the snapshot, and the snapshot exists only once
`intraday-market-sentiment` has written it. A schedule on this side would have
to guess how long that takes, read the row back out of Postgres, and decide
what to do when it is not there yet — three problems that all vanish when the
completion of the write is itself the trigger. There is consequently no
staleness window to configure and no race with the writer.

It follows that this function runs exactly as often as the snapshot does: 25
invocations a trading day, at 09:35, 09:50, 10:05 … 15:35 IST.

## Why it reads the loader's tables

`intraday-market-sentiment` deliberately shares no tables with
`intraday-data-loader`, so a stalled loader cannot feed it stale inputs. **This
function gives that up on purpose.**

The reason that rule does not transfer: a snapshot row is never revisited, so
a stale input there would be wrong forever. This function persists nothing, so
a stale SMA costs one routing decision that the next run corrects fifteen
minutes later. Re-fetching the history from Dhan on every run to avoid that
would leave the loader filling tables its only real consumer ignores.

The history is genuinely there. Measured 2026-09-12 against the live database:
`algo.candle_15min` holds **1,600 bars for NIFTY alone**, from 2026-06-15 to
2026-09-11 — eight times the 200 that `sma200` needs on the 15-minute frame.

**A stalled loader raises.** `assert_candles_fresh()` compares the newest
closed bar against the snapshot it was handed and allows exactly one bar of
lag, because `intraday-data-loader` fires every five minutes and
`intraday-market-sentiment` at :35/:50/:05/:20 — so the loader's write of that
bar and this function's read of it land in the same minute, and demanding
equality would raise on the ordinary case. More than one bar is not a race; it
is a loader that has stopped, and the SMAs would then be computed from a series
that ends before the market does.

## Why it still calls Dhan

`snapshot_ts` is the last **closed** 5-minute bar, so it is up to five minutes
old by construction, and `candle_5min`'s newest closed bar is the same bar. The
routing question is what price is doing *now* — whether it is back inside a
level it swept four minutes ago — and neither stored source can answer it.

One call to `/v2/charts/intraday`, and it deliberately keeps the bar every
other read in this repo filters out: the one still forming. `live.partial` says
which it is, because the sweep playbook cares a great deal about the difference
between a close back inside a level and a wick that has not closed yet.

## The classification

Ported from `trading-algo/helpers/sentiment_builder.py::build_intraday_sentiment`,
rule for rule, in pure Python.

**It is not `detect_market_regime()`.** That helper is the *daily* path — it is
what `daily-market-sentiment/sentiment.py` ports, it reads `atr_14` and SMA
slopes over recent daily bars, and it returns a third value, `TRANSITION`. The
intraday path never calls it and never produces `TRANSITION`: the intraday
regime comes out of a two-branch test and is `TREND` or `RANGE` by
construction. There is no missing enum value and nothing to fold.

| Output | Rule |
|---|---|
| `score` | −3…+3, one point each way from `close>sma20>sma50`, `close>sma100>sma200`, `rsi≥55`/`≤45` |
| `bias` | `BULLISH` if score ≥ 2, `BEARISH` if ≤ −2, else `NEUTRAL` |
| `regime` | `TREND` if `\|sma20−sma50\|/close > 0.001` **and** price agrees with bias **and** bias ≠ NEUTRAL; else `RANGE` |
| `confidence` | `\|score\|×25`, +20 if TREND, +15 if RSI agrees with bias, + `min(volume_strength×20, 40)`, capped at 100 |

Verified against the real 15-minute bar of 2026-09-11 (close 23,398.10, and
SMAs computed independently by Postgres over the same rows: sma20 23,364.28,
sma50 23,388.42, sma100 23,488.80, sma200 23,684.70):

| RSI | score | bias | regime | confidence |
|---|---|---|---|---|
| 40 | −2 | BEARISH | RANGE | 65.19 |
| 50 | −1 | NEUTRAL | RANGE | 25.19 |

`classify()` **raises** rather than classifying on a short history. That is the
one deliberate departure from the legacy builder, which pushes every indicator
through a `safe_value()` mapping NaN to `0.0`: below 200 bars that makes
`close > sma100 > sma200` read as `close > 0 > 0` — False — so the longer-SMA
test silently contributes nothing, the score caps at ±2, and bias then needs
both surviving tests to agree. Nothing raises and nothing looks wrong.
`daily-market-sentiment` already defends against exactly this with `sma200 NOT
NULL`; this is the same defence, earlier.

## The registry

`config.STRATEGY_REGISTRY` maps regime → the strategy functions valid for it,
as JSON in the environment so adding a playbook is a configuration change
rather than a redeployment.

```json
{"RANGE": ["strategy-range-liquidity-sweep"], "TREND": []}
```

`TREND` is deliberately **present and empty** rather than absent, and the
difference is load-bearing: a regime that maps to `[]` is "no playbook is valid
on this kind of day", which is the correct answer on a trending day for every
playbook built so far. A regime that is *missing from the map* raises, because
routing nothing would otherwise look identical to a correct stand-down.

**The gate here is coarse on purpose.** It answers "is this playbook valid for
this kind of day at all". The fine gate — VIX behaviour, how much range the day
has spent, opening-range width, time of day — belongs inside each strategy,
because only the strategy knows what its own playbook requires, and because a
strategy that trusts an upstream gate it cannot see will fire on a bad day the
moment that gate moves. A strategy re-checking the regime is not redundancy.

## The payload contract

`config.CONTEXT_VERSION` is an interface. Every strategy asserts the version it
was written against and raises on anything else, because a context that has
moved on would leave a strategy reading a key that is no longer there, getting
`None`, and gating on it — and a gate that passes because its input vanished is
the worst failure a playbook can have.

```
context_version   int, asserted by the consumer
dispatched_at     epoch seconds
instrument        security_id, trading_symbol, exchange_segment, instrument_type
snapshot          intraday-market-sentiment's row, carried through untouched
daily             the algo.daily_market_sentiment row for today, or null
daily_atr14       Wilder true ATR14 on daily bars, or null
classification    the table above, plus its inputs
live              {ts, price, high, low, partial}
candles           {interval_minutes, as_of, newest_ts, bars[]}
```

`snapshot` is passed through rather than unpacked, so a column added upstream
needs no change here.

**Payload size is measured, not estimated.** The real 2026-09-11 session — 75
five-minute bars plus the snapshot, the daily row and the classification —
serialises to **8,212 bytes**, 3.1% of Lambda's 256 KB asynchronous cap. At the
configured 200 bars that extrapolates to roughly 22 KB. `dispatch()` checks the
size before every invoke all the same, because exceeding it raises a boto3
error that names bytes and not the reason.

## Dispatch

Asynchronous (`Event`). This function returning is **not** a claim that any
strategy succeeded — each has its own log group with `error-notifier` watching
it. A synchronous invoke would fold every strategy's runtime into this
function's timeout and let one slow playbook delay the rest.

Every invoke is attempted before anything raises. Raising on the first failure
would hide the state of the others: with two eligible strategies and a typo in
the first name, the second would never be called and the log would name only
the typo.

## Deployment shape

Zip `handler.py`, `config.py`, `params.py`, `dhan.py`, `db.py`,
`indicators.py`, `classify.py` and `registry.py` at the **zip root** — not in a
subfolder. Handler is `handler.lambda_handler`, set under Code → Runtime
settings → Edit. Attach `neon-db-driver` and `neon-access`.

**Its own execution role. Never reuse another function's.** Both existing roles
in this account are scoped to a single function and both fail *silently* when
borrowed — a console-generated execution policy allows `logs:PutLogEvents` on
one log group ARN only, so a second function using it runs correctly and writes
its rows while producing **no logs at all**, and with no logs `error-notifier`
can never report a failure in it. The policy needs:

- `logs:CreateLogStream` + `logs:PutLogEvents` on **this** function's log group
- `ssm:GetParameter` on `/algo/dhan/token` and `/algo/neon/connection`, plus
  `kms:Decrypt` on the key behind them
- `lambda:InvokeFunction` on **each strategy function's ARN**, not a wildcard

**No EventBridge Scheduler role is needed**, which is the one upside of being
invoke-triggered: the second of the two silent IAM traps — a scheduler role
scoped to a single function ARN, which creates a schedule that shows `ENABLED`
and simply never fires — cannot apply here.

Subscribe this function's log group to `error-notifier`, and never subscribe
`error-notifier`'s own group to itself.

## Switching the chain on

`intraday-market-sentiment` gained `dispatch.py` and two settings, and **does
nothing until `STRATEGY_MANAGER_FUNCTION_NAME` is set.** That is deliberate: the
code ships and deploys with no behavioural change, so the cutover is one
environment variable rather than a code change at the moment it matters.

The order is: deploy this function → confirm `intraday-market-sentiment` has
run a clean live session → grant that function `lambda:InvokeFunction` on this
function's ARN → set `STRATEGY_MANAGER_FUNCTION_NAME`.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `STRATEGY_REGISTRY` | see above | regime → strategies, as JSON |
| `HISTORY_15MIN_BARS` | 260 | 200 for `sma200` (8 sessions) plus headroom |
| `HISTORY_5MIN_BARS` | 200 | the legacy strategy's own `tail(200)` window |
| `HISTORY_DAILY_BARS` | 40 | Wilder ATR14 needs 15 to start, 40 to settle |
| `MIN_BARS_TO_CLASSIFY` | 200 | below this, classifying is refused |
| `CANDLE_LAG_TOLERANCE_BARS` | 1 | the loader/sentiment race, and nothing more |
| `TREND_SEPARATION` | 0.001 | legacy value, **not measured against this system** |
| `INVOCATION_TYPE` | `Event` | asynchronous dispatch |
| `SNAPSHOT_INTERVAL_MINUTES` | 5 | the interval `snapshot_ts` names |

## Local verification

`boto3` and `pg8000` come from the runtime and the layer and are normally not
installed locally. Stub them to exercise the pure logic — `classify()`, the
indicators and `registry` — the way CLAUDE.md stubs `pg8000` for the loader.
The harness used for this component's verification drives all three off real
rows pulled from Neon rather than a fixture, because a synthetic fixture
already hid one live-data bug in this repo.

## Porting notes

Where this departs from `trading-algo`, and why.

- **`session_vwap` resets at the open.** The legacy
  `helpers/intraday_indicators.py::session_vwap` takes a plain `cumsum` over
  whatever frame it is handed, and `build_intraday_sentiment` hands it a 90-day
  frame — so the value it calls "session VWAP" is a 90-day cumulative VWAP that
  never resets. The same repo's `liquidity_swap_strategy` computes it correctly,
  resetting per `trade_date`. This function computes it over the bars passed in
  and the caller passes one session, which also makes it agree with
  `intraday_market_sentiment.vwap`.
- **A short history raises** instead of defaulting NaN SMAs to `0.0`. See above.
- **No `HOUR_1` classification.** Legacy built intraday sentiment for both
  `MIN_15` and `HOUR_1`; no strategy ever read the hourly one.
- **`atr_14` and the pivot levels are recomputed, not read.**
  `daily_market_sentiment` deliberately does not store them — its schema
  comment calls pivots "a pure function of pd_high/pd_low/pd_close" — so this
  function computes ATR14 from `candle_daily` and the strategies derive pivots
  from the three stored columns. No schema change was needed.
