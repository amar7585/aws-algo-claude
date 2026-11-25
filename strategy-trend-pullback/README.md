# strategy-trend-pullback

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Components](../docs/components.md)

The trend-day playbook: on a day the classifier reads as a **trend day**, a key
level broken in the trend's direction and then **held** — the trade is the hold,
with the trend. It fills the strategy registry's "bullish trending — pullback
continuation" slot, with the bearish mirror.

**It is not a sweep and not a fade.** Sweeps belong to
[`strategy-range-liquidity-sweep`](../strategy-range-liquidity-sweep/README.md)
on no-trend days (Amar, 2026-11-09).

| | |
|---|---|
| Status | **sending since 2026-11-11 16:56 IST** (`TELEGRAM_ENABLED=on`; in shadow from 2026-11-09 22:33, first shadow session 2026-11-11). Gap-aware stop (option C) deployed 2026-11-11 18:53 IST, CodeSha256 `kmBZxADI…` (previous `/ZpB2MYN…`). Option F stop (range low / high when PDH / PDL is beyond the range) deployed 2026-11-19 17:24:33 IST, CodeSha256 `MZZt7vDH…` (previous `kmBZxADI…`), with market-classifier's matching rule. **Rule G** (stop = a close back inside the 15-min range; a trigger candle closed inside the range stands down) deployed 2026-11-19 18:29:28 IST, CodeSha256 `AhXhKHn7…` (previous `MZZt7vDH…`), before any session ran on F; first session 2026-11-20. strategy-manager routes to it since 2026-11-09 22:37 (`TREND_PLAYBOOKS=strategy-trend-pullback`) |
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, **7 modules** since 2026-11-25 (5 before P2b; `notify.py` removed 2026-11-25), **no layers** |
| Trigger | asynchronous invoke from `strategy-manager`, for a v2 alert on a day whose `day_trend` is up or down |
| Reads | **nothing** — the context carries v2's alert, today's index candles, the 15-min snapshot and the daily read |
| Writes | **nothing** — its log; since P2b `order-manager` records its steps and sends its message |
| Secrets | **none** since 2026-11-25 (`/algo/telegram/brief` until then; unused since P2b) |

## 2026-11-25: its own Telegram code removed — DEPLOYED 2026-11-25

**Amar 2026-11-25 ("Do both", the cleanup).** Since P2b `order-manager` sends the message, so `notify.py` (imported by
nothing), `TELEGRAM_PARAMETER_NAME` / `HTTP_TIMEOUT_SECONDS` in `config.py` and the role's `telegram-read` policy are
gone. **Kept:** `TELEGRAM_ENABLED=on` - unset still means shadow (order-manager records, the text is only logged).
Checked with the sweep's change on 25 sessions: every call, step, message and position identical. **Deployed
2026-11-25 22:19:44 IST**, CodeSha256 `xNMXvVTvP2MjsQ4YAgoVNVDPWJM1+YarICt10pe6pqU=` (7 modules); configuration
unchanged. **Rollback: version 4 = `U2kH2J2j…`** (the stop-move timing fix). `telegram-read` deleted from the role 22:20
IST (IAM simulate: the role now reads no parameter at all).

## Since 2026-11-25: each stop move goes out at the close that sets it — DEPLOYED 2026-11-25

**Approved by Amar 2026-11-25** ("Approve" the timing fix; the trail rule itself kept: "Keep previous-candle").
`exits.steps()` used to work out a stop move only when it processed the *next* candle, so every move — the
`TREND STOP TO ENTRY` message and each trail move — reached `order-manager` one tick late. On 2026-11-25 the 10:55
candle touched +1R (high 25879.65 vs 25857.40), so the new stop 25852.60 was known at 11:00; it went out at 11:07:06,
with T1. Now the move is returned on the candle whose close sets it, the label it always carried.

- **Unchanged:** every stop, price, exit and lot; the trail rule; T1; the stop line; order-manager, risk-manager and
  every other function.
