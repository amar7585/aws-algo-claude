# daily-market-sentiment

AWS Lambda function that builds NIFTY's daily read each trading morning and
pushes it to Telegram.

| | |
|---|---|
| Schedule | `cron(35 9 ? * MON-FRI *)`, `Asia/Kolkata`, flexible window off; **and** (step 2, 2026-11-23) the evening brief: a second schedule `cron(45 15 ? * MON-FRI *)` with the input `{"brief": "evening"}` - see [below](#the-evening-brief-2026-11-23) |
| Writes | `algo.candle_daily`, `algo.daily_market_sentiment`, `algo.intraday_width_profile` (since 2026-11-12) |
| Reads | `algo.instrument_master`; `algo.fno_5m_snapshot` (the previous session's last row, for the morning brief - read only) |
| Dhan calls | 7 per run: 4 for the daily read and width profile, 3 for the morning brief (expiry list, option chain, VIX 5-min) |
| Evening brief | `EVENING_BRIEF=on` sends it, unset = shadow (built and logged only). READS ONLY: `candle_5min`, `intraday_sentiments`, `intraday_fno_data` (a count), `fno_5m_snapshot`, `pattern_events`, `position_master`, `candle_daily`; 3 Dhan calls (VIX 5-min, expiry list, option chain). **LIVE since 2026-11-23 10:59:41 IST** (CodeSha256 `z7gutlGe...`, `EVENING_BRIEF=on`; schedule `daily-market-sentiment-evening` from 11-23 15:45); rollback = version 1 (`y5ZjO+zC...`) |
| Morning brief | **LIVE: `MORNING_BRIEF_V2=on`** since 2026-11-22 12:51 IST (Amar: "deploy live directly") - the new brief is the 09:35 message; first run Mon 2026-11-23. Unset = shadow (old message sent, new brief logged). See [below](#the-morning-brief-2026-11-22) |
| Database | Neon `AI Trader APP` (`nameless-mountain-15353651`) / `Algo` / `algo` — the only one |

It does **not** populate `candle_5min` / `candle_15min` / `candle_1hr`. The
separate every-5-minute intraday function owns those. The single intraday call
here supplies today's open and the 09:15–09:30 range for the sentiment row; it
is not persisted as a series.

## What it does

1. **Daily candles** for NIFTY (`13`/`INDEX`) and INDIA VIX (`21`/`INDEX`) →
   `algo.candle_daily`. Incremental from the last stored `candle_ts`;
   300-calendar-day window on a cold start.
2. **The daily read** — regime, structure, the SMA/RSI inputs it
   was computed from, and the VIX-implied expected move →
   `algo.daily_market_sentiment`, one row per session.
3. **Telegram push** at 09:35: the old daily-read message, or - with `MORNING_BRIEF_V2=on` - the
   [morning brief](#the-morning-brief-2026-11-22).
4. **The width profile for today's session** → `algo.intraday_width_profile`,
   75 rows: the median 5-min candle width, (high − low) / close × 100, at each
   clock slot 09:15 … 15:25 over the previous 20 weekday sessions. One Dhan
   call for 45 days of 5-min bars. Runs **last**, so a failure never costs the
   daily read or the brief, and it still fails the run so error-notifier reports
   it before the first intraday run at 09:45 needs it. See below.

## The morning brief (2026-11-22)

Amar, 2026-11-22: **the 09:35 message IS the morning brief**, and this function sends it (and,
next, the evening brief). The plan and its decisions are in
`pattern-recorder/replay/out/alert-flow-plan-2026-11-22/` (git-ignored): `plan.md`,
`step1-morning-brief.md`.

| section | from | what it says |
|---|---|---|
| GAP | the daily candles + this run's 15-min call | the gap from the previous close; the first 15-min candle's colour, how much of the gap it took back, whether it filled it; the 15-min range against ADR10; where price stands; **now** (the chain's spot) |
| OPENING READ | the 15-min candle + the daily read | gap direction, who held the first 15 min, the daily regime / structure / swing. Not a trend call: day_trend first exists at the 09:45 run - the line says which 5-min close turns it on (rule G) |
| LEVELS | `candle_daily`, **every stored session** | PDH / PDC / PDL; the nearest "open" daily highs and lows (no later session traded through them - no fixed lookback); those the first 15 min took; the daily read's swing; the nearest unfilled daily FVG either side (three-candle gap, unfilled until a later day trades fully through it) |
| PAIN POINTS | **today's chain, read live** (not stored) | max pain, call wall and put wall **with the OI at each**, ATM, PCR - over ATM +-20 strikes with `chain.py`, a verbatim copy of intraday-fno-poller's, so the same numbers the poller stores; yesterday's close beside it (`fno_5m_snapshot`); CE / PE OI change since then (same expiry only); the straddle's expected move; the weekly expiry, EXPIRY TODAY |
| VIX | **live**: the latest closed VIX 5-min candle | the move vs the previous official close (`candle_daily` VIX), and how many of the last 20 sessions' daily moves it beats - UNUSUAL when it beats all of them (no fixed band, Amar 2026-11-22). Plus the VIX-implied move from the daily read |
| ADR10 | `candle_daily` | the mean high - low of the last 10 sessions |

No round-number levels (Amar). No futures OI in the morning: it needs another call and a contract
lookup, and in expiry week the roll distorts it (-22% on 11-20) - it goes in the evening brief.

**ALWAYS SENT (Amar 2026-11-22).** A section whose input is missing becomes a line
`<SECTION>: missing - <reason>`; it is never dropped silently. The gap is then **logged at ERROR**,
which error-notifier's subscription filter reports - it is **not raised**: Lambda's async retries
(2, plus the schedule's own 2) would re-run the whole function and send the brief again (in shadow
mode, the OLD message again). A failure of the daily read itself still raises as before; with the
brief live it first sends one line saying the daily read FAILED. **A bug in the brief itself** falls back
to the old daily-read message (logged at ERROR) and the run carries on to the width profile - raising
would retry into the same bug and never write the profile the 09:45 runs need.

**Deployed LIVE 2026-11-22** (Amar: no shadow day - "deploy live directly, if we get any issue we will
fix on the fly, we will monitor periodically"):
- code 12:51:02 IST, CodeSha256 `y5ZjO+zCPePh3IG17tqEN3y//Kk8PMlk0J7+WNJfqgE=` (previous `ONNju0RJ...`,
  the zip at commit c6ab155);
- configuration 12:51:15 IST: timeout 90 -> **300 s**, `MORNING_BRIEF_V2=on` (the function had no env vars);
- smoke invoke 12:51:34 IST on Sunday: HTTP 200, the new modules import with the layers, the weekend
  gate answered `skipped_non_trading_day` - no Dhan, database or Telegram touched. The live Dhan
  chain / VIX reads are first exercised at Mon 2026-11-23 09:35 (no valid token on a Sunday).

**Rollback, cheapest first:** unset `MORNING_BRIEF_V2` (the old 09:35 message, no deploy; the new
brief is then built after the width profile and only logged); or redeploy the c6ab155 zip
(`ONNju0RJ...`). Keep the 300 s timeout either way.

**Checked offline (2026-11-22)**, `check_step1.py` in the plan folder: the brief rebuilt for all 46
stored sessions with a daily read (09-15 .. 11-20) never raises, is ASCII, and its open, 15-min range,
previous close and gap equal the stored daily read; the only missing section is PAIN POINTS on the 24
sessions before the poller existed (no stored stand-in). The real handler, with Dhan / Neon / Telegram
faked: shadow sends the old message once and logs the new one; a chain failure or a bug in the brief
is logged at ERROR and raises nothing; live sends the new brief once, with missing lines marked; a
bug in the brief (live) sends the old message once, still writes the width profile, raises nothing; a
daily-read failure sends one failure line then raises (live) or sends nothing and raises (shadow, as
before); `skip_telegram` sends nothing; a width-profile failure after the brief sends no second
message. `chain.py` is byte-identical to the poller's.

## The evening brief (2026-11-23)

Amar, 2026-11-23: step 2 of the phone flow, "Evening brief around 15:45 (same function, second schedule)".
The plan, the mock-up it follows and the checks are in the plan folder (`step2-evening-brief.md`,
`mock_evening_1120.txt`, `check_step2.py`). The schedule invokes this function with
`{"brief": "evening"}`; **none of the 09:35 work runs** - the evening run never writes (no candle, no
daily read, no width profile) and the brief itself is not stored (every number is re-derivable).
`evening.py` composes it, every line pure; the handler only reads.

| section | from | what it says |
|---|---|---|
| CLOSE | `candle_5min` (today) + `candle_daily` | the official close (the 15:25 auction candle) vs PDC; the 15:15 price (the 15:10 candle, the last continuously traded one); the day range vs ADR10; the open; the 15-min range and where the close sits against it. **No 15:25 candle stored yet: "CLOSE not final"** (and the gap is logged at ERROR) |
| VIX | **live**: the latest closed VIX 5-min candle | vs the previous official close (`candle_daily` VIX). The official close of today is not available at 15:45 - the daily endpoint lags a session |
| LEVELS FILLED TODAY | `candle_daily`, every stored session | the open daily highs / lows (the morning brief's definition) that today traded through; a daily FVG formed today; FVGs filled today |
| OPTION CHAIN | the first stored 5-min snapshot -> **the chain read live** | ATM, call wall and put wall **with the OI at each**, max pain, PCR on the current weekly expiry (EXPIRY TODAY). If the read fails: the last stored snapshot, marked, and logged at ERROR |
| THE DAY'S READ | `intraday_sentiments` | day_trend through the day's 15-min rows |
| ALERTS / TRADES | `pattern_events`, `position_master` | the alerts sent (and how many reached the playbooks); the day's paper positions with lot-points (P2a: the sweep). Trend-playbook trades are recorded from P2b |
| NEXT SESSION | the day + the expiry list | PDH / PDL / PDC for tomorrow, and the next weekly expiry |
| SYSTEM | counts | 5-min candles, 15-min rows (+ option rows), 5-min option snapshots stored today |

**The same failure rules as the morning brief:** a missing input becomes a marked line and is logged
at ERROR, never raised (a raise would make the async retries re-send the brief); a bug in the brief
sends one failure line, logged at ERROR, no raise; a run that fails before its brief (connect, token)
sends one failure line, then raises. **A day with no stored 5-min candle and no 15-min row is a
holiday: nothing is sent** (`skipped_no_session`). Any other `brief` value raises.

**Checked offline (2026-11-23)**, `check_step2.py`: the brief rebuilt for all 23 stored sessions with
5-min snapshots (from 10-08) has every section and nothing missing, is ASCII, and its close / PDC /
15:15 price / day range equal the stored candles; 11-20 equals the mock-up's numbers (its LEVELS line
lists more filled highs, because the open levels use every stored session, as the morning brief does).
The real handler with Dhan / Neon / Telegram faked: live sends once, shadow only logs; a chain, VIX or
table failure costs only its own section and is logged at ERROR; no 15:25 candle gives "CLOSE not
final"; a bug sends one failure line; a connect failure sends one line then raises; a holiday sends
nothing; the evening run calls no writer and no 09:35 step; 3 Dhan calls. `check_step1.py` (the morning
path) still ALL PASS, with its brief texts byte-identical.

**Deployed 2026-11-23** (Amar: "Deploy it"):
- version 1 published first = the morning-brief code `y5ZjO+zCPePh3IG17tqEN3y//Kk8PMlk0J7+WNJfqgE=` (the rollback);
- code 10:59:41 IST, CodeSha256 `z7gutlGeVM2tulGRunoypuJHK9SDwqu09DDDsNzfpqE=`; configuration `MORNING_BRIEF_V2=on`,
  `EVENING_BRIEF=on` (timeout 300 s, layers unchanged);
- smoke 11:00 IST, `{"brief": "evening", "skip_telegram": true}` on the live session: HTTP 200, 11.4 s, 3 Dhan calls,
  101 MB; the brief built from live data, "CLOSE not final" mid-session as designed (logged at ERROR once);
- schedule `daily-market-sentiment-evening`: `cron(45 15 ? * MON-FRI *)`, `Asia/Calcutta`, flexible window off,
  input `{"brief": "evening"}`, retry 2 / 3600 s, role `Amazon_EventBridge_Scheduler_LAMBDA_7a49de2026` - the
  09:35 schedule's, scoped to this function only (no other function's role is used).

**Rollback, cheapest first:** unset `EVENING_BRIEF` (built and logged only), or disable the schedule; or
redeploy version 1's code.

**In the holiday gate since 2026-11-23 night** (Amar: "Add to holiday gate"). The evening schedule is the
eighth name in [`auth-dhan-broker`](../auth-dhan-broker/README.md)'s `MANAGED_SCHEDULE_NAMES` (and its ARN in that
role's `holiday-gate-access` policy), so on a holiday it is disabled with the other session schedules and
re-enabled on the next trading morning. Without it, it fired on a holiday: the run reads the Dhan token
**before** its own no-session check, and on a holiday no token is minted, so it failed on the expired token
and sent a "run FAILED" line (up to three, with Lambda's async retries) - found before the first such day,
2026-11-24. Its own `skipped_no_session` check stays as the second guard (a calendar nobody reseeded).

**First live day, 2026-11-23: both briefs validated** (read-only, `alert-flow-plan-2026-11-22/validation-2026-11-23.md`):
the morning brief delivered 09:35:47 (7 Dhan calls, 27.5 s, nothing missing, no rate-limit retry; its open,
15-min range and previous close equal the daily read), the evening brief delivered 15:45:44 (3 calls, 13.1 s,
nothing missing). The one ERROR line that day was the 11:00 manual evening test ("CLOSE not final" mid-session,
as designed). The sent texts are not logged, so their wording is compared on the phone.

## The width profile (2026-11-12)

`intraday-market-sentiment` judges its last six closed 5-min candles against
this table on every 15-minute run, and `market-classifier`'s volatility test
reads the ratio (agreed with Amar 2026-11-12: plan choice A2 put the build
here, at 09:35, ten minutes before it is first needed). See
[width_profile.py](width_profile.py) and the
[market-classifier README](../market-classifier/README.md#volatility).

- **Per clock slot**, because the day is not flat: over 103 sessions
  (2026-06-15 → 11-11) the median 09:15 candle was 4.7× a midday one,
  09:20–09:35 1.6–1.9×, 15:00 1.5×.
- **The last 20 weekday sessions before today.** Today is never in it. The
  2026-11-08 Muhurat Sunday is not a normal session and is left out. A normal
  from every session since June sat below this autumn's market (the median run
  read 1.45× it); from the last 20 it reads 1.2×.
- **Fails loudly**: fewer than 20 sessions in the fetched window, or a slot that
  fewer than 15 of them have a candle at, raises.
- **Frozen once written**: `slot_ts` is the slot on that session (that day's
  `candle_ts`), so every stored snapshot can be replayed against exactly the
  yardstick it was judged by, even if candle history is later refilled.

Verified offline 2026-11-12: the builder equals the measurement's normal on all
75 slots of all 40 sessions 09-15 → 11-12.

## Layout

```
handler.py       entry point and orchestration
brief.py         the morning brief, every line pure (2026-11-22)
chain.py         option-chain aggregates - VERBATIM COPY of intraday-fno-poller/chain.py
config.py        this function's tunables
params.py        the Dhan token and Telegram config
dhan.py          charts API client, and the measured facts about it
db.py            Neon access and the upserts
indicators.py    SMA / RSI / ATR, pure Python
sentiment.py     regime, structure, the daily read
width_profile.py the normal 5-min width per clock slot, pure
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

### `fromDate` keeps only what is stamped after it

```
fromDate 09:15:00  ->  74 candles, first stamped 09:20
fromDate 08:00:00  ->  75 candles, first stamped 09:15
```

Those counts are what a past session returns. On the **current** day the bar
at `fromDate` is not dropped but shortened: Dhan applies the rule to 1-minute
data before bucketing, so the bucket at `fromDate` comes back missing its first
minute (measured
2026-10-19 — see
[intraday-data-loader](../intraday-data-loader/README.md#the-window-start-bug),
where a boundary `fromDate` corrupted a third of the stored bars). Either way,
passing the session open as `fromDate` loses the opening print — the opening
range. All intraday fetches therefore start at `00:00:00`, where nothing is
stamped. The handler also asserts the first 15-min candle is stamped 09:15 and
raises otherwise; that catches the dropped bar, **not** the shortened one, so
the 00:00 start is what actually protects the opening range.

On the daily endpoint the same rule makes incremental resume clean: passing the
last stored `candle_ts` back returns only genuinely new candles — no duplicate,
no gap.

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

The shared classifier compares against `sma200`. With fewer than 200 stored
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

**Treat `regime` as a description of structure, not a prediction** (bias, score and confidence were removed 2026-11-19; the measurement below is the record) — the
same position `market-regime` takes: *"a regime is a description of what has
already happened, not a forecast."* Do not size a trade off this column.

Two related findings from the same measurement:

- `atr_floor = 0.004` in the OLD `detect_market_regime` had **never fired**. NIFTY's
  `atr_14/close` has a floor around 0.72% (p5); 0.40% is below p0, so
  `compressed` was False on all 943 sessions. It is an inherited constant that
  does nothing.
- Volatility, unlike direction, **is** forecastable. `atr_14/close` terciles
  predict next-day realised range monotonically — 0.728% / 0.911% / 1.123%, a
  1.54× spread. VIX/20-day-mean terciles work too but separate less well
  (1.30×).

## Porting notes

**The classification is no longer a port, and no longer lives here.** It comes
from the shared [`market-classifier` layer](../layers/market-classifier/README.md),
which runs the *same* rules on daily candles here and on 5-minute candles in
`intraday-market-sentiment`.

What it replaced: `detect_market_regime`, `calculate_sentiment`,
`sentiment_bias`, `calculate_confidence` and `combined_label`, all ported
formula-for-formula from `trading-algo/helpers/`, plus a local `indicators.py`
that duplicated `strategy-manager`'s copy byte for byte. All are deleted.

**Why they went rather than being kept.** The intraday path carried a port of a
*different* legacy builder, and the two disagreed about everything that
matters — `±7` score against `±3`, bias at `±4` against `±2`,
`TREND`/`RANGE`/`TRANSITION` against `TREND`/`RANGE`. So `regime` on a daily row
and `regime` on an intraday row were different measurements wearing one name,
and a score of `−3` meant "mildly bearish" in one and "as bearish as it gets" in
the other. Reconciling meant imposing one frame's rules on the other; both were
discarded and the replacement defined fresh.

**What changed in this table as a result:** `bias` is now
`bullish`/`bearish`/`range-bound` (was `neutral`, not `range-bound`); `regime`
is `trending`/`sideways`/`volatile-expansion` (was the six combined labels);
`sma20` became `sma9`; and `max_score`, `volatility`, `swing_*`,
`structure_determined`, `range_used`, `volatility_expanding` and `gap_pct` are
new columns. The single test row that predated the change was deleted.

Wilder RSI/ATR still match legacy formula-for-formula. The four deliberate
departures below all still hold.

1. **The `df.iloc[:-1]` drop is gone.** Legacy dropped the newest row because it
   ran intraday and that row was today's forming candle. The daily endpoint lags
   a session, so at 09:35 the newest stored row is already closed; dropping it
   would compute every level from the day *before* yesterday.
2. **VIX is real.** Legacy did `vix = last_closed.get("vix", 12.0)` and nothing
   in that path ever populated a `vix` column, so it was **always 12.0**. That
   value fed `calculate_confidence`. INDIA VIX is now fetched and stored like
   any other instrument — 4,612 daily candles back to 2008, real OHLC, genuinely
   zero volume.
3. **`price` comes from intraday**, since today's daily candle does not exist.
4. **`expected_move` is VIX-implied** — see above.

`bias` (removed 2026-11-19), `structure` and `regime` became three separate answers rather than a
combined label: `structure` is the swing read alone, and `regime` is that read
after volatility has had a veto. See the layer README for why those are not the
same question.

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
| `DHAN_CHARTS_BASE` | no | `https://api.dhan.co/v2/charts/` |
| `HTTP_TIMEOUT_SECONDS` | no | `60` |
| `PROFILE_SESSIONS` | no | `20` — sessions the normal width is the median over |
| `PROFILE_LOOKBACK_DAYS` | no | `45` — calendar days fetched to find them (one call; Dhan caps intraday at 90) |
| `PROFILE_MIN_SAMPLES` | no | `15` — a slot needs a candle in this many sessions, or the build raises |
| `MORNING_BRIEF_V2` | no | *unset* = shadow (old message sent, new brief logged); `on` = the new brief is the 09:35 message |
| `EVENING_BRIEF` | no | *unset* = shadow (the evening brief is built and logged); `on` = it is sent. Only the `{"brief": "evening"}` run reads it |
| `AGGREGATE_STRIKES_PER_SIDE` | no | `20` — the brief's chain window, the poller's own |
| `VIX_RANK_SESSIONS` | no | `20` — the sessions today's VIX move is ranked against |
| `ADR_SESSIONS` | no | `10` — ADR10 |
| `DHAN_OPTIONCHAIN_URL` / `DHAN_EXPIRYLIST_URL` | no | `https://api.dhan.co/v2/optionchain` / `.../optionchain/expirylist` |
| `DHAN_CLIENT_ID` | no | fallback only - the client id comes from the token's `dhanClientId` claim |

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

**Timeout: 90 s (read from AWS 2026-11-22; this note said 60), and that is tight.** A run takes
~12 s, but the Dhan retry backoff is `4 × 2^(attempt+1)` = 8 + 16 + 32 = **56 s of sleeping on
one rate-limited call**. A single `DH-904` kills the run. 300 s is the safe value, and **the
morning brief needs it**: three more calls, at the same minute as intraday-fno-poller's 09:35
tick (each function paces only itself).

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
- `SENTIMENT_FIELDS` was diffed against `information_schema.columns` on the live
  table after the migration: 35 columns each way, zero drift

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
