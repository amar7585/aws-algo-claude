# Components

← [Back to root README](../README.md) · [Architecture](architecture.md) · [Flow charts](flow.md)

## Register

| Component | Kind | Cadence | Status | Docs |
|---|---|---|---|---|
| `instrument-master-loader` | Lambda function | monthly | **built, running** | [README](../instrument-master-loader/README.md) |
| `auth-dhan-broker` | Lambda function | daily, weekdays 08:00 | **built, running** | [README](../auth-dhan-broker/README.md) |
| `daily-market-sentiment` | Lambda function | daily, weekdays 09:50 | **built, running** | [README](../daily-market-sentiment/README.md) |
| `neon-db-driver` | Lambda layer | — | **built** | [README](../layers/neon-db-driver/README.md) |
| `neon-access` | Lambda layer | — | **built** | [README](../layers/neon-access/README.md) |
| `intraday-data-loader` | Lambda function | every 5 min, 10:00–15:35 | **built, running** | [README](../intraday-data-loader/README.md) |
| `error-notifier` | Lambda function | on failure only | **built, running** | [README](../error-notifier/README.md) |
| `intraday-market-sentiment` | Lambda function | every 15 min, 09:35–15:35 | **built, deployed** | [README](../intraday-market-sentiment/README.md) |
| Strategy task | Lambda function | per session | planned | — |

Everything above the divider exists and runs. See
[architecture.md](architecture.md) for why none of these sit inside a state
machine: each runs on its own clock, and a failure in one should raise its own
alarm rather than fail a session.

**There is no Step Functions state machine and no History/Regime split.** An
earlier design recorded both; what got built instead is `daily-market-sentiment`
doing the daily fetch and the daily read in one function, on one schedule, and
`intraday-data-loader` filling the three intraday candle tables on another, and
`intraday-market-sentiment` writing the 15-minute read on a third. The
remaining planned piece is the strategy task.

## Built

### instrument-master-loader

Refreshes `algo.instrument_master` from Dhan's public scrip master.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package |
| Layer | `neon-db-driver` |
| Package contents | `handler.py` alone — everything else is stdlib |
| Dependencies | pg8000 (from the layer). No pandas, no `dhanhq`, no compiled wheels |
| Reads | `https://images.dhan.co/api-data/api-scrip-master.csv` (public, unauthenticated) |
| Writes | `algo.instrument_master` |
| Secrets | `NEON_CONNECTION_STRING` only — no broker credentials needed |

Full configuration, deployment notes and the known `FUTIDXBSE` rule gap are in
its [README](../instrument-master-loader/README.md).

### neon-db-driver layer

