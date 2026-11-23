# risk-manager

← [Back to root README](../README.md) · [order-manager](../order-manager/README.md) · [strategy-manager](../strategy-manager/README.md)

THE ONE RISK MANAGER, in front of `strategy-manager` (Amar 2026-11-22). **P2b — LIVE since 2026-11-23 17:22:31 IST**
(pattern-recorder repointed; function created 15:34:37 IST, CodeSha256 `KsSFZbsj5RQEYIJZnvkq6Ffaxde8dfcEopPPBZgoTIo=`). Deployed 2026-11-23 after the close, Amar: "deploy now" - P2a's two-session live check skipped on his call (no sweep trade had run through P2a yet).
First live session: **Wed 2026-11-25** (11-24 is a holiday). Plan: `p2b-build-plan-2026-11-23.md` in the notes.

**P3 — the O1 limits IN FORCE since 2026-11-23 18:42:03 IST** (Amar: "Approve P3, insert O1 now"). One `risk_control`
row, `set_at` 1795439523: kill switch off, max open 4, max 8 trades per day, daily loss 500 point-lots. No code change,
no deploy. First read live: Wed 2026-11-25. Plan: `p3-limits-plan-2026-11-23.md` in the notes.

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
- **Limits** (NULL = off; **in force since P3: O1 = max open 4 / 8 trades per day / daily loss 500**, Amar 2026-11-22):
  - **max open** counts OPEN positions only: not WATCHING (the sweep after its EXIT, balance 0), REFUSED or CLOSED;
  - **max trades per day** counts every non-REFUSED position of the session; a sweep re-entry reuses its row, so it is
    the same trade;
  - **daily loss** is in point-lots: realised + marked at the newest index close, each position divided by its own
    `lot_size` (500 point-lots = 32,500 point-units at the lot of 65).
  - **A limit that binds** puts every playbook in `exit_only` (`why: max open positions 4/4` / `max trades per day
    8/8`): a would-be entry is still sent, headed REJECTED, and stored REFUSED; a sweep re-entry ends the watch.
  - **A loss breach** = `force_exit("DAILY LOSS EXIT")`, and the day stays off (while realised <= -limit, or once any
    position carries that exit reason). From the 15:10 candle the time exit wins.
- **Run block:** tick playbooks (sweep) get a mode on every tick; alert playbooks (trend) on their alert, and on a
  tick only while they hold a position **entered on or before the tick's newest candle**, never `entry_exit`.

## Kill switch / limits — Amar's INSERTs

Neon project `nameless-mountain-15353651` (AI Trader APP), database `Algo`: **confirm the project id first.**

**The newest row with `set_at` <= now is the WHOLE state**: the kill switch and all three limits together.
- **A kill row written with NULL limits switches the limits off.** They stay off the next morning too, after the kill
  switch has reset, and nothing raises.
- **A limits row written with `kill_switch` false turns off a kill switch set earlier in the same session.**

So the kill statements copy the limits from the row in force. `kill_switch` is always written as a literal, never
copied: a copied old `true`, written now, would arm the switch again for this session. Two rows in the same second
collide on the primary key and fail.

```sql
-- the row in force (read it first)
SELECT set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, set_by, reason
FROM algo.risk_control WHERE set_at <= extract(epoch FROM now())::bigint ORDER BY set_at DESC LIMIT 1;

-- kill switch ON (this session; the next one if set outside session hours) - keeps the limits in force
INSERT INTO algo.risk_control (set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, set_by, reason)
SELECT extract(epoch FROM now())::bigint, true, max_open, max_trades_per_day, daily_loss_limit, 'amar', 'kill switch'
FROM algo.risk_control WHERE set_at <= extract(epoch FROM now())::bigint ORDER BY set_at DESC LIMIT 1;

-- kill switch OFF again the same day - keeps the limits in force
INSERT INTO algo.risk_control (set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, set_by, reason)
SELECT extract(epoch FROM now())::bigint, false, max_open, max_trades_per_day, daily_loss_limit, 'amar', 'kill switch off'
FROM algo.risk_control WHERE set_at <= extract(epoch FROM now())::bigint ORDER BY set_at DESC LIMIT 1;

-- change the limits: write all three (NULL = that limit off). Kill switch false - true instead if it is on this session
INSERT INTO algo.risk_control (set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, set_by, reason)
VALUES (extract(epoch FROM now())::bigint, false, 4, 8, 500, 'amar', '<why>');
```

- **The kill switch needs no "off" row the next day:** it resets each morning by itself.
- **Rolling back P3 (limits off):** the last statement with `NULL, NULL, NULL`, `set_by` 'P3 rollback'. It applies
  from the next run.
- Nothing is ever updated or deleted.

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
| **P3: O1** | 18:42:03 | **`risk_control` row `set_at` 1795439523**: kill switch off, 4 / 8 / 500, `set_by` 'P3 deploy'. Before it: the seed in force, 0 open positions, after the close. After it: read back as the row in force (2 rows); the live row through `db.read_control` + `rules.assess` offline 14/14 (`prelim/p0/p3_o1_check.py`); the O1 chain re-run on the deployed code 4,901.89 / 4 REFUSED. Rollback: the limits INSERT above with NULLs |

**Smoke (15:39, today's final tick, the 15:25 candle, built from the stored rows; the sweep's `TELEGRAM_ENABLED=off` for the smoke, restored 15:39:55):** risk-manager 2.46 s -> `force_exit (TIME EXIT)` for the sweep only, no positions; strategy-manager v4 routed it (0.90 s); the sweep re-derived the day: 0 entered trades, 0 stored, **0 signals sent**, the same 3 register rows (only `updated_at` moved); trend not invoked; no error. Between 15:36 and 17:22 nothing but the smoke invoked the chain.

**Rollback:** pattern-recorder back to `STRATEGY_MANAGER_FUNCTION_NAME=strategy-manager` with `invoke-strategy-manager` restored (document in `pattern-recorder/iam/`), and the published versions above for strategy-manager, the sweep and trend. risk-manager and `risk_control` can stay (nothing calls them then).

## Deploy order (as planned) — own role pair, never borrowed

Migration `migrations/2026-11-23-risk-control.sql` (+ seed) → order-manager (256 MB) → this function (role from
[iam/](iam/), log subscription to error-notifier) → trend + sweep v4 → strategy-manager v4 → pattern-recorder
`STRATEGY_MANAGER_FUNCTION_NAME=risk-manager` + its invoke policy → smoke. Each with a rollback version.
