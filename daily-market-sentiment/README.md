# daily-market-sentiment

AWS Lambda function that builds NIFTY's daily read each trading morning and
pushes it to Telegram.

| | |
|---|---|
| Schedule | `cron(50 9 ? * MON-FRI *)`, `Asia/Kolkata`, flexible window off |
| Writes | `algo.candle_daily`, `algo.daily_market_sentiment` |
| Reads | `algo.instrument_master` |
| Dhan calls | 3 per run |
| Database | Neon `AI Trader APP` (`nameless-mountain-15353651`) / `Algo` / `algo` — the only one |

It does **not** populate `candle_5min` / `candle_15min` / `candle_1hr`. The
separate every-5-minute intraday function owns those. The single intraday call
here supplies today's open and the 09:15–09:30 range for the sentiment row; it
is not persisted as a series.

## What it does

1. **Daily candles** for NIFTY (`13`/`INDEX`) and INDIA VIX (`21`/`INDEX`) →
   `algo.candle_daily`. Incremental from the last stored `candle_ts`;
   300-calendar-day window on a cold start.
2. **The daily read** — regime, bias, score, confidence, the SMA/RSI inputs it
   was computed from, and the VIX-implied expected move →
   `algo.daily_market_sentiment`, one row per session.
3. **Telegram push** of that read.

## Layout

```
handler.py       entry point and orchestration
config.py        this function's tunables
params.py        the Dhan token and Telegram config
dhan.py          charts API client, and the measured facts about it
db.py            Neon access and the upserts
indicators.py    SMA / RSI / ATR, pure Python
sentiment.py     regime, score, bias, the daily read
notify.py        Telegram
```

Epoch/IST handling, `connect()` and the shared connection-string read come from
the [`neon-access`](../layers/neon-access/README.md) layer. pg8000 comes from
`neon-db-driver`. Both layers are required.

`params.py` is named `params`, not `secrets` — a `secrets.py` at the zip root
shadows the stdlib module of that name, and the zip root is first on
`sys.path`, so it shadows it for boto3 too. That was a real failure during
development, not a hypothetical.

## Measured facts about Dhan's v2 charts API

Verified against live data on **2026-09-10/11**. Each is load-bearing; getting
one wrong fails quietly, not loudly. Full detail in `dhan.py`'s docstring.

### `fromDate` is exclusive

```
fromDate 09:15:00  ->  74 candles, first stamped 09:20
fromDate 08:00:00  ->  75 candles, first stamped 09:15
```

Passing the session open as `fromDate` **silently drops the 09:15 candle** —
which *is* the opening range. All intraday fetches start at `00:00:00`, and the
handler asserts the first 15-min candle is stamped 09:15 and raises otherwise.

The same property makes incremental resume clean: passing the last stored
`candle_ts` back returns only genuinely new candles — no duplicate, no gap.

### The daily endpoint lags a session

On the evening of 2026-09-10, with the full 09-10 session present in intraday
(candles through 15:35), the newest **daily** candle was still 09-09. It
appeared overnight. So the read describes **yesterday**, and today's open must
come from intraday. Never wait for today's daily candle.

### Caps, limits, junk

- **Intraday: 90 days per call**, enforced with `HTTP 400 DH-905`. **Daily has
  no cap** — one call returned 5,129 candles back to 2006 — so the daily path
  needs no chunking however stale the last stored candle is.
- **Rate limits are tighter than the documented 5/s.** Six unpaced calls earned
  `DH-904` and stayed throttled; 4 s spacing runs clean.
- **Stray post-close candles.** 2026-09-10 carried a 19:20 candle, zero volume,
  echoing the close. `session_candles()` filters to 09:15–15:30.
- No `{"status","data"}` envelope; `timestamp` is true UTC epoch seconds at the
  **start** of the candle; `volume` arrives as a float into a `bigint` column.

### `interval: 15` is correctly aligned

Worth stating because the `nifty-chart` and `market-regime` skills both assert
the opposite and aggregate three 5-min candles instead:

```
5-min 09:15 / 09:20 / 09:25 aggregated ->  H 23536.50  L 23477.55
interval:15, first bucket, stamped 09:15 -> H 23536.50  L 23477.55
```

Identical. The "misaligned buckets" belief is the exclusive `fromDate`
misdiagnosed. Relatedly, those skills' "5 candles per call" limit is the **MCP
connector's** formatting, not the API — REST returned 3,958 candles in one call.

## `security_id` alone is not unique

`resolve_instrument()` takes `(security_id, instrument_type)` and raises unless
exactly one row matches.

This is not defensive coding. On 2026-09-11 an earlier version queried on
`security_id` alone and took `fetchone()`. `security_id 13` is **both** NIFTY
(`IDX_I`/`INDEX`) **and** ABB (`NSE_EQ`/`EQUITY`); it resolved to ABB and wrote
204 ABB candles plus an ABB sentiment row labelled NIFTY, then sent that to
Telegram. **19 security_ids in `instrument_master` collide across instrument
types**, so this was systematic, not bad luck.