- **record.py:** a stop move was never tagged `(late: ...)` — that hid this lag. Now a stop move from a late or
  retried tick is tagged like any other step. On a `force_exit` run (it closes what is open at the newest candle) a
  move set by the newest candle is left out: it would apply only after that close (handler.py passes `closing`).
- **Checked offline (2026-11-25):** the old and new `steps()` on the 166 grid candidates and the live 2026-11-25
  position, cut at every tick (7,518 ticks): every full day identical; at each tick the new code adds at most one stop
  move, on the newest candle, and equals the old code one tick later; all 250 moves come one tick earlier (93 of them
  the stop to entry). Harness: `pattern-recorder/replay/out/trend-stop-move-timing-2026-11-25/diff_exits.py`
  (git-ignored, with the old `exits_old.py`). The 2026-11-25 one-day runner on this code (the whole chain, tick by
  tick, real order-manager): 12 steps, 7 messages, 2 positions and +117.92 point-lots, all == live; the stop to entry
  sent at 11:02:01 instead of live's 11:07:06, T1 still 11:07. Only "deployed == repo zip" fails (not deployed).
  Report: `pattern-recorder/replay/out/validation-2026-11-25/wt_1125_trendfix/report_1125.md`.
- **Deployed 2026-11-25 20:35:43 IST** (Amar: "Deploy now"), CodeSha256
  `U2kH2J2joOvbQa8sHfM+WIDKpSswAadJqu3D0IYRJ0k=`, 16,751 B (`exits`, `record`, `handler` changed; 5 modules
  byte-equal); configuration and environment unchanged (`TELEGRAM_ENABLED=on`, `ORDER_MANAGER_FUNCTION=order-manager`,
  30 s, 128 MB). **Rollback: version 3 = `2jBUCjCH…`** (C1), published just before. No live smoke: a real payload
  could write a position and send a message; the replay above ran this zip's handler on every 2026-11-25 tick.
  First live session: Thu 2026-11-26.

## Since C1 (2026-11-23): holds at the option walls too — DEPLOYED 2026-11-23

**Decided by Amar 2026-11-23** ("Trend levels (C1)", "Approve as recommended"; deploy: "commit and deploy the changes",
the same night instead of after the first P2b session). On a trend day a hold counts at PDH / PDL / ORH / ORL **or at
the call wall / put wall** (its own level, or one within NEAR); a key level is preferred when an event is near both,
so every key-level candidate is exactly as before. pattern-recorder's trend branch alerts on the same set.
**Unchanged:** entry, the stop line (ORH / ORL) and its buffer, R, T1, the exit, the window, rule G, the limits.

- **Why:** 2026-11-23 trended down all day between ORL and PDL with every hold at max pain, the put wall or a swing
  low. Replayed on 88 sessions (`pattern-recorder/replay/out/miss-2026-11-23/`, O1, the decided exit): +2,213
  point-lots over the key set, profit factor 1.87 -> 2.22, max drawdown -467 vs -487, the same worst day. The evidence
  is 35 sessions with option rows; without 11-23 the interval crosses zero. Max pain (C2) and the re-look (D) were not
  taken; C2 is re-measured with the options overlay after the Fri 2026-12-11 close.
- **Message:** a wall hold reads `hold at PUT WALL 25500.00 (...)`; a hold only near a wall `hold at SWING LOW, near
  the PUT WALL (...)`. **Accepted (Amar):** when a newly alerted wall event and a key-level event fire on the same
  candle, whichever reaches order-manager first opens the position and its message names it (seen once in 88 sessions,
  2026-10-21 10:35); the trade is the same.
- **Checked before deploy:** 18 unit tests + 9 / 9 mutations on the zip's modules; the real code == the replay's C1 on
  all 184 positions (every exit step); the whole P2b chain on 11-23: only the two put-wall shorts added, +716.40.
- **Deployed 2026-11-23 22:56:57 IST**, CodeSha256 `2jBUCjCH+gy+ITeyVgmyTnTY+vVXHy4uss/7wj6QFzs=` (`config`,
  `pullback`, `report` changed; 5 modules byte-equal); configuration and environment unchanged. **Rollback: version 2 =
  `dlZK1gOx…`** (P2b). pattern-recorder followed at 22:57:04. First live session: Wed 2026-11-25, with P2b.

