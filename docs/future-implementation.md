# Future implementation

← [Back to root README](../README.md) · [Components](components.md)

This file records work Amar has agreed to but that is not built yet. For each piece it gives the decision (in Amar's words where they matter), what exists today, and what is still open. Discussed 2026-11-20. Numbers are measured and dated, and must be re-measured, not edited, when behaviour changes.

Section 1 is built for every playbook: since P2b (2026-11-23) the risk manager owns both rules. Sections 2 and 3 were planned in detail on 2026-11-22; P1, P2a and P2b of that plan are live (2026-11-23), while P3 (the limits) and the Dhan phases are not built. See section 5.

---

## 1. The closing-auction rule, for every playbook

### What the market does (measured 2026-11-20, read-only)

Since **2026-08-03** NSE has run a closing auction. The NIFTY stocks stop continuous trading at 15:15. The index is calculated from their prices, so it stops moving until the official close lands. The measurements below use **price only, not volume**.

| NIFTY index, 5-min candles | Before 08-03 (34 sessions) | Since 08-03 (76–77 sessions) |
|---|---|---|
| 15:15 / 15:20 candles flat (high = low) | 0 of 68 | 153 of 154 (11-03 moved 0.1 pt) |
| 15:15 candle range, median / max | 14.9 / 35.7 | 0.00 / 0.10 |
| 15:15 close vs 15:10 close, median / max | 5.8 / 33.0 | 0.3 / 4.8 |
| 15:25 close vs 15:20 close (the jump to the official close), median / max | 7.3 / 39.1 | 24.9 / 201.0 |

The future does not follow that jump. Over 15:10 → 15:25, the future moved a median 9.1 pts against the index's 24.2. The gap between the two moves was a median 22.6 pts (90th percentile 64), and their correlation was 0.57, against 0.75 before 08-03.

For example, on 08-03 the index rose 201 pts while the future moved −1. So the index's 15:25 candle is a price nobody traded, and an option holder did not see it.

The auction close is also the day's high or low on 17 of 76 sessions. On those days the next day's index PDH or PDL is an auction print.

### The rule (Amar, 2026-11-20)

**No new trade after 15:00. Any live trade out at 15:15, whichever playbook.**

"Out at 15:15" means the close of the **15:10 candle**. Candles are stamped at their start, so that close is the 15:15 price.

### Where it stands

| | No new trade after 15:00 | Out at 15:15 |
|---|---|---|
| strategy-range-liquidity-sweep | already true (no entries or re-entries after 15:00) | **deployed 2026-11-20 22:48 IST** as an **interim** (CodeSha256 `VGiH4SNG…`, version 8 = the code before). A trade still in after the 15:10 candle gets `CLOSED AT 15:15`, sent at the 15:16 tick. See its [README](../strategy-range-liquidity-sweep/README.md) |
| strategy-trend-pullback | already true: signals stop at **14:30**, which Amar kept | **since P2b (2026-11-23)**: it follows its trades, and the risk manager's time exit closes what is still open at the 15:10 close (`TREND CLOSED AT 15:15`, sent on the 15:16 tick) |
| pattern-recorder alerts | not trades. They keep going to Telegram after 15:00 (Amar: keep them) | n/a |
| any new playbook | the risk manager's `exit_only` from the 15:00 candle close (since P2b) | the risk manager's `force_exit("TIME EXIT")` from the 15:10 candle (since P2b); the playbook closes at that close after its own stop / target |

### Sweep replay behind the interim

The replay was read-only. It ran the production modules over 110 sessions (06-15 .. 11-20), with the output in `pattern-recorder/replay/out/auction-close-2026-11-20/`, which is git-ignored. With the gate off it reproduces the README's 24 entries and +11.53R.

Only 2 of the 24 trades were still in at 15:10, and the jump never hit a stop or T2.

| End rule | 08-13 long | 11-13 long (live) | 17 trades since 08-03 |
|---|---|---|---|
| as it was: follow the index to the 15:25 print | +0.38R | +0.75R | +9.91R |
| **out at 15:15 (chosen)** | −0.59R | +0.67R | +8.87R |
| out at 15:20 (after the 15:15 candle) | −0.58R | +0.66R | +8.87R |
| follow the future to 15:30, minus the 15:10 basis | −0.65R | +0.68R | +8.82R |

Amar chose out at 15:15. Out at 15:20 gives the same result, but its message comes five minutes later. Following the future gives the same result too, but the sweep would need the future's candles, which it is not given today.

---

## 2. Risk manager: one place for every open trade, every new-trade limit and the kill switch

> **Built differently, 2026-11-22 / 11-23.** The design below (2026-11-20) was replaced by section 5: a `risk-manager` Lambda in front of strategy-manager on every run, live since P2b (2026-11-23). Kept as the record of what was asked.

### What Amar asked for (2026-11-20)

> "one place but it is risk manager which will handle open trade and restrict taking new trade also manage kill switch"

His answers when the design was worked through, verbatim:

| Question | Amar |
|---|---|
| Where it runs | "risk manager will run only if there is open trade not every run" |
| Who decides exits | "play book will have entry and exit stratergy before run it will check open position based on that it will run" |
| Kill switch | "it will stop running playbooks and if there is open position it will exit from that but data capture will run as it is" |
| Limits from day one | max open trades at once, daily loss limit, max trades per day. Numbers to be set after a replay. He did not choose "no opposite trade while one is open" |
| What an open position is | "when we implement trade management then we will check from there" (see section 3) |
| Where the check before a playbook runs lives | **strategy-manager** |
| How the kill switch is controlled | "it will be manage y database and dhan kill switch": a database flag plus Dhan's own account kill switch |

### The design as agreed

On every 5-minute tick:

1. **Data capture always runs**: loader, sentiment, classifier, F&O poller, pattern-recorder. The kill switch never touches it.
2. **strategy-manager checks before invoking any playbook**: the kill switch, open positions, trades today and loss today.
3. **If the kill switch is on**, no playbook runs, and every open position is exited.
4. **A playbook holding a position runs its exit strategy.** The **risk manager runs only while a trade is open**. It applies the 15:15 exit, the daily-loss exit and the kill-switch exit.
5. **A flat playbook runs its entry strategy only if every limit allows it**: before 15:00, fewer open trades than the maximum, fewer trades today than the maximum, and the daily loss limit not hit.

Every playbook has both an **entry strategy and an exit strategy**:
- sweep: 3 lots; book 2 at T1, the 15-min range midpoint, and run the last to T2 on a stop that never moves; exit on a close back beyond the level, then watch and allow one re-entry (T1 built in the sweep itself, live since 2026-11-22 15:44, to be revisited after trade-journal validation);
- trend: S3, a close back inside the 15-min range, then break-even after T1.

The common rules (15:00, 15:15, the limits, the kill switch) live in **one place**, so a new playbook gets them without copying them.

The AIML component register this repo follows splits these jobs into three components:
- **R2 RiskEngine**: the gate on new trades and the kill switch;
- **X3 PositionManager**: open trades;
- **C1 SessionStateMachine**: square-off time and the daily limits.

Amar wants them as one component.

### What this replaces

- The plan for `strategy-trend-pullback` to follow its own trades (agreed earlier the same day). Its exit strategy now arrives as part of this design.
- The sweep's own 15:15 exit, deployed as an interim. The risk manager takes it over.

### Open before building

- **The limit numbers**, set by a replay over the stored sessions with both playbooks' signals.
- **Dhan's kill switch**: its API and conditions are not yet checked against Dhan's documentation. It acts on Amar's **live trading account**, so calling it from code needs his explicit OK.
- **The kill-switch flag and the limits in the database**: which table and which columns. This is a data-model change.
- **How strategy-manager tells a playbook to run in exit mode, entry mode, or not at all.** That is a payload change, so `context_version` goes up for every playbook.
- **The trend playbook's exit strategy** as code: S3, T1 → break-even, the 15:15 exit, and its messages.
- **What "exit" means** while nothing places orders: a message only, or an order once section 3 exists.

---

## 3. Trade management (execution): what the risk manager reads positions from

Amar: open positions will come from trade management, "when we implement trade management then we will check from there".

**This is a scope change.** This repo places no orders today. A playbook sends a Telegram signal and Amar trades it himself. Trade management means placing and tracking orders through Dhan, recording positions, and reconciling them with the broker. It needs its own plan, covering what a position is, the tables, error and retry handling, and the Dhan calls.

Since 2026-11-22 (see "Order" below), it starts as a **paper-mode position record**, planned together with the risk manager. Dhan orders come last.

---

## 4. Smaller items from the same discussion

| Item | State |
|---|---|
| **Runbook scoring at the auction close.** [session-observation-runbook.md](session-observation-runbook.md) resolved a row at "the 15:25 close if none", which is the auction print. Scoring at the 15:10 close re-scores 3 of 17 rows: 11-16 from +0.38% to +0.10%, 11-19 from +0.99% to +0.94%, 11-20 from +1.07% to +1.01% | **done 2026-11-23** (Amar: "Yes, re-score"). The runbook now resolves at the 15:10 candle's close, and the 3 rows are re-scored with their old values in `notes`. Outcome and agreement are unchanged; row 13 stays `reversal` at +0.10% |
| **Index PDH/PDL can be auction prints** (17 of 76 sessions) | kept as printed: it matches Amar's chart and NSE's official high/low. Revisit only with evidence |
| **F&O trades to 15:40**, but every component stops at 15:30, so the futures' 15:30 / 15:35 bars are never stored | separate decision, not taken |
| Classifier and sentiment rows at 15:15 / 15:25, pattern-detector (log-only), the poller's basis at 15:20 / 15:25 | cosmetic: nothing acts on them after 15:00. No change |
| [architecture.md](architecture.md) / [flow.md](flow.md) describing the freeze | not yet. This file is the record for now |

## Order (decided by Amar 2026-11-22: "B: paper first")

Three orders were put to Amar:

- **A:** trade management with Dhan orders first, then the risk manager. This was the earlier proposal.
- **B:** one plan for both, with a paper-mode position record first.
- **C:** the risk manager first, on an interim position source that trade management would later replace.

He chose B:

1. **One plan covers both** trade management and the risk manager. It includes the replay that sets the limit numbers.
2. **First build: trade management's position record, in paper mode.**
   - A playbook's sent signal opens a position.
   - The playbook's exit strategy, or the risk manager, closes it.
   - Every exit is a Telegram message. No order is placed.
3. **The risk manager runs on that record.** It applies no new trade after 15:00, out at 15:15, the limits, and the database kill-switch flag.
4. **Last, each behind its own approval:** Dhan order placement, and Dhan's account kill switch. Both act on Amar's live account.

This section records the order only. The plan was written the same day, and section 5 holds the decisions taken under it: where the risk manager runs, the data model, the lots, the limits, and the trend playbook's exit rule (backlog item 5). What an exit does once Dhan orders exist is still open; it belongs to P5 in section 5's build order.

---

## 5. Paper positions + the risk manager: decided 2026-11-22, P1 + P2a + P2b live (2026-11-23)

**Amar's decisions, 2026-11-22.** The full plan with the options and their trade-offs is outside the repo, in the side-session notes: `paper-positions-risk-manager-plan-2026-11-22.md`, `p0-limits-findings-2026-11-22.md` and `p1-build-plan-2026-11-22.md`.

### What runs where

- **A new `risk-manager` Lambda, in front of strategy-manager:** pattern-recorder → risk-manager → strategy-manager → playbooks.
- It runs on **every** tick and alert:
  - it checks the kill switch and the open positions;
  - it stops duplicate entries;
  - it decides which function each playbook runs (entry or exit).
- **Kill switch on:** strategy-manager is not invoked, and the risk manager closes the open positions itself.
- **strategy-manager stays a pure router.** This replaces the 11-20 answers "runs only when a trade is open" and "the check is in strategy-manager".

### Who owns which rule

- **Hard rules are the risk manager's only:** no new trade after 15:00, and every trade out at 15:15. The sweep keeps its own 15:00 window and 15:15 step in its derivation as the alert register's record (Amar 2026-11-23), but the trade decision is the risk manager's: exit-only after 15:00, and the 15:15 close sent only on its time-exit instruction.
- **Strategy rules are the playbooks':** SL, target and exit conditions. The playbook closes the position.
- **15:15 is a "time exit" instruction.** The playbook applies its own SL / target on the 15:10 candle first, then closes the rest at the 15:10 close.

### One common order manager, its own Lambda

- **`order-manager`**, "like what we have in algo_trading app just dont save order directly save the position in position master". Its contract:
  - the playbooks decide, and hand each step over as one signal per synchronous call;
  - in paper mode no order is saved: the step updates `position_master`;
  - Dhan orders go through it later.
- **The playbook writes the message text**, and the order manager sends it only when the step is applied.
- **No extra limit check when an entry is written.** The risk manager's decision is the only limit check. Same-candle duplicates are stopped by the table's unique key.

### Data model, sizing and pricing

- **Data model:** `position_master`, after trading-algo's. One row per position, balance = remaining quantity. The sweep writes its order when it sends.
- **Lots:** **3 for both playbooks, 2 sold at T1, 1 at T2.** Each playbook's own rules say *when*:
  - sweep: T1 = the range midpoint;
  - trend: T1 = entry + 2R; the last lot takes stop-to-entry at 1R, the previous-candle trail and the time exit.
- **Quantities in units:** lots × the near-month NIFTY future's lot size, 65 at 2026-11-22, stored per position.
- **Paper fill at the signal:** the sweep's confirming-candle close; trend's trigger close.
- **Loss unit:** index points × lots.

### Messages and the kill switch

- **A refused signal is still sent, marked REJECTED with the reason,** and is stored as `REFUSED`.
- **The DB kill switch resets each morning**, like Dhan's own switch.

### The limits

- **O1: max open 4, max 8 trades per day, daily loss 500 point-lots. Trend repeats allowed.**
- They come from the read-only replay over 87 sessions with the decided rules:
  - no limits: +5,248 point-lots, worst day −408, max drawdown −514;
  - every binding limit cost points, and none lowered the drawdown;
  - a daily-loss exit at 150–250 did harm;
  - O1 costs −7%.

### Build order

| Phase | What | State |
|---|---|---|
| P0 | the limits replay | **done** 2026-11-22 |
| **P1** | `algo.position_master` ([migration](../migrations/2026-11-22-position-master.sql)) + [`order-manager`](../order-manager/README.md), paper mode | **deployed 2026-11-22 21:16 IST**, called by nothing until P2a (22:16 that day). Offline tests, the Neon temp-table test and a dry-run smoke all passed |
| P2a | the sweep records every entered trade through `order-manager` (trend moved into P2b), its own messages unchanged | **deployed 2026-11-22 22:16 IST** (sweep `6+z2tBAr…`). Its two-session live check was skipped: no sweep trade ran through it before P2b, on Amar's call |
| P2b | `risk-manager` (with `risk_control`), the context v4 `run` decision, trend exit mode with the decided rule, time / forced exits, REJECTED signals, every trade message through `order-manager` (the sweep's own 15:00 / 15:15 kept as the register's record) | **live 2026-11-23 17:22 IST**, deployed after the close; first session Wed 2026-11-25. Offline: whole-chain replay on 87 sessions = P0 (161 positions, 5,248.11 point-lots; O1 4,901.89), 29/29 mutations caught; the smoke on 11-23's final tick sent nothing. Deploy log + rollbacks in the [risk-manager README](../risk-manager/README.md) |
| P3 | the O1 limits into `risk_control` | — |
| P5 / P6 | Dhan order placement / Dhan's kill switch | each its own plan + approval; **they act on the live account** |

### Measured at the P1 deploy, open to decide

- **A warm `order-manager` call takes ~1.5 s** (a fresh Neon TLS connection each call), so each step's message will arrive about 1.5 s later than today. Reusing the connection would cut that.
- **Memory is 100 of 128 MB.**

### Measured at the P2b deploy (2026-11-23), open to decide

- **Keeping the connection cut a warm `order-manager` call only to 1.33 s** (from 1.55 s). The handshake was not most of it: the rest is per-statement round trips to Neon in Singapore. 256 MB since P2b; 100 MB used.
- **`risk-manager` adds ~2.5 s per tick** before strategy-manager (a new connection each call; none kept there).
- **So a sweep message now reaches the phone about 4 s later than under P2a** (risk-manager ~2.5 s + order-manager ~1.3 s). Open: whether that is worth chasing (a kept connection in risk-manager too, fewer statements per call).