`fetchall()` plus an exactly-one check means a future collision raises rather
than picking whichever row Postgres returns first.

## `expected_move` comes from VIX, not ATR

```
expected_move    = price × (VIX / 100) / √252 × K      # K = EXPECTED_MOVE_K, default 1.0
upper_volatility = price + expected_move
lower_volatility = price − expected_move
```

This replaces legacy's `atr_14 × {TREND .35, TRANSITION .25, RANGE .18}`, which
**measured 5.7% coverage over 944 sessions** — the day left that band 94% of the
time, mean band 61 pts against a mean actual move of 164 pts.

Measured alternatives, on a held-out test set of 284 sessions (fitted on the
preceding 660):

| | Coverage | Corr. with actual move | Test MAE |
|---|---|---|---|
| legacy ATR × regime factor | 5.7% | — | — |
| ATR-14 raw | 81.2% | 0.365 | 66.8 |
| ADR-10 raw | 74.8% | 0.409 | 62.6 |
| **VIX-implied** | **73.5%** | **0.410** | **61.7** |
| best VIX/realised/gap blend | — | 0.448 | 62.5 |

No blend of VIX with realised excursion or the overnight gap beat plain VIX on
error. **Do not add terms back without numbers that beat MAE 61.7.**

`K` picks coverage: `0.71 → 50%`, `1.00 → 73%`, `1.10 → 82%`, `1.42 → 93%`.
1.0 is the unscaled textbook 1-sigma daily move, so it means something standard
rather than being a fitted constant.

## The sma200 guard

`detect_market_regime` compares against `sma200`. With fewer than 200 stored
candles that value is absent, every comparison with `None`/NaN is false,
`bull_stack` and `bear_stack` both go false, and legacy **returned `TRANSITION`
without raising** — a plausible regime built on a missing indicator.

`build_daily_sentiment()` raises before classifying, and
`daily_market_sentiment.sma200` is `NOT NULL` so the schema enforces it too.

The 300-day cold start measured to **204 candles** — four clear of 200. Thin by
design: the cold start happens once and the table only grows afterwards. If the
first run raises `sma200 unavailable with 19X candles`, set `COLD_START_DAYS` to
400.

## The score is not a forecast — measured

Over 933 sessions (2022–2026), the sentiment score's correlation with forward
return:

```
 1-day   r = -0.020        10-day mean forward return by bias:
 5-day   r = -0.057            BULLISH  +0.086%
10-day   r = -0.078            NEUTRAL  +0.304%
                               BEARISH  +0.886%
```

**The sign is inverted and the magnitude is noise.** BEARISH days are followed
by *higher* returns than BULLISH ones — a −0.92σ spread in the wrong direction
at 10 days. An RSI control over the same period shows the same sign, which
explains it: RSI dominates the score and NIFTY mean-reverted over this window.

Caveats: r ≈ 0.02–0.08 is near-noise either way, n=52 for BEARISH is small, and
2022–26 is one market character. This says the signal is weak, not that it is
reliably invertible.

**Treat `regime`/`bias` as a description of structure, not a prediction** — the
same position `market-regime` takes: *"a regime is a description of what has
already happened, not a forecast."* Do not size a trade off this column.

Two related findings from the same measurement:

- `atr_floor = 0.004` in `detect_market_regime` has **never fired**. NIFTY's
  `atr_14/close` has a floor around 0.72% (p5); 0.40% is below p0, so
  `compressed` was False on all 943 sessions. It is an inherited constant that
  does nothing.
- Volatility, unlike direction, **is** forecastable. `atr_14/close` terciles
  predict next-day realised range monotonically — 0.728% / 0.911% / 1.123%, a
  1.54× spread. VIX/20-day-mean terciles work too but separate less well
  (1.30×).

## Porting notes

Formulas are ported formula-for-formula from `trading-algo/helpers/` —
`detect_market_regime`, `calculate_sentiment`, `sentiment_bias`,
`calculate_confidence`, Wilder RSI/ATR — with four deliberate exceptions.

1. **The `df.iloc[:-1]` drop is gone.** Legacy dropped the newest row because it
   ran intraday and that row was today's forming candle. The daily endpoint lags
   a session, so at 09:50 the newest stored row is already closed; dropping it
   would compute every level from the day *before* yesterday.
2. **VIX is real.** Legacy did `vix = last_closed.get("vix", 12.0)` and nothing
   in that path ever populated a `vix` column, so it was **always 12.0**. That
   value fed `calculate_confidence`. INDIA VIX is now fetched and stored like
   any other instrument — 4,612 daily candles back to 2008, real OHLC, genuinely
   zero volume.