## Since P2b (2026-11-23): it follows the trade — DEPLOYED 2026-11-23

**Approved by Amar 2026-11-23 ("Approve P2b"); deployed after the close ("deploy now").** Code 15:35:52 IST, CodeSha256 `dlZK1gOx2pwGmD5pNFe/rbPI0Psjxr3u4+SG4t74MEI=`, `ORDER_MANAGER_FUNCTION=order-manager`, `TELEGRAM_ENABLED=on`; rollback: version 1 = `DVj+KKye…` (rule G, one alert in, one message out). In the chain from 17:22:31 IST; first live session Wed 2026-11-25. Plan: `p2b-build-plan-2026-11-23.md` in the notes folder. The sections below describe the entry rules, unchanged.

**Two kinds of run** (context v4, through [`risk-manager`](../risk-manager/README.md) → `strategy-manager`):

- **An alert on a trend day (entry).** The candidate is built exactly as before ([pullback.py](pullback.py)) and
  becomes an **ENTRY** through [`order-manager`](../order-manager/README.md): 3 lots at the trigger candle's close
  (the fill at the signal), `stop_loss` = the stop line (ORH for a long, ORL for a short), `t1_target` = entry +
  2R, the candidate text as its message. When the risk manager allows no entry (after 15:00, a limit, the daily
  loss) it is sent as **REJECTED** — stored REFUSED, its message headed `TREND PULLBACK REJECTED - <reason>`.
  A second alert on a candle already entered is answered `duplicate` (one position per candle and side), with
  no message.
- **A tick while it holds a position (exit mode).** The risk manager passes this playbook's open rows in `run`,
  so it reads no database. The decided exit ([exits.py](exits.py)) is re-derived over today's closed candles and
  only what `order-manager` does not have yet is sent — read, then catch up, like the sweep.

**The exit — "Previous-candle trail" (Amar 2026-11-22),** 3 lots for both playbooks, 2 at T1:

| | |
|---|---|
| stop | a 5-min **close** back inside the 15-min range — out at that close (`TREND STOPPED`) |
| T1 | entry + 2R: **2 of 3 lots** sold there (`TREND T1`) |
| to entry | once price touches +1R, the stop moves to entry from the next candle (`TREND STOP TO ENTRY`) — sent at the close of the candle that touched +1R (since 2026-11-25; one tick later before) |
| trail | then the stop trails the previous closed 5-min candle's low (high for a short), never back past entry — stored, **not messaged** (Amar 2026-11-23); each move stored at the close that sets it |
| protected stop | hit on a touch, filled at the candle's open if it opens through it (`TREND TRAILED OUT` / `TREND BREAK-EVEN`) |
| one candle, both | the protected stop comes before T1 |
| 15:10 | the playbook does not close on its own: the risk manager's `force_exit("TIME EXIT")` closes it at the 15:10 candle's close, after its own steps on that candle (`TREND CLOSED AT 15:15`); `DAILY LOSS EXIT` at the newest close |

**Verified offline 2026-11-23.** `exits.steps` reproduces the decided replay (`exits_grid.py`, key
`edge|half|1R|prev5|2.0`) on all 166 candidates, with half sold at T1 as replayed (worst 0.0025 pts — prices
rounded to 2 dp). The whole chain on the 87 clean sessions reproduces P0's paths with 2 of 3: 156 trend
positions, each within 0.65 point-units (T1 rounded to 2 dp), and the total matches to the paisa. With O1
limits: 4 REFUSED, each with its REJECTED message. Late, repeated and dropped ticks and injected `order-manager`
failures all end in the same record, with no message twice. Harness: `prelim/p0/p2b_*.py` in the notes.

## Reference session — 2026-11-06 (n = 1)