Carries [pg8000](https://github.com/tlocke/pg8000), the Postgres driver every
function here uses to reach Neon.

Chosen because it is **pure Python**: no libpq, no compiled extension. The layer
builds with a plain `pip install` on any machine, works unchanged on `x86_64`
and `arm64`, and stays inside Lambda's zip limits. psycopg2 would need a binary
matched to the Lambda runtime, and `psycopg2-binary` is explicitly not
recommended for production.

Build and publish steps are in its [README](../layers/neon-db-driver/README.md).

### auth-dhan-broker

Keeps a live DhanHQ v2 access token in SSM Parameter Store, so no session
function has to authenticate itself and no token is refreshed by hand.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package |
| Layer | none — `boto3` ships with the runtime, the rest is stdlib |
| Package contents | `handler.py` alone |
| Schedule | `cron(0 8 ? * MON-FRI *)`, `Asia/Kolkata` |
| Writes | `/algo/dhan/token` (SSM `SecureString`) |
| Secrets | `DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET` as env vars |

Mints a token from client id + PIN + TOTP and writes it to the parameter. Any
failure raises; there is no fallback path.

**There is deliberately no renew step.** `/v2/RenewToken` would have let one
TOTP login carry a week, but it refuses TOTP-minted tokens outright —
`"Renewal of token not allowed for this token type"`, measured 2026-09-10. The
branch was built, tested live and removed. A token lives 24 hours, so one 08:00
run covers the 09:15–15:30 session with hours to spare.

Full configuration, IAM, the measured API findings and what remains unknown are
in its [README](../auth-dhan-broker/README.md).

### daily-market-sentiment

Fetches daily candles for NIFTY and INDIA VIX into `algo.candle_daily`, computes
the daily read into `algo.daily_market_sentiment`, and pushes it to Telegram.
Three Dhan calls, ~12 s.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 8 modules |
| Layers | `neon-db-driver` + `neon-access` |
| Schedule | `cron(50 9 ? * MON-FRI *)`, `Asia/Kolkata` |
| Secrets | `/algo/dhan/token`, `/algo/telegram/brief`, `/algo/neon/connection` |

It reads the Dhan token from `/algo/dhan/token` rather than holding broker
credentials of its own. It does **not** invoke `auth-dhan-broker` on demand —
that path cannot fire in practice, since the 08:00 refresh precedes the 09:50
run by nearly two hours and a token lives 24 hours.

Its README carries the measured facts that make it work: `fromDate` is
exclusive, the daily endpoint lags a session, `security_id` alone is not unique,
and the sentiment score is **not** predictive of forward return.

### intraday-data-loader

Fills `algo.candle_5min`, `algo.candle_15min` and `algo.candle_1hr` for NIFTY
and the current-month NIFTY future, through the session. It computes nothing.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 5 modules |
| Layers | `neon-db-driver` + `neon-access` |
| Schedule | every 5 min 10:00–15:30 + a 15:35 sweep, `Asia/Kolkata` — 68 invocations a day |
| Secrets | `/algo/dhan/token`, `/algo/neon/connection` — no environment variables at all |

Which intervals a run fetches follows one rule — **fetch interval *I* when
`(run time − 09:15)` is a whole multiple of *I* minutes** — because Dhan's
intraday buckets are session-aligned from 09:15, not clock-aligned. That was
measured, not assumed, and is asserted at runtime.

The current-month future is never hardcoded: the nearest option expiry's month
names the contract, which rolls itself at each expiry. It belongs here rather
than in the daily function because its daily series is a rolled continuous one
that changes meaning at each expiry, while its intraday series is
contract-specific and safe to store per `security_id`.

**Partial candles are stored on purpose** — Dhan returns the in-progress bucket
and the primary-key upsert corrects it on a later pass. A consumer tells the two
apart with `candle_ts + interval_seconds <= now`; only the newest bar per table
is ever partial. Its README carries the reasoning and the measured facts.

### error-notifier

Pushes failures from every other function to Telegram. It computes nothing and
stores nothing.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 3 modules |
| Layers | **none** — stdlib plus the runtime's boto3 |
| Trigger | CloudWatch Logs subscription filters on every other log group |
| Secrets | `/algo/telegram/brief` |

**Why a log subscription rather than a `try/except` in each function.** A
timeout kills the process before any `except` runs, and an import error fires
before the handler module loads — the two failures most likely to go unnoticed.
Both still reach the log. It also touches none of the existing functions.

**Why not a CloudWatch alarm.** An alarm can only say `Errors >= 1`; the log
event carries the exception, so the message names the instrument, the interval
and the HTTP status.

Its own log group must never be subscribed to it — that is a billing loop. The
handler refuses payloads from its own log group so the mistake is inert rather
than expensive.

Deliberately no `neon-access` layer: that package imports pg8000, and this
function never touches the database.

### intraday-market-sentiment

Writes one row describing the market every fifteen minutes — futures basis and
open-interest buildup, INDIA VIX, and the option-chain read (straddle, PCR, OI
walls, max pain, IV skew) for the nearest **and** monthly expiries at once —
plus the ten raw option legs behind it.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 8 modules |
| Layers | `neon-db-driver` + `neon-access` |
| Schedule | 09:35–15:35 every 15 min, `Asia/Kolkata` — three rules, 25 invocations a day |
| Writes | `algo.intraday_market_sentiment`, `algo.option_chain_snapshot` |
| Secrets | `/algo/dhan/token`, `/algo/neon/connection` — no environment variables required |

**It shares no tables with `intraday-data-loader`.** Everything comes from the
Dhan API — its own candles, its own chains — so a stalled loader cannot feed it
stale inputs. The single exception is **its own previous row**, read back to
provide the baseline for every `*_change_pct`: Dhan serves only a live option
chain and has no historical-chain endpoint, so an intraday OI delta cannot be
had any other way.

**Runs fire five minutes past each 15-minute boundary** — 09:35, 09:50, 10:05 —
so a 5-minute bucket has just closed and the bar the snapshot describes is
final. Nothing it writes is ever partial, which is the opposite of the loader's
deliberate choice: a snapshot row is never revisited, so a partial bar here
would be wrong forever. `snapshot_ts` is that closed bar, not the run clock,
which also makes a re-run idempotent.

Two strike widths, not interchangeable: aggregates over ATM ±20, raw legs
stored for ATM ±2 (10 rows a snapshot). Both expiries sit on one row as
`near_*` / `mth_*` column pairs, and the monthly is the first monthly
*strictly after* the nearest so the two can never name the same contract.

Its README carries the measured facts: the chain is at the flat
`/v2/optionchain` while `expirylist` is nested, futures open interest returns
as `open_interest` rather than `oi`, and IV and the greeks arrive as `0` when
Dhan did not compute them — stored as `NULL`, because `0` poisons any skew.

## Planned

### Strategy task

Evaluates the playbooks valid for the classified regime and emits signals. It
never runs unconditionally: a playbook fired in the wrong regime is the main
way this loses money, so the regime gate comes first.

## Shared conventions

Any new component in this repo is expected to hold to these. They are covered
in more depth in [architecture.md](architecture.md#invariants).

- **Epoch seconds** for every stored time value.
- **Raw Dhan segment codes** in `exchange_segment`.
- **`(security_id, instrument_type)`** as the instrument identity.
- **Prefer pure Python** — a compiled dependency needs a stated reason and a
  measured package size, not a default yes.
- **Fail loudly.** Raise on error so Lambda records a failure; never return a
  500-shaped success. Never let a schema change degrade into a silent 0-row
  write.
- **Secrets from environment variables**, sourced from Secrets Manager or
  encrypted SSM at deploy time. Never committed.

## Source lineage

This is a rewrite, not a greenfield design. Two repos outside this one hold
context that is not derivable from the code here.

| Repo | What it holds |
|---|---|
| `trading-algo` | The legacy production system being ported. Source of truth for `INSTRUMENT_RULES` (`brokers/implementations/dhan/dhan_broker.py`), `ExchangeSegment` (`bin/enums/dhan.py`), and the monthly reload rule (`data/database_service.py`). |
| `AIML` | Architecture notes and the component register the naming here follows, plus a separate Postgres port of the legacy schema that targets a **different** Neon project and is not compatible with this one. |

Agreed scope for the loader is to replicate **all 9** `INSTRUMENT_RULES`, not to
narrow them to Nifty-only — even though most are irrelevant to index trading.