3. **`price` comes from intraday**, since today's daily candle does not exist.
4. **`expected_move` is VIX-implied** — see above.

The six-label taxonomy (`bias` + `structure` combined into `regime`) is also
new; legacy stored `TREND`/`TRANSITION`/`RANGE` and the bias separately. Those
are the same information taken apart.

## Configuration

| Environment variable | Required | Default |
|---|---|---|
| `NEON_CONNECTION_STRING` | no | *unset* — read from `/algo/neon/connection`; set only to override |
| `TOKEN_PARAMETER_NAME` | no | `/algo/dhan/token` |
| `TELEGRAM_PARAMETER_NAME` | no | `/algo/telegram/brief` |
| `NIFTY_SECURITY_ID` / `NIFTY_INSTRUMENT_TYPE` | no | `13` / `INDEX` |
| `VIX_SECURITY_ID` / `VIX_INSTRUMENT_TYPE` | no | `21` / `INDEX` |
| `COLD_START_DAYS` | no | `300` |
| `EXPECTED_MOVE_K` | no | `1.0` |
| `API_PACING_SECONDS` | no | `4.0` |
| `UPSERT_BATCH_SIZE` | no | `5000` |

No credentials in environment variables. `/algo/telegram/brief` is a
`SecureString` holding `{"bot_token": "...", "chat_id": "..."}`.

## IAM

| Action | Resource |
|---|---|
| `ssm:GetParameter` | `/algo/dhan/token`, `/algo/telegram/brief`, `/algo/neon/connection` |
| `kms:Decrypt` | `Resource: "*"` with `kms:ViaService = ssm.<region>.amazonaws.com` |
| `logs:*` | `AWSLambdaBasicExecutionRole` |

The parameter names carry a leading slash but the ARN does **not** double it —
`parameter/algo/dhan/token`.

## Deployment

Zip upload, handler `handler.lambda_handler`, Python 3.14, memory 256 MB.
Layers, both required:

```
arn:aws:lambda:ap-south-1:709458364771:layer:neon-db-driver:1
arn:aws:lambda:ap-south-1:709458364771:layer:neon-access:1
```

**Attach the layers before uploading the code** — `handler.py` imports
`neon_access` at module load, so the reverse order gives
`Unable to import module 'handler': No module named 'neon_access'`.

Set the handler under **Code → Runtime settings → Edit**, not Configuration →
General. Upload a zip rather than pasting into the console editor.

**Timeout: currently 60 s, and that is tight.** A run takes ~12 s, but the Dhan
retry backoff is `4 × 2^(attempt+1)` = 8 + 16 + 32 = **56 s of sleeping on one
rate-limited call**. A single `DH-904` kills the run. 300 s is the safe value.

Test with `{"skip_telegram": true}` first — the brief goes to CloudWatch instead
of Telegram. `{"trade_date": "YYYY-MM-DD"}` overrides the session date.

## Local verification

No test suite. Put the layer on `sys.path` the way Lambda mounts it, stub the
runtime-supplied packages, and the whole pure path is exercisable:

```python
import sys, types
sys.path.insert(0, "layers/neon-access/python")
sys.path.insert(0, "daily-market-sentiment")
stub = types.ModuleType("pg8000"); stub.dbapi = types.ModuleType("pg8000.dbapi")
sys.modules["pg8000"] = stub; sys.modules["pg8000.dbapi"] = stub.dbapi
b3 = types.ModuleType("boto3"); b3.client = lambda *a, **k: None
sys.modules["boto3"] = b3
```

Covered before deploy:

- `ist_midnight_epoch` verifies `(ts + 19800) % 86400 == 0` and raises otherwise
- `rolling_mean` warmup positions are `None`; `wilder_rsi` returns 50.0 flat and
  100.0 on a monotonic rise
- `session_candles` drops 76 → 75, removing the 19:20 stray
- the first 15-min candle is stamped 09:15
- `resolve_instrument("13", "INDEX")` returns NIFTY against a master containing
  both NIFTY and ABB under that id; a 0-row or 2-row match raises
- `SENTIMENT_FIELDS` count equals the placeholder count, and the insert's column
  list matches `schema.sql` in **both** directions
- the `sma200` guard raises at 84 candles rather than emitting a regime

## First live run — 2026-09-11

```
resolved 13/INDEX -> NIFTY on IDX_I
NIFTY: cold start 300d from 2025-11-15 -> 204 fetched, 204 written
INDIA VIX: incremental from 2026-09-10 -> 1 fetched, 1 written
done in 12.5s: NIFTY, 206 rows, 3 api calls, regime=transitional
```

Cross-checked: `pd_high/low/close` matched 2026-09-10's official quote OHLC
exactly; `price 23270.30` matched that morning's open; `min15` 23231.40–23277.30
matched the 09:15 candle; `expected_move 172.98` = 23270.30 × 11.8/100 ÷ √252.