NIFTY broke PDH 24,579.90 at 09:45 (the day trend turned `up` with that candle;
the classifier's 10:00 row was the first to show it), held on it 10:50–12:05,
then rallied. v2's 10:55 alert — a hold at PDH (C2/touch_hold) — becomes:

```
TREND PULLBACK LONG - 2026-11-06 10:55
Day trend up | hold at PDH 24579.90 (C2/touch_hold, candle 10:55)
Entry  24589.45  (index close of the 10:55 candle)
Stop   exit on a 5-min close below 24539.20 (min of PDH / ORH 24579.90 / 24539.20)
       nominal 24519.30, risk 70 pts
T1     24702.60 session high (+113, RR 1:1.6) -> then stop to break-even
T2     hold the rest to the 15:10 close
v2: vol z -0.80, rvol 1.05x | 15m volatile-expansion|range-bound
Index points. Not an order.
```

What followed: the 11:00 candle wicked to 24,563.35 and closed back above PDH;
no candle closed below PDH again; T1 was reached; the 15:10 close was 24,938.90.
**One session is not a calibration.**

## The rules ([pullback.py](pullback.py))

All on the **index** — the chart the levels are drawn on.

| | Rule | Why |
|---|---|---|
| Setup | v2's **C2** (retest / hold of a level broken this session in the trend's direction) or **R1** (hold candle), in the trend's direction, at PDH/PDL/ORH/ORL (its own level or one within NEAR) — **or, since C1 (2026-11-23), at the call wall / put wall** the same way; a key level is preferred when an event is near both | breakout-and-hold only. C1 (the break itself) was weak in the replay; R2 sweeps belong to the sweep playbook |
| Window | trigger candle closes 09:45–14:30 | Amar's short-into-liquidity window |
| Entry | the index close of the trigger candle | the alert lands ~2 min after it |
| Stop (S3) | exit on a 5-min **close back inside the opening range** — below the range high for a long, above the range low for a short: market-classifier's `day_trend` off line (rule G, 2026-11-19 — inside the 15-min range is no trend). A trigger candle that has itself closed back inside the range stands down. Nominal stop = that line − buffer, buffer = max(0.03% of price, 0.5 × mean range of the last 6 candles) | the trend's own off line, so the trade ends exactly where the trend does (rule G section below) |
| T1 | the session high before the trigger candle (low for a short), then **stop to break-even** on the remainder | Amar's break-even rule from short-into-liquidity. None when the entry is already beyond it |
| T2 | the remainder held to the 15:10 close | the index freezes for the closing auction from 15:15 |
| RR gate | **none** | a 1.5 gate kept the worse setups in the replay |

Every gate check runs even after one fails, so a stand-down names all its
reasons. Missing data — the trigger candle, the opening range, a missing or
stale daily read — **raises**: an alert that cannot be judged must not look like
a quiet stand-down.

## Replay (2026-11-09, read-only)

[pattern-recorder/replay/out/trend_playbook_replay.md](../pattern-recorder/replay/out/trend_playbook_replay.md)
(git-ignored output), 78 clean sessions 2026-07-01 → 2026-11-06, v2.1 alerts with
the day trend read at the live 15-min lag:

- **88 holds** in 43 sessions (median 1 per such session); 47 on days that ended
  as trend days that way.
- On those 47, S3 was **never** stopped before T1; 32 reached it (median +23 to
  T1-or-stop, +30 on the remainder).
- On the 41 that did not, every stop lost (median about −17). **The trend read is
  right about half the time — that, not the stop, is this playbook's risk.**
- ORH/ORL holds (65) did better than PDH/PDL holds (23).

Verified 2026-11-09: `pullback.evaluate()` equals the replay's S3 values on all 88
holds (entry, stop, risk, T1) and stands down the other 68 with-trend alerts
(40 breaks, 4 sweeps, 24 outside the window).

## Gap days (2026-11-11, option C)

The first live session, 2026-11-11, gapped wholly above PDH (ORL 25263.80 >
PDH 25084.05). Its three candidates — R1 holds at ORL, 12:40 / 12:45 / 13:15 —
carried the stop at PDH, risk 222–238 pts against 116–133 to T1; the index fell
330 pts that afternoon without closing below PDH, so the stop never triggered.

