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
| Status | **sending since 2026-11-11 16:56 IST** (`TELEGRAM_ENABLED=on`; in shadow from 2026-11-09 22:33, first shadow session 2026-11-11). Gap-aware stop (option C) deployed 2026-11-11 18:53 IST, CodeSha256 `kmBZxADI…` (previous `/ZpB2MYN…`). strategy-manager routes to it since 2026-11-09 22:37 (`TREND_PLAYBOOKS=strategy-trend-pullback`) |
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 5 modules, **no layers** |
| Trigger | asynchronous invoke from `strategy-manager`, for a v2 alert on a day whose `day_trend` is up or down |
| Reads | **nothing** — the context carries v2's alert, today's index candles, the 15-min snapshot and the daily read |
| Writes | **nothing** — its log and its Telegram message are its output |
| Secrets | `/algo/telegram/brief` (to send) |

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
| Setup | v2's **C2** (retest / hold of a level broken this session in the trend's direction) or **R1** (hold candle), in the trend's direction, at PDH/PDL/ORH/ORL (its own level or one within NEAR) | breakout-and-hold only. C1 (the break itself) was weak in the replay; R2 sweeps belong to the sweep playbook |
| Window | trigger candle closes 09:45–14:30 | Amar's short-into-liquidity window |
| Entry | the index close of the trigger candle | the alert lands ~2 min after it |
| Stop (S3) | exit on a 5-min **close** below min(PDH, ORH) for a long (above max(PDL, ORL) for a short) — the line where the day trend turns off. On a **gap day** (the whole opening range above PDH / below PDL) that line is the opening-range low / high instead — the same line as market-classifier's `day_trend` off line (2026-11-11, option C). Nominal stop = that line − buffer, buffer = max(0.03% of price, 0.5 × mean range of the last 6 candles) | the only stop that survived real trend days in the replay (0 of 47 stopped before T1); S1 (pullback low) and S2 (close back across the level) were shaken out on a quarter to a third of them |
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

## Configuration

| Variable | Default | |
|---|---|---|
| `TELEGRAM_ENABLED` | *(unset)* | **unset = shadow**: the candidate is logged `WOULD SEND`, nothing is sent |
| `HOLD_FAMILIES` | `C2,R1` | the setup |
| `WINDOW_FROM_MINUTE` / `WINDOW_TO_MINUTE` | 585 / 870 | 09:45 / 14:30, on the trigger candle's close |
| `STOP_BUFFER_PCT` | 0.03 | the sweep playbook's buffer rule |
| `STOP_BUFFER_RANGE_FACTOR` | 0.5 | |
| `STOP_BUFFER_LOOKBACK_BARS` | 6 | |
| `EXPECTED_CONTEXT_VERSION` | 2 | asserted against strategy-manager |
| `TELEGRAM_PARAMETER_NAME` | `/algo/telegram/brief` | |

## Deployment shape

Zip the five modules at the zip root; handler `handler.lambda_handler`; **no
layers**. Its own role `strategy-trend-pullback-role` ([iam/](iam/)): trust
policy, `lambda-logs` (its own log group only) and `telegram-read`
(`/algo/telegram/brief` + `kms:Decrypt` via SSM). Never borrow another
function's role. `strategy-manager-role`'s `invoke-playbooks` needs this
function's ARN, and strategy-manager's `TREND_PLAYBOOKS` names it. Subscribe the
log group to `error-notifier`.

## What it does not do

No orders, no position sizing, no strike selection, no database write. It does
not manage the trade after the alert — each invoke judges one alert.
