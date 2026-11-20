# Future implementation

← [Back to root README](../README.md) · [Components](components.md)

This file records work Amar has agreed to but that is not built yet. For each piece it gives the decision (in Amar's words where they matter), what exists today, and what is still open. Discussed 2026-11-20. Numbers are measured and dated, and must be re-measured, not edited, when behaviour changes.

Nothing in sections 2 and 3 is built. Section 1 is built for one playbook (the sweep).

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
| strategy-trend-pullback | already true: signals stop at **14:30**, which Amar kept | only stated in the signal ("hold the rest to the 15:10 close"). The playbook does not follow its trades, so nothing is sent at 15:15. Section 2 covers this |
| pattern-recorder alerts | not trades. They keep going to Telegram after 15:00 (Amar: keep them) | n/a |
| any new playbook | must hold to the rule | must hold to the rule. The risk manager in section 2 is meant to make this automatic |

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
- sweep: exit on a close back beyond the level, then watch and allow one re-entry;
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

**This is a scope change.** This repo places no orders today. A playbook sends a Telegram signal and Amar trades it himself. Trade management means placing and tracking orders through Dhan, recording positions, and reconciling them with the broker. It needs its own plan, covering what a position is, the tables, error and retry handling, and the Dhan calls. It comes before, or alongside, the risk manager's position check.

---

## 4. Smaller items from the same discussion

| Item | State |
|---|---|
| **Runbook scoring at the auction close.** [session-observation-runbook.md](session-observation-runbook.md) resolves a row at "the 15:25 close if none", which is the auction print. Scoring at the 15:10 close would re-score 3 of 17 rows: 11-16 from +0.38% to +0.10%, 11-19 from +0.99% to +0.94%, 11-20 from +1.07% to +1.01% | **open**: needs Amar's sign-off |
| **Index PDH/PDL can be auction prints** (17 of 76 sessions) | kept as printed: it matches Amar's chart and NSE's official high/low. Revisit only with evidence |
| **F&O trades to 15:40**, but every component stops at 15:30, so the futures' 15:30 / 15:35 bars are never stored | separate decision, not taken |
| Classifier and sentiment rows at 15:15 / 15:25, pattern-detector (log-only), the poller's basis at 15:20 / 15:25 | cosmetic: nothing acts on them after 15:00. No change |
| [architecture.md](architecture.md) / [flow.md](flow.md) describing the freeze | not yet. This file is the record for now |

## Proposed order (not agreed)

1. Plan trade management.
2. Plan the risk manager, including the replay for the limit numbers and the Dhan kill-switch check.
3. Build both, each behind its own approval.