Replay, 80 sessions 07-01 → 11-11, every hold, the playbook's own management (half
at T1, the rest at break-even or 15:10), index points:

| Option | trades | total pts | mean R | won / lost | worst |
|---|---|---|---|---|---|
| S3 as it was | 95 | +1653 | +0.18 | 59 / 36 | −195 |
| gap-aware stop only | 93 | +1874 | +0.67 | 57 / 36 | −162 |
| **gap-aware stop + the classifier's off line (chosen)** | 92 | +1899 | +0.68 | 57 / 35 | −162 |
| stand down on gap days | 56 | −138 | −0.10 | 30 / 26 | −145 |

19 gap days carry 39 of the 95 holds; on them the median risk halves (156 → 82).
Non-gap days are unchanged by construction, including the 2026-11-06 reference.
On 11-11 the three candidates' stop becomes ORL 25263.80 (risk 42 / 58 / 45); the
13:30 close 25215.40 stops them for −76 / −93 / −81 — a close-based stop can exit
beyond its nominal level. Descriptive, small sample: C differs from the gap-aware
stop alone on one trade (09-09 10:35).

Verified offline 2026-11-11: `pullback.evaluate()` equals that replay on all 92
option-C candidates; on the 56 non-gap holds the old and new code agree on every
field; on the 39 gap-day holds only the stop line moves (to ORL / ORH); the 68
other with-trend alerts still stand down.

## PDH above the range (2026-11-19, option F)

On 2026-11-19 PDH sat 135 points above the opening-range high; the range broke at
09:45 but the classifier's `day_trend` turned up only at 14:00, because it needed
a close above both. Option F turns the day on at the range break and, when PDH is
above the range (PDL below it), moves the off line — and this playbook's stop — to
the far side of the opening range. Everywhere else it is option C unchanged.

Full-chain replay ([trend_rules_replay.md](../pattern-recorder/replay/out/trend_rules_replay.md),
git-ignored), 86 clean sessions 07-01 → 11-19, every candidate counted, index points:

| Rule | candidates | total pts | mean R | won / lost | median risk | first per session |
|---|---|---|---|---|---|---|
| option C | 99 | +1963 | +0.67 | 63 / 36 | 61 | +305 (47) |
| on at the range, off at the range high itself | 127 | +1262 | +0.15 | 64 / 63 | 30 | +796 (67) |
| on at the range above VWAP, off at VWAP | 142 | +1150 | +0.16 | 87 / 55 | 52 | +883 (73) |
| on at the range, option C's line | 150 | +2349 | +0.48 | 93 / 57 | 49 | +602 (67) |
| **option F (chosen)** | 224 | +2957 | +0.36 | 147 / 77 | 77 | +1108 (76) |

F against C: +994 (+597 without its best day), better in Jul–Sep and Oct–Nov,
better on 32 days and worse on 17. The added trades are thin (mean +0.12 R) and
the stop is wide on those days (2026-11-06: risk 221 against 70, same +231). The
trend read is now on most of the time, so more candidates come from days that do
not end as trends (157 of 224) — their losses are smaller (mean −0.06 R against
−0.45) because the stop is wider.

Verified offline 2026-11-19: `pullback.evaluate()` equals the replay's option F
on all 259 candidates (entry, stop line, risk, T1; damaged SEP-future sessions
included); on option C's 117 playbook alerts the old and new code agree on 89,
and on the 28 where PDH / PDL is beyond the range only the stop line, its name,
the nominal stop, risk and RR change. One known edge, older than F: when the
trigger candle has itself closed past the off line but inside the buffer
(2026-07-16 13:20, one case), the playbook still sends it and it exits on the next
close (−8.5); the replay had stood it down.

**Replaced the same evening by rule G, before any session ran on F (next section).**

## Trend only outside the 15-min range (2026-11-19, rule G)

