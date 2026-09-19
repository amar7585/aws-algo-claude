# strategy-manager

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md)

Decides which playbooks are valid for the market as it stands, and invokes
them. It evaluates no playbook itself and emits no signal.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 3 modules |
| Layers | `neon-db-driver` + `neon-access` — only `neon-access` is used, for the IST/epoch helpers; it opens no connection |
| Schedule | **none** — invoked by `intraday-market-sentiment`, 24×/day |
| Reads | **nothing** |
| Writes | **nothing** |
| Secrets | none |

## It is a pure router

No database connection, no API call, no indicator, no row written. Everything
it decides on arrives in the payload. That is a deliberate narrowing — the
previous version classified the 15-minute frame itself, read three candle
tables and called Dhan for a live price. Two things moved out:

**The classification moved *up*.** It lives in the
[`market-classifier` layer](../layers/market-classifier/README.md) and is run
by `intraday-market-sentiment`, which **stores** the result as columns on
`algo.intraday_market_sentiment`. Before, the rules lived in two places — a
daily port here-ish and an intraday port there — and the regime a strategy
acted on was never persisted at all; it existed only in a log line.

**The data fetch moved *down*.** Each playbook reads the bars it needs. This
function was fetching a fixed window on their behalf and guessing at the size.

Its one Dhan call went with them. The only thing it used Dhan for was a live
price, and the only playbook built never read it — a sweep is confirmed by a
**closed** bar reclaiming a level, so an unconfirmed live tick is exactly what
that setup must not act on.

## The chain

```
EventBridge (every 15 min)
  → intraday-data-loader        commits candles
  → intraday-market-sentiment   writes the snapshot + its classification,
                                reads the daily row, invokes with both
  → strategy-manager            routes on regime|bias
  → the playbooks               fetch their own bars, gate again, report
```

**Why invoked and not scheduled.** Its input *is* the snapshot, and the
snapshot exists only once the sentiment function has written it. A cron here
would have to guess how long that takes, read the row back out of Postgres,
and decide what to do when it is not there yet — three problems that disappear
when the completion of the write is itself the trigger.

**Every invoke is `Event`.** This function returning is not a claim that any
playbook succeeded. Each has its own log group and `error-notifier` reports
from all of them.

## Routing is on a combination

`regime` alone is not enough. "sideways" says the swing read found no
progression; it does not say whether the market is leaning up, leaning down, or
genuinely balanced — and a playbook that is right on a balanced day can be
wrong on a sideways day with a bearish lean. The key is `"<regime>|<bias>"`.

| | `bullish` | `bearish` | `range-bound` |
|---|---|---|---|
| **`trending`** | — | — | — |
| **`sideways`** | — | — | `strategy-range-liquidity-sweep` |
| **`volatile-expansion`** | — | — | — |

**Eight of the nine cells are deliberately empty, and the ninth is a
placeholder.** Which combinations should run which playbook is a decision to
take against observed sessions, and there are none yet — see the layer README,
"Revisit once sessions have accumulated".

**A present key mapping to `[]` and a missing key are different things.** The
first is "no playbook is valid on this kind of day". The second means the
classifier and this registry have drifted apart — a new regime or bias value
appeared and nobody told the router — and it **raises**, because routing
nothing would otherwise look exactly like a correct stand-down.

**Both gates are load-bearing.** This registry answers "is this playbook valid
for this kind of day at all". The playbook's own gate answers what only it can
know — VIX behaviour, how much of the day's range is spent, opening-range
width, time of day. A strategy that trusted an upstream gate it cannot see
would fire on a bad day the moment that gate moved.

## The payload contract — v2

`CONTEXT_VERSION` is an interface. Every playbook asserts the version it was
written against and raises on anything else, so a change is loud on both sides.

```json
{
  "context_version": 2,
  "dispatched_at": 1789104600,
  "instrument": { "security_id": "13", "instrument_type": "INDEX", ... },
  "snapshot":   { "snapshot_ts": ..., "regime": ..., "bias": ..., "sma9": ... },
  "daily":      { "trade_date": ..., "regime": ..., "stale": false, ... }
}
```

**What changed from v1:**

- `classification` is gone as a separate block — it is **on** the snapshot now
  (`bias`, `structure`, `regime`, `volatility`, `score`, `max_score`,
  `confidence`, `sma9/50/100/200`, `rsi`, `swing_*`).
- `candles`, `live` and `daily_atr14` are gone. Playbooks fetch their own.
- `sma20` no longer exists anywhere; the frame carries `sma9`.
- `daily` may be `None`, and carries `stale` when this morning's 09:35 run did
  not land.

**Measured size: 842 bytes** for a real snapshot plus a real daily row —
against v1's ~8 KB, which was dominated by 200 bars of OHLCV. The 256 KB
asynchronous cap is still checked before every invoke, because exceeding it
raises a boto3 error that names bytes and not the reason.

## `daily` can be absent, and that is not this function's problem

`daily-market-sentiment` runs at 09:35 and can fail. A playbook that needs the
previous day's levels should say so on its own gate rather than have the router
refuse to route. What this function does is make the state visible: a missing
daily row and a **stale** one are both logged here as well as by the reader.

**The stale check exists because of a real bug.** This function used to look
the daily row up itself with `trade_date = ist_midnight_epoch(today)`. A daily
row is stamped with the session it *describes* — yesterday, because Dhan's
daily endpoint lags — so a row stamped today never exists and that lookup
**always returned `None`**. Every playbook gating on the daily read was gating
on nothing. The lookup now lives in `intraday-market-sentiment`, keyed as
"newest row stamped before today", with `created_at` deciding staleness.

## IAM

Its execution role needs `lambda:InvokeFunction` on **each playbook's ARN**,
not a wildcard — and its own log group, not a borrowed one. Both
console-generated roles in this account are scoped to a single ARN and fail
**silently** when borrowed: a borrowed execution role produces no logs at all,
and a borrowed scheduler role creates a schedule that shows `ENABLED` and never
fires. See CLAUDE.md.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `STRATEGY_REGISTRY` | the 9-cell map above | filling a cell is configuration, not a redeploy |
| `INVOCATION_TYPE` | `Event` | a slow playbook must not sit inside this timeout |
| `MAX_PAYLOAD_BYTES` | 262144 | Lambda's asynchronous cap |
