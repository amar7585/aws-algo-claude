# market-classifier

← [Back to root README](../README.md) · [Architecture](../docs/architecture.md) · [Flow charts](../docs/flow.md) · [Components](../docs/components.md)

Turns one **measurement** snapshot into **judgement**: reads the
`algo.intraday_fno_data` row `intraday-market-sentiment` just wrote, scores it
with the shared [`market-classifier` layer](../layers/market-classifier/README.md),
derives the futures-OI buildup, writes `algo.intraday_sentiments`, and invokes
`pattern-detector`.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package, 5 modules |
| Layers | `neon-db-driver` + `neon-access` + `market-classifier` (v2, the scoring rules) |
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
- **this function judges** — regime, structure, bias, buildup and the
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
  VIX), `day_range`, the VIX-implied `expected_move`, and `session_elapsed` from
  the **run clock** (at a 09:45 run the bar stamped 09:45 has zero elapsed time,
  so 30 minutes have passed, not 35).

## buildup is derived here

`buildup` is the four-quadrant read of `fut_price_change_pct` against
`fut_oi_change_pct` (`LONG_BUILDUP`, `SHORT_BUILDUP`, `SHORT_COVERING`,
`LONG_UNWINDING`, or `FLAT` within the epsilon band). It moved here with the
scoring because it is a judgement label, not a measurement — the two deltas it
is built from stay on the fno row.

## No option term is scored yet

The chain aggregates (PCR, IV skew, straddle) sit on the fno row and are
deliberately **not** fed to the score. Dhan serves no historical option chain,
so those thresholds cannot be measured until `intraday_fno_data` rows
accumulate; scoring them on invented numbers would make `bias` mean one thing
before recalibration and another after. When the rows exist the agreed shape is
a separate options overlay, not extra terms folded into `score`. See the layer
README, "Revisit once sessions have accumulated", and
[[classifier-deferred-calibration]] in project notes.

## The chain

```
EventBridge (every 15 min)
  → intraday-data-loader        commits candles
  → intraday-market-sentiment   measures → intraday_fno_data, invokes with the
                                row + today's bars + VIX baseline + run clock
  → market-classifier           scores → intraday_sentiments, invokes ↓
  → pattern-detector            the two-clock turn rule; on a turn, invokes ↓
  → strategy-manager            routes on regime|bias
```

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
