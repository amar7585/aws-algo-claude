# market-classifier layer

← [Back to root README](../../README.md) · [Architecture](../../docs/architecture.md)

One classification, used by `daily-market-sentiment` and the `market-classifier`
function.

```python
from market_classifier import classify, decorate, read_structure

decorate(candles)                       # sma9/50/100/200, rsi, atr, vol_avg
structure = read_structure(todays_bars, "5min")
result = classify(frame="5min", bar=candles[-1], structure=structure,
                  vix=..., vix_baseline=..., day_range=...,
                  expected_move=..., vwap=..., session_elapsed=...)
```

`classify()` **scores from scalars + a precomputed structure read** — the
caller owns `decorate()` and `read_structure()`, so the scorer needs only the
newest bar's numbers and the swing result. That is what lets the
`market-classifier` function score straight off a stored `intraday_fno_data`
row without re-fetching bars, while `daily-market-sentiment` runs the identical
`classify()` on daily candles (`frame="daily"`). Same rules, same score scale,
so a daily row and an intraday row can be read against each other.

Pure Python, stdlib only. No dependency of its own.

## Why it is a layer and not two copies

`daily-market-sentiment/indicators.py` and `strategy-manager/indicators.py`
held byte-identical copies of `rolling_mean`, `wilder_rsi` and `wilder_atr`,
and the copy was deliberate — the reasoning is still in the latter file:
sharing meant the `neon-access` layer, where every change costs a republish
and an ARN repoint on every function.

That trade held while the two functions ran **different** classifications. It
stops holding the moment they must produce the **same** one: two copies of the
inputs to a single agreed rule set is exactly the drift the single rule set
exists to prevent.

## What it returns

Three separate answers, not one label:

| Field | Values | From |
|---|---|---|
| `bias` | bullish / bearish / range-bound | the score, against `BIAS_THRESHOLD` |
| `structure` | trending / sideways / transitional | the swing read alone |
| `regime` | trending / sideways / volatile-expansion | the swing read **after** volatility has a veto |
| `volatility` | low / normal / high | INDIA VIX against its bands |

`structure` and `regime` share two of their three values and are still not the
same question. On a day whose range is expanding, the structure underneath is
the thing being disrupted — so `regime` says `volatile-expansion` while
`structure` keeps reporting what it actually saw.

### The score

Four terms, ±4, plus a fifth worth ±1 on the intraday frame only.

| Term | Range | Test |
|---|---|---|
| alignment | ±2 | `close > sma9 > sma50 > sma100 > sma200` full, fast pair alone ±1 |
| momentum | ±1 | RSI against `RSI_BULLISH` / `RSI_BEARISH` |
| swing | ±1 | the swing read's **direction**, not its label |
| vwap | ±1 | price against session VWAP — **intraday only** |

`max_score` moves with the VWAP term rather than the daily frame being scored
out of a total it can never reach.

### Structure

A bar is a swing high when its high is the highest of the `SWING_WINDOW` bars
either side of it — a (2k+1) fractal. Two consequences, both intended:

- **The last k bars can never be a swing.** Structure describes price up to k
  bars ago and cannot flip on an unconfirmed wick.
- **Early in a session there is nothing to say.** The first run of the day has
  ten 5-minute bars and yields at most one swing of each kind, so the read is
  `determined: False` and reports `sideways` rather than inventing a trend.

## Thresholds

Every number lives in `thresholds.py` and is read from the environment, with an
optional per-frame override:

```
CLASSIFIER_<NAME>_<FRAME>    e.g. CLASSIFIER_RSI_BULLISH_DAILY
CLASSIFIER_<NAME>            e.g. CLASSIFIER_RSI_BULLISH
```

So re-tuning is a configuration change on two Lambdas, not a layer republish
and an ARN repoint on both. An unknown name raises rather than returning None —
a typo would otherwise disable a test silently.

**Most of them are unmeasured starting values.** The agreed sequence is: build
the shape, collect sessions, then fit. Two are not guesses, and both were
corrected after being measured against live stored data on 2026-09-13 — see
the notes on them in `thresholds.py`:

