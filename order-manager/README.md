# order-manager

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Components](../docs/components.md) · [Future implementation](../docs/future-implementation.md)

**The one common order manager.** The playbooks decide; every step they take (an entry, T1, an exit, a re-entry, a stop move, a time exit) is handed here, **one signal per synchronous call**, and this function carries it out.

**Paper mode (now):**
- no order is placed and **no order is saved**;
- the step updates the one position row in `algo.position_master`;
- the caller's message goes to Telegram.

**The Dhan phase (P5, its own approval):** the same entry point will place the Dhan order, and only then is an order record kept.

This follows `trading-algo` (`managers/execution_router.py`):
- the strategy returns a signal;
- in PAPER mode, `PaperTradeManager.save_paper_trade` saves the position;
- in LIVE mode, `OrderManager` saves the broker order.

Amar, 2026-11-22: "common order manager like what we have in algo_trading app just dont save order directly save the position in position master"; "Its own Lambda".

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 8 modules since 2026-11-28 (+ `option`, `dhan`, `params` for the option paper; 5 before) |
| Layers | `neon-db-driver` + `neon-access` |
| Trigger | **synchronous invoke** by a playbook or the risk manager. P1 (2026-11-22 21:16 IST): called by nothing. The sweep calls it since P2a (2026-11-22 22:16 IST); the trend playbook and `risk-manager` since P2b (2026-11-23) |
| Reads | `algo.position_master` (its own row, locked), `algo.instrument_master` (the lot size), `algo.option_paper_position` (its own row, locked); with `OPTION_PAPER` on, Dhan's option chain at an entry and a quote at each sale (data reads only) |
| Writes | `algo.position_master` and, since 2026-11-28, `algo.option_paper_position` — **its only writer** ([schema](schema.sql), migrations [2026-11-22](../migrations/2026-11-22-position-master.sql), [2026-11-28](../migrations/2026-11-28-option-paper.sql)) |
| Secrets | `/algo/neon/connection`, `/algo/telegram/brief`, `/algo/dhan/token` (since 2026-11-28) (SSM) |
| Status | **running.** P1 deployed 2026-11-22 21:16:44 IST; P2b `EUUQoC7Y…` 2026-11-23 15:32; **option paper `0b6JDpBs…` since 2026-11-28 18:08 IST** (`OPTION_PAPER=on` from Mon 2026-11-30 00:00 IST; rollback version 2 = `EUUQoC7Y…`). Committed + pushed (eb8827b) |

## The contract — `contract_version` 1

```json
{"contract_version": 1,
 "dry_run": false,
 "signal": {
   "kind": "ENTRY",
   "playbook": "strategy-range-liquidity-sweep",
   "signal_key": "15min high|high|<alert_ts>",
   "instrument": {"security_id": "13", "instrument_type": "INDEX", "exchange_segment": "IDX_I", "trading_symbol": "NIFTY"},
   "underlying": null,
   "session_start": "<IST midnight epoch>", "entry_ts": "<signal candle START>", "candle_ts": "<this step's candle START>",
   "direction": "short", "price": "<fill>", "lots": 3,
   "stop_loss": "<stop>", "t1_target": "<T1>", "target": "<T2>", "risk_pts": "<entry - stop>",
   "watch": false, "refused_reason": null,
   "message": "<the playbook's own text, word for word; null = record only>"}}
```