F kept the trend — and this playbook — on with price back inside the opening
range: 82 of its 273 replayed trades were entered inside the range, 10 on the
no-trend day 2026-11-13. Amar: inside the 15-min range is no trend, so no trend
trade. Rule G: the stop is a close back inside the range (below the range high for
a long, above the range low for a short), the classifier's off line; and a trigger
candle that has itself closed back inside the range stands down (this also closes
the 2026-07-16 edge above).

Full-ruleset replay from the deployed zips
([full_ruleset_report.md](../pattern-recorder/replay/out/validation-2026-11-19/full_ruleset_report.md),
git-ignored), index points, every candidate:

| Rule | trades | total pts | profit factor | max drawdown | entered inside the range | non-trend days |
|---|---|---|---|---|---|---|
| option C | 99 | +1963 | 2.09 | −976 | 19 | −1516 |
| option F | 273 | +3472 | 1.86 | −1340 | 82 | −1496 |
| **rule G (live)** | 165 | +1556 | 1.90 | −336 | 2 (now stood down) | −498 |

With the sweep playbook, 34 sessions 09-30 → 11-19: rule G +1148, profit factor
2.11, drawdown −210 (F +1467 / 1.69 / −1252). On the final code (the two
inside-range entries stood down): trend side 163 trades, +1562, PF 1.91, drawdown
−336, none inside the range; with the sweep +1155, PF 2.12, drawdown −210.
Verified offline 2026-11-19: `pullback.evaluate()` equals the replay's rule G on
all 185 candidates, and stands down the 2 whose trigger candle closed back inside
the range. Known cost: a retest that dips back
inside the range turns the trend off, so it is not this playbook's trade
(2026-11-19 10:30).

## Configuration

| Variable | Default | |
|---|---|---|
| `TELEGRAM_ENABLED` | *(unset)* | **unset = shadow**: the candidate is logged `WOULD SEND`, nothing is sent. From P2b the signal still goes to `order-manager` (recorded) with `message: null` |
| `HOLD_FAMILIES` | `C2,R1` | the setup |
| `WINDOW_FROM_MINUTE` / `WINDOW_TO_MINUTE` | 585 / 870 | 09:45 / 14:30, on the trigger candle's close |
| `STOP_BUFFER_PCT` | 0.03 | the sweep playbook's buffer rule |
| `STOP_BUFFER_RANGE_FACTOR` | 0.5 | |
| `STOP_BUFFER_LOOKBACK_BARS` | 6 | |
| `EXPECTED_CONTEXT_VERSION` | 4 | asserted against strategy-manager (3 until P2b, 2 until 2026-11-19) |
| `ORDER_MANAGER_FUNCTION` | *(unset)* | P2b: `order-manager`. **Required** — unset raises: every entry and step goes through it |

## Deployment shape

Zip the seven modules at the zip root (five before P2b); handler `handler.lambda_handler`; **no
layers**. Its own role `strategy-trend-pullback-role` ([iam/](iam/)): trust
policy, `lambda-logs` (its own log group only) and, from P2b, `invoke-order-manager`.
It reads no parameter: `telegram-read` (`/algo/telegram/brief` + `kms:Decrypt` via SSM) was deleted 2026-11-25,
unused since P2b. Never borrow another
function's role. `strategy-manager-role`'s `invoke-playbooks` needs this
function's ARN, and strategy-manager's `TREND_PLAYBOOKS` names it. Subscribe the
log group to `error-notifier`.

From P2b: eight modules (`exits.py`, `record.py`, `ordermgr.py` added; `notify.py` was no longer imported, and was
removed 2026-11-25 - seven modules since); the role gains `invoke-order-manager` ([iam/](iam/)): `lambda:InvokeFunction` on
`order-manager`'s ARN only. The environment adds `ORDER_MANAGER_FUNCTION=order-manager`. Deploy it together with
`strategy-manager` v4 and `risk-manager`.

## What it does not do

No orders, no strike selection, no database write. Before P2b it did not manage
the trade after the alert — each invoke judged one alert. From P2b it follows
its trades, and `order-manager` writes them; it still places no order.
