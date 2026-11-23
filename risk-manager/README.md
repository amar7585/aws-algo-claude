# risk-manager

← [Back to root README](../README.md) · [order-manager](../order-manager/README.md) · [strategy-manager](../strategy-manager/README.md)

THE ONE RISK MANAGER, in front of `strategy-manager` (Amar 2026-11-22). **P2b — LIVE since 2026-11-23 17:22:31 IST**
(pattern-recorder repointed; function created 15:34:37 IST, CodeSha256 `KsSFZbsj5RQEYIJZnvkq6Ffaxde8dfcEopPPBZgoTIo=`). Deployed 2026-11-23 after the close, Amar: "deploy now" - P2a's two-session live check skipped on his call (no sweep trade had run through P2a yet).
First live session: **Wed 2026-11-25** (11-24 is a holiday). Plan: `p2b-build-plan-2026-11-23.md` in the notes.

```
pattern-recorder --(Event: each alert once, then the tick)--> risk-manager --(Event, + run)--> strategy-manager --> playbooks
```

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip, 5 modules (`handler`, `rules`, `db`, `ordermgr`, `config`); layers `neon-db-driver` + `neon-access` |
| Reads | `algo.risk_control` (row in force), `algo.position_master` (today, all playbooks), `algo.trading_holiday` — read-only |
| Writes | nothing. Kill-switch closes go through `order-manager` |
| Invokes | `strategy-manager` (Event), `order-manager` (sync, kill-switch closes only) |
| Secrets | `/algo/neon/connection` |

## The rules ([rules.py](rules.py), pure)

- **Kill switch** (DB row, append-only): on for the session it is set in, else the next (set after the previous
  session's 15:30 close). On: every OPEN / WATCHING position closed (`KILL SWITCH EXIT`, one message each, at the
  newest close) and **strategy-manager is not invoked**. Resets each morning with no row.
- **15:00:** no new trade on a candle closing at or after 15:00 → `exit_only`.
- **Time exit:** from the 15:10 candle (the 15:16 tick) every holder gets `force_exit("TIME EXIT")` and closes at
  that candle's close after its own steps.
- **Limits** (NULL = off; O1 = 4 / 8 / 500 is P3): max open, max trades per day (REFUSED not counted), daily loss in
  point-lots (realised + marked). A loss breach = `force_exit("DAILY LOSS EXIT")`, and the day stays off.
- **Run block:** tick playbooks (sweep) get a mode on every tick; alert playbooks (trend) on their alert, and on a
  tick only while they hold a position **entered on or before the tick's newest candle**, never `entry_exit`.

## Kill switch / limits — Amar's INSERTs

```sql
INSERT INTO algo.risk_control (set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, set_by, reason)
VALUES (extract(epoch FROM now())::bigint, true, NULL, NULL, NULL, 'amar', 'kill switch');
```
Carry the limits in force into a kill row (the newest row is the whole state).

## Verified offline 2026-11-23

Whole chain on 87 sessions: limits off = P0 exactly (161 positions, 5,248.11 lot-pts); O1 = 4,901.89 with 4
REFUSED + 4 REJECTED messages; kill on 11-20 11:02 closes the open position, nothing runs after, other days
unchanged; dropped / duplicated / late ticks and injected failures end in the same record, no message twice. Unit
tests + 29/29 mutations caught (`prelim/p0/p2b_*.py` in the notes).

## Deployed 2026-11-23 (local `aws` CLI, ap-south-1)

| step | IST | what |
|---|---|---|
| migration | 15:32:05 | `algo.risk_control` created + seed (kill switch off, limits NULL, `set_by` 'P2b deploy') |
| order-manager | 15:32:50 | `EUUQoC7Y…`, 256 MB; rollback version 1 = `TdBvzcXF…` |
| risk-manager | 15:34:37 | created: role `risk-manager-role` (trust + `lambda-logs`, `algo-ssm-read`, `invoke-strategy-manager-order-manager`; simulated: own log group, `/algo/neon/connection` and the two functions allowed, the rest denied), 30 s, 256 MB, `neon-db-driver:1` + `neon-access:1`, no environment; log group subscribed to error-notifier (`logs-risk-manager`) |
| strategy-trend-pullback | 15:35:52 | `dlZK1gOx…`, + `ORDER_MANAGER_FUNCTION=order-manager` (`TELEGRAM_ENABLED=on` kept), + `invoke-order-manager`; rollback version 1 = `DVj+KKye…` |
| strategy-manager | 15:36:11 | `jqV8KvNo…`; rollback version 1 = `r5D73Twk…` |
| strategy-range-liquidity-sweep | ~15:36 | `dgeSzzph…`; rollback version 12 = `6+z2tBAr…` (P2a) |
| pattern-recorder | 17:22:31 | `STRATEGY_MANAGER_FUNCTION_NAME=risk-manager`; `invoke-risk-manager` added, `invoke-strategy-manager` removed (simulated: risk-manager allowed, strategy-manager denied); rollback version 1 = `pUdG7CGU…` + the old variable |

**Smoke (15:39, today's final tick, the 15:25 candle, built from the stored rows; the sweep's `TELEGRAM_ENABLED=off` for the smoke, restored 15:39:55):** risk-manager 2.46 s -> `force_exit (TIME EXIT)` for the sweep only, no positions; strategy-manager v4 routed it (0.90 s); the sweep re-derived the day: 0 entered trades, 0 stored, **0 signals sent**, the same 3 register rows (only `updated_at` moved); trend not invoked; no error. Between 15:36 and 17:22 nothing but the smoke invoked the chain.

**Rollback:** pattern-recorder back to `STRATEGY_MANAGER_FUNCTION_NAME=strategy-manager` with `invoke-strategy-manager` restored (document in `pattern-recorder/iam/`), and the published versions above for strategy-manager, the sweep and trend. risk-manager and `risk_control` can stay (nothing calls them then).

## Deploy order (as planned) — own role pair, never borrowed

Migration `migrations/2026-11-23-risk-control.sql` (+ seed) → order-manager (256 MB) → this function (role from
[iam/](iam/), log subscription to error-notifier) → trend + sweep v4 → strategy-manager v4 → pattern-recorder
`STRATEGY_MANAGER_FUNCTION_NAME=risk-manager` + its invoke policy → smoke. Each with a rollback version.
