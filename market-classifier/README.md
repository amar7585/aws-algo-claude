# market-classifier

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md)

Turns one **measurement** snapshot into **judgement**: reads the
`algo.intraday_fno_data` row `intraday-market-sentiment` just wrote, classifies it
with the shared [`market-classifier` layer](../layers/market-classifier/README.md),
derives the futures-OI buildup, writes `algo.intraday_sentiments`, and invokes
`pattern-detector`.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 6 modules |
| Layers | `neon-db-driver` + `neon-access` + `market-classifier` (the scoring rules — **v3** since 2026-11-13 12:31 IST, the 30-minute volatility test, see [Volatility](#volatility); v2 before) |
| Schedule | **none** — invoked by `intraday-market-sentiment`, once per snapshot (25×/day) |
| Reads | **nothing from the database** — the measurement row arrives in the payload |
| Writes | `algo.intraday_sentiments` (one judgement row per snapshot) |
| Secrets | `/algo/neon/connection` (to write) |

## Why it is its own function

The classification used to run *inside* `intraday-market-sentiment` via the
layer, stored as columns on the one wide `intraday_market_sentiment` row. It was
split out so each function has a single job:

- **`intraday-market-sentiment` measures** — price, indicators, OI, straddle,
  VIX, the `*_change_pct` deltas → `intraday_fno_data`.
- **this function judges** — regime, structure, buildup, day_trend and the
  swing/volatility reads → `intraday_sentiments`.

The two tables join 1:1 on `(security_id, instrument_type, snapshot_ts)`.

## One rule set, both frames

The scoring is the layer's `classify()`, the **same** function
`daily-market-sentiment` runs on daily candles. This function only assembles the
5-minute-frame inputs and calls it — it re-implements no rule. That is the whole
reason the rules live in a layer: a daily row and an intraday row are on one
scale. See [inputs.py](inputs.py) and the [layer README](../layers/market-classifier/README.md).

**What it assembles** (all from the payload, no re-fetch):

- the **scored bar** — `close` (= `spot`) plus the stored `sma9/50/100/200` and
  `rsi`. Measurement computed these off the ~200-bar history it alone fetches;
  the classifier reads them straight off the row.
- the **structure read** — `read_structure()` over **today's session bars**
  (handed over in the payload). structure is a classification answer, so the
  swing read lives here, not on the measurement side.
- the **volatility inputs** — `vix`, the `vix_baseline` (previous snapshot's
  VIX), the row's `recent_range_vs_normal` (the range test, see
  [Volatility](#volatility)), and — reported, no longer voting — `day_range`,
  the VIX-implied `expected_move` and `session_elapsed` from the **run clock**
  (at a 09:45 run the bar stamped 09:45 has zero elapsed time, so 30 minutes
  have passed, not 35).

## Volatility

`regime` is volatile-expansion when either test fires; otherwise it is the
swing read (trending, else sideways).

| Test | Fires when | Since |
|---|---|---|
| VIX | VIX moved ≥ 5% against the previous snapshot | unchanged |
| Range | the **last six closed 5-min candles** were ≥ **2.0×** as wide as normal for their clock slots (`recent_range_vs_normal`, measured by `intraday-market-sentiment` against `algo.intraday_width_profile`) | 2026-11-13 12:31 IST |

**Why it changed (agreed with Amar 2026-11-12).** The range test was the day's
range since 09:15 against the VIX-implied move × √(time elapsed). That assumes
volatility is spread evenly through the session, and it is not: the median
09:15 candle is 4.7× a midday one. At 09:45 only 8% of the session has elapsed,
so the opening range alone tripped it — the 09:45 row read volatile-expansion
on 82% of 34 sessions, and on 2026-11-12, an inside day, until 11:30. The
opening range already has its own levels (ORH/ORL); what the veto should catch
is a burst **now**.

Measured on 936 stored snapshots (2026-09-15 → 11-12), read-only:

| | Range since open (old) | Last 30 min vs normal (new) |
|---|---|---|
| Runs reading volatile-expansion | 40% | 8% |
| at 09:45 | 82% | 21% |
| each later hour | 28–62% | 2–10% |
| Confirmed sweeps passing the sweep playbook's own gate (of 7) | 1 | 4 |
| No-trend alerts reaching `sideways\|range-bound` (of 173) | 27 | 43 |

Six candles rather than three because three switched the test off and straight
back on within 30 minutes 16 times, six twice, with the same routing result;
recomputing it every 5 minutes instead of every 15 changed one alert's route in
173. 2.0 is about the top 10% of runs, what 1.5 meant for the old test.

- `range_used` is still computed and stored; it no longer votes on this frame.
  The **daily** frame keeps it as its range test (plan choice D1) — on a daily
  candle "the day's range against the VIX move" already is "the latest candle
  against normal".
- **A row without `recent_range_vs_normal` raises** before scoring (plan choice
  B2): the layer reads a missing ratio as "this frame does not use the recent
  test" and would silently fall back to `range_used`.
- Rows written before 2026-11-13 12:31 IST keep the old test's regime (not
  backfilled). 2026-11-13 itself is split: 09:45 → 12:30 on the old test, 12:45
  on the new one.
- Rolled out in two steps (plan choice C1). Step 1, 2026-11-12 21:28 IST: the
  table and columns, and the two measuring functions. Step 2, 2026-11-13
  12:31:58 IST: layer `market-classifier:3` (CodeSha256 `XyZLZzZW…`; previous
  `:2`, `JnpOUYX4…`) attached to this function only, then this function's code
  (`0+M7spXq…`; previous `PuU8qJzU…`). The layer went first: v3 without a ratio
  behaves exactly like v2, so no run could land on new code with the old layer.

Verified offline 2026-11-12, old vs new code on all 936 snapshots: every column
other than `regime`, `volatility_expanding` and `confidence` identical;
`volatility_expanding` equals the measurement on 936 of 936; `regime` on 928 —
the 8 others are rows where the replay's swing read differs from the live one
(the replay reads finished candles where the live run saw the newest still
forming), not the volatility test.

## buildup is derived here

`buildup` is the four-quadrant read of `fut_price_change_pct` against
`fut_oi_change_pct` (`LONG_BUILDUP`, `SHORT_BUILDUP`, `SHORT_COVERING`,
`LONG_UNWINDING`, or `FLAT` within the epsilon band). It moved here with the
scoring because it is a judgement label, not a measurement — the two deltas it
is built from stay on the fno row.

## The day trend (added 2026-11-08)

`day_trend` — `up` / `down` / `none` — answers one question beside the three
classification answers: *is price trading outside today's opening range, and on
which side?* Inside the range is no trend (rule G, Amar 2026-11-19). It is not an
input to `structure` or `regime`. pattern-recorder reads it to decide which patterns to
alert (plan v3, agreed with Amar 2026-11-07/08). See [trend.py](trend.py).

The rule, on the index's **closed** 5-minute candles from the 09:30 candle on:

| | on | off |
|---|---|---|
| `up` | a close above the opening-range high | a close back below it — back inside the range |
| `down` | a close below the opening-range low | a close back above it — back inside the range |

As one line: up off = ORH, down off = ORL — rule G (2026-11-19, see the notes
below). PDH / PDL no longer decide the trend; they stay levels for
pattern-recorder's alerts and the sweep playbook. The daily read is still
required (a missing or stale one raises), so a broken daily feed fails here.

- **PDH / PDL** — the daily read's `pd_high` / `pd_low` (already in the payload).
  After a weekend Muhurat session, that session is the previous one — nothing
  special-cased (Amar, 2026-11-09).
- **Opening range** — today's 09:15, 09:20 and 09:25 candles.
- **Closed candles only** — the candle still forming at the run is dropped, so
  the 10:00 run judges up to the 09:55 candle.
- **Stateless** — re-derived from 09:30 on every run.
- **Fails loudly** — a missing or stale daily read, or an incomplete opening
  range once the 09:30 candle has closed, raises before the row is written.
  `none` is always written explicitly; `NULL` means only "written before
  2026-11-09".

Verified offline 2026-11-09: equal to the v2.1 replay's day trend at 888 of 888
runs (37 sessions with a daily row), and the old and new handler write identical
values in every other column on 864 stored snapshots.

**The gap-day off line (Amar, 2026-11-11, option C).** On 2026-11-11 the whole
opening range sat above PDH, so "below the lower of PDH / ORH" meant PDH, 180
points under the range: the day stayed `up` through a 330-point fall that never
reached it. On a gap day the trend now turns off when price closes back below
the opening range (mirror for a range below PDL). `strategy-trend-pullback` uses
the same line as its stop, so "trend off" means one thing in both. Chosen on
outcomes over a replay of 80 sessions (07-01 → 11-11): 19 gap days; the line
changes the trend on 3 of them (07-24, 09-09, 11-11), where price closed back
into the gap. Verified offline 2026-11-11: old vs new rule on all 912 stored
snapshots (39 sessions) — equal on 904; the 8 that differ are 11-11 13:45 →
15:25 (`up` → `none`); none on a non-gap day, none on the other 7 gap days; the
handler writes identical values in every other column. Deployed 2026-11-11 18:53
IST (CodeSha256 `PuU8qJzU…`, previous `vNi2RmBT…`); rows written before then keep
the old rule's value (not backfilled).

**Turning on at the opening-range break (Amar, 2026-11-19, option F).** On
2026-11-19 PDH sat 135 points above the opening-range high. The range high broke
at 09:45 and the index stayed above session VWAP all day, but "above both" turned
`up` only on the 14:00 row — after 7 counter-trend short alerts from the
no-trend branch. F turns on at the range break. When PDH (PDL) lies inside or
beyond the far side of the range it is exactly option C; when PDH is above the
range (PDL below it) the day turns on at the range high (low) and off at the
range's far side. Chosen on outcomes over a full-chain replay
([trend_rules_replay.md](../pattern-recorder/replay/out/trend_rules_replay.md),
git-ignored; 86 clean sessions 07-01 → 11-19, this rule → v2.1 alerts → the trend
playbook): on for 36 of 36 hindsight trend days (27 by 10:30) against 27 (17);
trend playbook +994 points over every candidate and +803 over the first per
session against option C, ahead in both Jul–Sep and Oct–Nov. Tighter off lines
measured worse (the range high itself −701, session VWAP −813: they switch off on
normal pullbacks). **The cost, accepted:** `day_trend` is on at 85% of classifier
runs (46% under C) and in every session, so it reads "the last opening-range
break", not "a confirmed trend day". pattern-recorder therefore sends fewer
no-trend sweeps / rejections (about 90 fewer over the 86 sessions) and more breaks
/ retests, and the playbook's stops are wider (median risk 77 against 61 points).
Verified offline 2026-11-19: the new rule equals the replay's option F at all
2,328 classifier runs (98 sessions); old vs new differ at 916 runs in 68
sessions, every one a session with PDH above or PDL below the opening range; the
other five modules in the zip are byte-identical to the deployed ones. Deployed
2026-11-19 17:24:41 IST (CodeSha256 `1X/KWrz8…`, previous `0+M7spXq…`), together
with strategy-trend-pullback's matching stop; rows written before then keep the
old rule's value (not backfilled). **Replaced the same evening, before any session
ran on it** (next note).

**Trend only outside the 15-min range (Amar, 2026-11-19, rule G).** F held the
trend "on" with price back inside the opening range: 85% of classifier rows on,
82 trend trades entered inside the range over 86 sessions, 10 trend trades on the
no-trend day 2026-11-13. Amar: *"in no trend why we need to take trending trades …
why you are forcefully taking trades if the price is inside 15min candle range"*.
Rule G turns the trend off at the first close back inside the range. Full-ruleset
replay from the deployed zips
([validation-2026-11-19/](../pattern-recorder/replay/out/validation-2026-11-19/full_ruleset_report.md),
git-ignored; trend chain + sweep playbook): trend side, 86 clean sessions — 165
trades, +1556 points, profit factor 1.90, max drawdown −336 (F 273 / +3472 / 1.86
/ −1340; C 99 / +1963 / 2.09 / −976); trend + sweep, 34 sessions 09-30 → 11-19 —
+1148, PF 2.11, drawdown −210 (F +1467 / 1.69 / −1252). Known cost: a dip that
closes back inside the range turns the trend off, so a retest that reclaims the
range high is judged on the no-trend branch (2026-11-19 10:30 is not taken).
Verified offline 2026-11-19: equal to the replay's rule G at all 2,328 classifier
runs (98 sessions); it differs from F at 582 runs in 69 sessions; the other five
modules in the zip are byte-identical to the deployed ones. Deployed 2026-11-19
18:29:32 IST (CodeSha256 `0KTbE8Ht…`, previous `1X/KWrz8…` = option F), together
with strategy-trend-pullback's matching stop. First session on it: 2026-11-20.

## No option term is read yet

The chain aggregates (PCR, IV skew, straddle) sit on the fno row and are
deliberately **not** read by the classification — `structure`, `regime`,
`volatility` and `day_trend` come from price, VWAP and VIX only. Dhan serves no
historical option chain, so thresholds on those aggregates cannot be measured
until `intraday_fno_data` rows accumulate; setting them on invented numbers
would make the stored answers mean one thing before recalibration and another
after. When the rows exist the agreed shape is a separate options overlay, not
extra terms folded into the price read. (Until 2026-11-19 this section was about
the `bias` score, removed with bias that day.) See the layer README, "Revisit
once sessions have accumulated", and [[classifier-deferred-calibration]] in
project notes.

## The chain

```
EventBridge (every 15 min)
  → intraday-data-loader        commits candles
  → intraday-market-sentiment   measures → intraday_fno_data, invokes with the
                                row + today's bars + VIX baseline + run clock
  → market-classifier           classifies → intraday_sentiments, invokes ↓
  → pattern-detector            the two-clock turn rule; log-only since 2026-11-09
```

strategy-manager is no longer on this chain: since 2026-11-09 16:04 it is invoked
by `pattern-recorder` on the 5-minute poller's clock, which reads this function's
`day_trend` off `intraday_sentiments`.

**Write, then invoke.** The judgement row is written before `pattern-detector`
is invoked, so a failed invoke raises with the row already committed; the upsert
makes a retry rewrite the identical row. `UNSET MEANS OFF` — with no
`PATTERN_DETECTOR_FUNCTION_NAME` set, the row is written and nothing is
dispatched.

## IAM

Its own execution role (`market-classifier-role`), not a borrowed one:
`ssm:GetParameter` on `/algo/neon/connection` (+ `kms:Decrypt` via SSM),
`logs` on **its own** log group only, and `lambda:InvokeFunction` on
`pattern-detector`'s ARN — not a wildcard. Both console-generated roles in this
account are single-ARN scoped and fail **silently** when borrowed (no logs at
all, or a schedule that never fires). See CLAUDE.md.

`error-notifier` is subscribed to this function's log group, so a failure here
reaches Telegram.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `PATTERN_DETECTOR_FUNCTION_NAME` | *(unset)* | unset = off; set it to switch the detector leg on |
| `PATTERN_DETECTOR_INVOCATION_TYPE` | `Event` | a slow detector must not sit inside this timeout |
| `EXPECTED_MOVE_K` | `1.0` | the K in the VIX-implied expected move; same value daily uses |
| `BUILDUP_EPSILON_PCT` | `0.0` | move below which buildup counts a change as none; a tunable to set from measurement later |