**Kinds:** `ENTRY`, `REJECTED`, `T1`, `EXIT` (with `watch: true` for the sweep's exit-and-watch), `WATCH ENDED`, `RE-ENTRY`, `STOP`, `TARGET`, `TRAIL`, `STOP MOVE`, `TIME EXIT`, `DAILY LOSS EXIT`, `KILL SWITCH EXIT`.

**The answer:**

```json
{"result": "applied | duplicate | rejected_stored | ignored", "kind": "T1", "why": "...", "position_id": 41, "status": "OPEN",
 "balance_units": 65, "lots_open": 1.0, "lot_size": 65, "realized_profit": 1300.0, "sent": true, "dry_run": false}
```

- **A wrong `contract_version`, a missing field, a non-raw segment code or an inconsistent step RAISES.** The caller's synchronous invoke sees `FunctionError` and raises too. error-notifier reports both, and the caller's async retry resends the signal, which is safe (below).
- **Never a `{"statusCode": 500}` shape.**

## The rule: a step is applied only if it moves the position forward ([positions.py](positions.py))

| Kind | Applies when | Change |
|---|---|---|
| `ENTRY` | no row on the unique key | insert `OPEN`: `lots` × `lot_size` units at `price` |
| `REJECTED` | no row on the unique key | insert `REFUSED`: no quantity, the reason |
| `T1` | `OPEN`, no T1 yet, fewer units than the balance | close `lots` × `lot_size` at `price` |
| `EXIT` with `watch` | `OPEN` | close the balance → `WATCHING` |
| `EXIT` / `STOP` / `TARGET` / `TRAIL` / `TIME EXIT` / `DAILY LOSS EXIT` / `KILL SWITCH EXIT` | `OPEN` | close the balance → `CLOSED` |
| | `WATCHING` (not `EXIT`) | the watch ends → `CLOSED`, nothing to close |
| `WATCH ENDED` | `WATCHING` | → `CLOSED` |
| `RE-ENTRY` | `WATCHING`, no re-entry yet | `lots` × `lot_size` units at `price` → `OPEN` |
| `STOP MOVE` | `OPEN`, the stop moves the trade's way | `tsl_stop_loss` |

The other answers:
- **`duplicate`** (nothing changes, nothing is sent):
  - the same step again on the same candle;
  - an opening signal for a position that already exists (a retry, or a second alert on the same candle — 10 of those in the 2026-11-22 limits replay);
  - a step older than the last one applied (a late run).
- **`ignored`:** any step for a `REFUSED` position. Its later steps are never sent.
- **Anything else raises.** The caller's derivation and the record disagree.

**Quantities are units (Amar 2026-11-22, "Units now"):** lots × lot size, as Dhan's position API reports them.
- The paper position is on the NIFTY index price, whose own `lot_units` is 1.
- `lot_size` is therefore the near-month NIFTY future's `lot_units`, chosen by its symbol: `NIFTY-<MON><YEAR>-FUT` for the session's month, else the next month's. If neither is found, the call raises.
- It is stored on the position at entry. The value was 65 on NOV / DEC / JAN when read on 2026-11-22.
- `realized_profit` is index points × units and is exact: prices are carried as `Decimal`, and `open_avg` is the price of the units still open.

## One transaction per call ([handler.py](handler.py))

1. **Lock** the row: `SELECT … FOR UPDATE` on the unique key. For an opening signal with no row, also read the lot size.
2. **Decide** in pure code (`positions.py`).
3. **Write** with `INSERT … ON CONFLICT DO NOTHING RETURNING` or `UPDATE`.
   - If a concurrent run inserted the same position in between, the call rolls back and decides again on the stored row, which is now a `duplicate`.
4. **Send** the message, if the step was applied or a rejection stored, and the caller sent one.
5. **Mark** `message_sent_at`, then **commit**.

- **A refused Telegram send** raises and rolls back, so the retry applies and sends it again.
- **The only duplicate message left** is a send that succeeded followed by a commit that failed.
- **`dry_run: true`** runs everything, statements included, then **rolls back and sends nothing**. It is the deploy smoke.
- **`TELEGRAM_ENABLED=off`** logs `WOULD SEND` instead of sending.

## Verified offline, 2026-11-22 (no database, no network)

The test harness is in the side-session notes folder, `prelim/p0/test_order_manager.py`.

- **Against the 2026-11-22 limits replay:**
  - every one of its positions was turned into signals and fed through the state machine: 180 positions and 414 steps (trend 156 + sweep 5 as stored + 19 gate off);
  - every step was applied once, and every immediate repeat was a `duplicate`;
  - the whole sequence sent again changed nothing;
  - balance and status agreed after every step;
  - the realised total equals the replay exactly: **5,791.52 lot-points × 65**;
  - all 10 same-candle trend duplicates were merged.
- **`REJECTED`** is stored as `REFUSED`; an `ENTRY` after it is a `duplicate`, and a step for it is `ignored`.
- **Contradictions raise:**
  - a second T1;
  - a T1 that would close the whole balance;
  - a re-entry while open;
  - a stop moved against a long or a short;
  - a step with no ENTRY;
  - WATCH ENDED while open;
  - a second close of a different kind;
  - `watch` on a non-EXIT;
  - a bad `session_start`;
  - an ENTRY off its candle;
  - zero lots;
  - a non-raw segment code;
  - an unknown kind.
- **A short's realised profit and cumulative averages** are checked by hand.
- **The handler against a fake connection:**
  - a new ENTRY runs lock → lot size → insert → send → mark → commit;
  - a dry run runs lock → lot size → insert → rollback (no send);
  - a T1 runs lock → update → send → mark → commit;
  - a duplicate runs lock → commit (no send);
  - a refused send runs lock → update → send → rollback;
  - the insert race runs lock → lot → insert → rollback → lock → commit (duplicate);
  - record-only and Telegram off: no send;
  - a step for REFUSED is `ignored`;
  - REJECTED is stored and sent;
  - a bad `contract_version` or `dry_run` raises;
  - the lot-size symbols roll the year (DEC → JAN).
- **Seven source mutations, each caught:**
  - a wrong T1 quantity;
  - no late-run guard;
  - a re-entry allowed twice;
  - the P&L sign;
  - a duplicate that sends (both guards removed);
  - commit before the send;
  - a T1 repeat that is not recognised.
- **A bug found and fixed by these tests:** an ENTRY did not set `refused_reason`, so its insert would have raised.

## Verified on Neon, 2026-11-22 (temporary tables only, nothing kept)

Amar: "Run it". Project `nameless-mountain-15353651` / `Algo`, confirmed first. The test used `db.py`'s own statements, rendered by `prelim/p0/neon_temp_sql.py` in the notes folder, against `CREATE TEMP TABLE … ON COMMIT DROP` built from [schema.sql](schema.sql).

- **Postgres does not allow a foreign key from a temporary table to a permanent one.**
  - For the test, the foreign key pointed at a temporary copy of the NIFTY key, read from `algo.instrument_master`.
  - The real foreign key targets that table's primary key, the same one `playbook_alerts` already references live.
- **The success path, in one transaction:**
  - the lock on an empty table returned 0 rows;
  - the lot size read `NIFTY-NOV2026-FUT` and `NIFTY-DEC2026-FUT`, both 65, from the real `instrument_master`;
  - the ENTRY inserted position 1, and the same insert again returned no row (the conflict);
  - the lock then returned the row;
  - the T1, mark-sent and TARGET updates applied;
  - the REJECTED insert was stored;
  - the final row matched exactly:
    - position 1 `CLOSED`, a short at 25189.10;
    - 195 / 195 units, `buy_avg` 25125.85;
    - realised **12,333.75**: 130 units × 39.10 + 65 units × 111.55;
    - the REFUSED row has 0 / 0 and its reason.
  - A conflicting insert still uses up an identity value, so ids can have gaps. That is harmless.
- **Five inserts, each refused by its own constraint:**
  - `position_master_balance_status` (OPEN with a zero balance);
  - `position_master_refused_reason` (REFUSED without a reason);
  - the instrument foreign key (`99999999`);
  - `position_master_trade_mode_check` (`LIVE`);
  - `position_master_lot_size_check` (0).
- **Afterwards:** `algo.position_master` does not exist yet, and no temporary table is left.

## Deployed, 2026-11-22 (Amar: "Deploy P1")

Local `aws` CLI. The account is 709458364771 and the region ap-south-1, passed explicitly every time because the CLI's default is us-east-1. Git Bash needs `MSYS_NO_PATHCONV=1`, or it rewrites `/aws/lambda/...` into a Windows path.

1. **Migration** `2026-11-22-position-master.sql` applied shortly before 21:16 IST, after the target checks.
   - Result: owner `neondb_owner`, 39 columns, 0 rows, every constraint, both indexes.
2. **Role** `order-manager-role`: the trust policy plus the two inline policies `lambda-logs` and `algo-ssm-read`, with no managed policies.
   - The policy **documents** were read back and match `iam/`.
3. **Log group** `/aws/lambda/order-manager` created first.
   - **Function** `order-manager` created 21:16:44 IST: python3.14 x86_64, `handler.lambda_handler`, `neon-db-driver:1` + `neon-access:1`, timeout 30 s, 128 MB, no environment variables.
   - **CodeSha256 `TdBvzcXF77AaWfGg5+UdJXVmnpApqDkYQ5xgKAke/w0=` == the tested zip.** State Active.
4. **error-notifier:**
   - permission `logs-order-manager`, scoped to this log group's ARN;
   - subscription filter `error-notifier` with the same pattern as the sweep's: `?ERROR ?Traceback ?"Task timed out" ?"Unable to import"`.
5. **Dry-run smoke**, two synchronous invokes at 21:17 IST, with a synthetic `smoke-test` ENTRY for 2026-11-13 11:00, short 25189.10, 3 lots.
   - Both answered `applied`: 195 units, `lot_size` 65 from `NIFTY-NOV2026-FUT`, `sent: false`, `dry_run: true`, StatusCode 200, no FunctionError.
   - The table still has **0 rows**.
   - The rolled-back inserts consumed identity values 1 and 2, so the first real position will be id 3.

**Measured (the plan's estimates, corrected):**

| | cold (first) | warm (second) |
|---|---|---|
| Duration | 3,923 ms (+ 435 ms init) | **1,552 ms** |
| Max memory | 99 MB of 128 | 100 MB of 128 |

- **Each call opens a fresh Neon connection**, a TLS handshake from Mumbai to Singapore, and that is most of the ~1.5 s.
  - The plan's "about 50–100 ms per step" was wrong. **A step's message will reach the phone about 1.5 s later** than when the playbook sent it itself.
  - Keeping the connection open across warm invocations would cut that. It is not done, because it is a design change for its own decision.
- **Memory headroom** is about 28 MB: boto3 and pg8000 load on every call. 256 MB (as the sweep uses) would give room and a faster CPU for the handshake. Not changed: that is its own decision.

**Rollback:**
- `aws lambda delete-function --function-name order-manager --region ap-south-1` (nothing calls it);
- the error-notifier permission `logs-order-manager` and the log group, which can be removed the same way;
- `DROP TABLE algo.position_master;` (empty).

## Since P2b (2026-11-23): one connection across warm calls, 256 MB — DEPLOYED 2026-11-23 15:32:50 IST

**Approved by Amar 2026-11-23** with P2b ("OM connection reuse + 256 MB in P2b"); deployed with P2b after the close, CodeSha256 `EUUQoC7YO3lQfzJ6E88jscIl5nViQe957O1poACXhDs=`, 256 MB; rollback: version 1 = `TdBvzcXF…` (P1). From P2b this function **sends the trade messages** of both playbooks and
the risk manager's kill-switch closes, so the ~1.5 s it took per call reached the phone.

- **The connection is kept in the execution environment** ([handler.py](handler.py) `connection()`). Each call
  first checks it with `SELECT 1` (and ends that read with a rollback); if Neon dropped it (autosuspend, a network
  blip) it is closed and replaced. **A call that fails drops the connection**, so the next one starts clean. The
  log line says `reused connection` or `new connection`.
- **Memory 128 → 256 MB** (a deploy-time setting): 28 MB of headroom at P1, and a faster CPU for the cold
  handshake.
- **Callers:** the sweep (P2a), the trend playbook and `risk-manager` (P2b) — each with `lambda:InvokeFunction`
  on this ARN in its own role.
- **Measured after deploy (two dry-run smokes, 15:33 IST):** first call 2.88 s (new connection, + 0.47 s init), second **1.33 s (reused connection)**, 100 of 256 MB. Reuse saved only ~0.2 s against P1's 1.55 s: the handshake was NOT most of it - the per-statement round trips Mumbai -> Singapore are. Still ~1.3 s per step message. Not chased.

**Verified offline 2026-11-23:** the second call reuses the connection and checks it; a dead one is closed and
replaced; a failed call raises and drops it; warm handler calls share one. Mutations removing the check or the
drop are caught. `p2b_unit.py` in the notes.

## Paper trading on the option — DEPLOYED 2026-11-28 18:08 IST (Amar: "deploy now"), first live session Mon 2026-11-30

**The deploy**, Saturday, no session running; local `aws` CLI, ap-south-1, as in plan §7:
1. **Migration** `2026-11-28-option-paper.sql` applied 18:07:18 IST: `algo.option_paper_position` (20 columns, owner
   `neondb_owner`, empty) and `risk_control.daily_loss_limit_rupees`.
2. **This function:**
   - `algo-ssm-read` updated (+ `/algo/dhan/token`, read back);
   - **version 2 = `EUUQoC7Y…` (P2b) published as the rollback**;
   - code **`0b6JDpBsT7Til0wT7cSN6xlh0EWkabmL7ijAWPmxNq4=`** == the tested zip (8 modules).
   - Dry-run smoke with the switch off at 18:08:02: a synthetic 11-13 ENTRY answered `applied`, `dry_run`, no option
     key (the old answer), 2.62 s, nothing kept.
3. `risk-manager` deployed, then the `risk_control` row (see its README).
4. **`OPTION_PAPER=on`, `OPTION_PAPER_FROM=1795977000`** (Mon 2026-11-30 00:00 IST) at 18:08:47. Dry-run smoke at
   18:08:57: a REJECTED answered `rejected_stored` with `option: null` (the switch is read), nothing kept.
   - Afterwards: `position_master` 11 rows (none from the smokes, none open), `option_paper_position` 0 rows,
     error-notifier not invoked.

**NOT yet exercised: a real Dhan read.** The stored token expired at 08:30 today (no weekend mint), so an option
buy cannot be smoked until Monday's 07:30 mint. The first real option-chain read is Monday's first entry, unless a
dry-run ENTRY is run before 09:15 on Monday.

**Rollback:** `aws lambda update-function-configuration --function-name order-manager --environment
"Variables={}"` (switch off = the old behaviour). The old code is version 2 (`EUUQoC7Y…`). The option table can stay.

## Paper trading on the option — what was built (2026-11-28)

Amar 2026-11-28: "no we are doing paper trading why do you want to place live order", then "Paper on the option".
The build plan was approved ("Approve as recommended"):
`pattern-recorder/replay/out/p5-dhan-orders-2026-11-28/paper-options-build-plan.md` (git-ignored, with the
measurements R1, exits on options, L1).

**What it does, with `OPTION_PAPER=on`.** Every index step that changes quantity, and only when it is *applied*,
also buys or sells the paper option **1 strike in the money** ([option.py](option.py)):
- **The contract:** a CE one strike below ATM for a long, a PE one strike above for a short. ATM = the nearest 50 to
  the step's index price. Expiry = the nearest expiry; on its own expiry day, the next one.
- **ENTRY / RE-ENTRY:** buy lots × lot size. One option-chain read gives the security id and the live price. A
  re-entry buys a new contract.
- **T1:** sell lots × lot size. **Any close:** sell everything still open. Each sale takes one quote read.
- **REJECTED, STOP MOVE, WATCH ENDED, a duplicate, a step for REFUSED:** no Dhan read, no option change.
- **Exits are still decided on the index by the playbooks.** The contract with the callers is unchanged (v1).

**Where it goes.** One row per index position in **`algo.option_paper_position`**
([schema](schema.sql), [migration](../migrations/2026-11-28-option-paper.sql)):
- the contract;
- buy / sell quantity and value, open units, realised rupees (gross) and estimated charges (rates unverified);
- every fill in `fills`.

It is written in the **same transaction** as the index step, after the Dhan reads and before the message. It has no
foreign key to `instrument_master`, which cannot name a weekly contract.

**The message.** The playbook's text is unchanged, plus one line, for example
`option: BUY NIFTY 01DEC 25750 CE x195 @ 142.50` or
`option: SELL NIFTY 01DEC 25750 CE x130 @ 168.10 | this sale +₹3,328`.

**Failure.** Any Dhan failure (HTTP error, missing strike or price, expired token, an unexpected shape) **raises**:
- the whole step rolls back, index step included;
- the caller's synchronous call raises, error-notifier pages, and the async retry runs it again (O4).

A DH-904 / 429 is paced and retried first ([dhan.py](dhan.py)): the option-chain family 1 call per 3 s, quotes 1 per
second.

**The switch and the cut-over.**
- `OPTION_PAPER` off (the default) = **exactly today's behaviour**. `compare_off.py` checked 14 steps against git
  HEAD: answers, messages and stored rows identical, no option row.
- `OPTION_PAPER_FROM` (epoch): a position entered before it has no option row and is left alone. From it on, a
  missing option row raises.

**Dhan access.** Data reads only: no order API, so no static IP. The token is auth-dhan-broker's
`/algo/dhan/token`; the client id comes from the token's JWT claim ([params.py](params.py)). `algo-ssm-read` adds
`/algo/dhan/token` ([iam/](iam/)).

**Verified offline 2026-11-28** (scripts in the plan's folder):
- `test_option_paper.py`:
  - a trend long through ENTRY / T1 / TRAIL, the strike, the expiry and expiry day, the line, realised rupees;
  - a short; REJECTED; the sweep's exit-and-watch, re-entry on a new contract, a close while out;
  - a Dhan failure on a buy and on a sale (everything rolled back);
  - a missing strike, the cut-over, a kill-switch close, a dry run, an option row out of step;
  - the Dhan client (429 retried, 500 raises, missing price raises) and the token reader.
  - Plus the risk manager's side. **ALL PASS; 10 / 10 mutations caught.**
- `check_real.py`: the 11 live positions of 2026-11-25 → 11-27 through this handler, priced from the pulled Dhan
  1-min data. **11 / 11 equal** to the measured 1 ITM contract, prices and units. Option net over those positions
  +₹36,888.
- **Neon temp-table test, 2026-11-28** (Amar "Run it"; project `nameless-mountain-15353651` / `Algo`, one
  transaction, `ON COMMIT DROP` tables only; `render_neon_sql.py` in the plan's folder). It ran the migration's own
  DDL and this code's own statements (`insert_option`, `lock_option`, `update_option`, and the risk manager's
  `read_control` / `read_options`):
  - the insert, the locked read (fills come back as JSON) and the T1 update are exact: open 65, realised ₹3,328.00,
    charges ₹90.46, 2 fills;
  - six refusals, each by its own constraint: units ≠ buy − sell, option type XX, lot size 0, no such position
    (foreign key), a second row for a position, a rupee limit of 0;
  - the README's kill-switch statement on a temp copy of `risk_control` with the new column carries ₹20,000 into
    the kill row.
  - **Afterwards:** no option table, no new column on the real `risk_control` (still 2 rows), no temp table left.
- The deploy and its dry-run smokes are done (section above). **Still not measured:** the latency added per step
  (the first real Dhan read is Monday 2026-11-30's).

## Deployment shape

- Zip the eight modules (`handler.py`, `positions.py`, `db.py`, `notify.py`, `config.py`, `option.py`, `dhan.py`, `params.py`; five before 2026-11-28) at the **zip root**, with LF endings.
- Handler `handler.lambda_handler`, set under Code → Runtime settings → Edit.
- Layers: `neon-db-driver` + `neon-access` (the same ARNs as the other database functions).
- Timeout 30 s and memory 128 MB, to be re-measured on the smoke: Neon wakes inside `connect()`. **From P2b: 256 MB.**
- **Environment:** none until 2026-11-28; since then `OPTION_PAPER=on` and `OPTION_PAPER_FROM=1795977000` (unset = off = the old behaviour).

**Its own role, `order-manager-role`** ([iam/](iam/)), with the trust policy and two inline policies:
- `lambda-logs`: its own log group `/aws/lambda/order-manager` only;
- `algo-ssm-read`: `ssm:GetParameter` on `/algo/neon/connection`, `/algo/telegram/brief` and (since 2026-11-28) `/algo/dhan/token`, plus `kms:Decrypt` conditioned on `kms:ViaService = ssm.ap-south-1.amazonaws.com`.

Other rules:
- **Never borrow another function's role.** CLAUDE.md: a borrowed role writes no logs at all and nothing alarms. Check the policy documents after creating them, not the role names.
- **No invoke grant to anyone in P1.** Each caller gets `lambda:InvokeFunction` on this ARN, in its own role, in P2a / P2b.
- **Subscribe its log group to `error-notifier`** (`add-permission` + `put-subscription-filter`, as in the [error-notifier README](../error-notifier/README.md)).

**Redeploy:** rebuild the zip (the eight modules at the zip root, LF endings), then `aws lambda update-function-code --function-name order-manager --zip-file fileb://order-manager.zip --region ap-south-1`. Check that the CodeSha256 equals the local zip's, and record it here.

## What it does not do

- No order, ever, in paper mode. Its only Dhan calls are the option-paper **data** reads above (with
  `OPTION_PAPER` on); P5 / P6 orders are parked (Amar 2026-11-28: "we are doing paper trading").
- No limits and no kill switch. The risk manager's decision is the only limit check (Amar 2026-11-22); `algo.risk_control` is read by [`risk-manager`](../risk-manager/README.md) (P2b), never here.
- It never formats a message: the text is the playbook's.
- It never decides a trade: it records and announces what a playbook decided.