| Threshold | Was | Is | Why |
|---|---|---|---|
| `STRUCTURE_LOOKBACK_DAILY` | 10 | **40** | over the 204 real daily bars, a k=2 fractal finds two swings of each kind only at 40 bars; 10, 15, 20, 25 and 30 all leave structure permanently undetermined |
| `RANGE_EXPANSION_MULTIPLE` | 1.0 | **1.5** | over 203 sessions, full-day range ÷ expected move has median 0.95 and p90 1.59, so 1.0 calls 44.8% of ordinary days an expansion; 1.5 keeps 11.8% |

## The range-expansion test is time-scaled

`expected_move` is a whole-session figure; the day's range at 10:00 covers 45
minutes. Compared raw, the ratio climbs mechanically through the session — the
test trips far more easily at 15:15 than at 10:00, and the regime drifts into
`volatile-expansion` every afternoon regardless of the market. No threshold
fixes that; it only moves when the drift crosses the line.

Volatility scales with the square root of time, so the range is measured
against `expected_move × √f`, where `f` is the fraction of the session elapsed.
Callers pass `session_elapsed`; the daily frame passes `1.0`.

Measured on the real 2026-09-11 session:

| | 09:55 | 11:15 | 12:35 | 13:55 | 15:25 |
|---|---|---|---|---|---|
| raw ratio | 0.27 | 0.70 | 0.75 | 0.92 | 1.25 |
| **scaled** | **0.77** | **1.22** | **1.01** | **1.05** | **1.25** |

The raw row climbs monotonically — it is measuring the clock. The scaled row
has no trend in the hour.

The scaling is sensitive when almost no session has elapsed: the 09:15 bar
alone reads 2.30×. The first run of the day is 10:00, where `f` is 0.13 and the
reading is 0.77, so that region is never sampled in production.

## Revisit once sessions have accumulated

Three decisions were deliberately deferred on 2026-09-13 because the data to
settle them does not exist yet. They are not oversights, and none of them
should be closed by argument — wait for the rows.

**1. The RSI weight.** ±1 at 60/40. Measured over the real 5-minute series, the
term is non-zero on 33.6% of bars and changes the resulting `bias` on **6.9%**
— about one bar in fourteen, which is not the heavy influence it looks like on
paper. But that sample is two sessions, and the daily frame could not be
measured at all: only 5 of the 204 stored daily bars have an `sma200`, because
it needs 200. Re-run the measurement once there are a few hundred daily bars
and a few weeks of 5-minute ones, then decide between 60/40, 70/30, and
dropping the term.

**2. The option-chain overlay — not built.** `intraday_fno_data` and
`option_chain_snapshot` were both **empty** when this layer was written, and
Dhan serves only a *live* option chain with no historical endpoint, so option
history can only accumulate forward from the first live run. Nothing
option-based can be back-measured. Folding PCR, IV skew or straddle terms in
now would mean inventing thresholds, and every row written before they were
recalibrated would carry a `bias` meaning something different from the rows
after — which is the exact incomparability this layer exists to end.

When the rows exist, add it as a **price core + options overlay**: the core
keeps producing `bias`/`structure`/`regime` on identical rules across both
frames, and the option data produces its own `options_bias` stored beside the
core rather than inside it. Fields available on the snapshot row: `buildup`,
`pcr_oi`, `pcr_volume`, `straddle_pct`, `ce/pe_oi_change_pct`, `max_oi_call`
and `max_oi_put` (the walls), `max_pain`, `iv_skew`, `basis_pct`.

One swap is worth making at the same time: `near_straddle_pct` is the market's
own implied move for the session and is a more direct input to the
range-expansion test than the VIX-derived expected move it uses today.

**3. The routing key.** `STRATEGY_REGISTRY` in `strategy-manager` is keyed on a
`regime|bias` combination, but only one cell is filled and it is a placeholder.
Which combinations run which playbook is a decision to take against observed
sessions, not to infer from the taxonomy.

## Verification

There is no test suite. The layer is pure and takes plain dicts, so it runs
locally against real stored candles with no credentials and no driver:

```python
import sys
sys.path.insert(0, "layers/market-classifier/python")
from market_classifier import classify, decorate
decorate(bars)                      # bars: [{"ts","open","high","low","close","volume"}, ...]
print(classify(bars, frame="daily", structure_candles=bars[-40:], vix=11.8))
```

Pull the bars straight out of Neon (project `nameless-mountain-15353651`,
database `Algo`) rather than inventing a fixture — a synthetic fixture has
already hidden one live-data bug in this repo, and it is what hid the
`STRUCTURE_LOOKBACK_DAILY` problem above until it was run on the real series.
