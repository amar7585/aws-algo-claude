# intraday-market-sentiment

AWS Lambda function that writes one row describing the state of the market
every fifteen minutes through the session — futures basis and open-interest
buildup, INDIA VIX, and the option-chain read (straddle, PCR, OI walls, max
pain, IV skew) for two expiries at once.

| | |
|---|---|
| Schedule | 09:35 → 15:35 at 15-min steps, `Asia/Kolkata` — three rules, see [Deployment](#deployment-shape) |
| Invocations | **25** per trading day |
| Writes | `algo.intraday_market_sentiment`, `algo.option_chain_snapshot` |
| Reads | `algo.instrument_master`, and its own previous row |
| Instruments | NIFTY (`13`/`INDEX`), INDIA VIX (`21`/`INDEX`), the current-month future |
| Database | Neon `AI Trader APP` (`nameless-mountain-15353651`) / `Algo` / `algo` — the only one |

It does not push to Telegram. Failures surface through
[error-notifier](../error-notifier/README.md) like every other function here.

## Everything comes from the Dhan API, with one exception

This function does **not** read `candle_5min` or anything else
[intraday-data-loader](../intraday-data-loader/README.md) writes. It fetches
its own candles and its own chains, so the two components share no state and a
stalled loader cannot quietly feed this one stale inputs.

The exception is **its own previous row**. Dhan serves only a *live* option
chain — there is no historical-chain endpoint — so an intraday OI delta cannot
be computed any other way. That one read back is what `prev_snapshot_ts`
records.

## The five-minute offset is the point

Runs fire at 09:35, 09:50, 10:05 … 15:35 — five minutes past each 15-minute
boundary. At each of those moments a 5-minute bucket has *just closed*, so the
bar the snapshot describes is final.

That matters because **nothing this function writes is ever partial**.
`intraday-data-loader` stores the in-progress bucket on purpose and corrects it
by primary key on a later pass; a snapshot row is never revisited, so a partial
bar written here would be wrong forever with nothing to correct it. The bar is
chosen by the same test that README documents for reading `candle_5min`:

```
a bar is closed  iff  candle_ts + interval_seconds <= now
```

The schedule makes that test cheap. The test is what makes it *true*.

## snapshot_ts is the bar, not the clock

`snapshot_ts` is the timestamp of that last closed bar — 09:30 for the 09:35
run, 09:45 for the 09:50 run. `captured_at` holds the actual run time, so the
lag stays visible. Three things follow:

- snapshots land on a clean 15-minute grid, 09:30 → 15:15, **and then 15:25**;
- a re-run at 09:36 **overwrites** its own row rather than adding a near-duplicate;
- the previous-snapshot lookup is exact rather than approximate.

**The last snapshot of a day is 15:25, not 15:30.** A session's final 5-minute
bar is stamped 15:25; there is no 15:30 bar, because the market closes then and
a 15:30 stamp is post-close data this system filters out. So the 15:35 run
describes 15:25 and its deltas cover **ten** minutes rather than fifteen —
which is precisely why `prev_snapshot_ts` is stored rather than the window
being assumed. Verified against the 2026-09-11 session: 75 bars, 09:15 → 15:25.

Every other series — the future, INDIA VIX — is aligned to the index's
`snapshot_ts` rather than to its own latest bar, so `basis`, `vix` and `spot`
all describe the same instant. A series missing a bar at exactly that stamp
falls back to its newest earlier one **and logs how stale it is**; computing
basis across two different minutes silently is the failure that avoids.

## Every change column shares one baseline

`prev_snapshot_ts` is `MAX(snapshot_ts) < this one` — full stop, not restricted
to today. Three consequences, all intended:

| Situation | Baseline | Effect |
|---|---|---|
| Normal run | 15 minutes earlier | a 15-minute delta |
| First run of a day (09:35) | **previous session's 15:30** | deltas span the overnight gap |
| After a missed run | 30+ minutes earlier | a wider delta, no special handling |

Because the window varies, `prev_snapshot_ts` is stored on every row. A reader
takes the window from the data rather than assuming it is fifteen minutes.

**Open-relative figures are deliberately not stored.** `spot`, `vix_open` and
the rest make the first snapshot of a day recoverable from the table, so
"since open" derives from the stored series instead of occupying columns.

### OI deltas go null across an expiry roll

If the previous row's expiry differs from this row's, its OI totals describe a
contract that is no longer being reported here. A percentage between the two is
arithmetic on unrelated numbers that reads exactly like a real collapse in open
interest, so it is left `NULL` instead.

## The two expiries

Both live on **one row** — `near_*` and `mth_*` — so one row is one complete
read of the moment.

- **nearest** — the first expiry on or after today.
- **monthly** — the first monthly expiry *strictly after* the nearest.

A monthly is the **last expiry in its calendar month**, found by grouping and
never by counting weeks. Measured 2026-09-12, the live list is
`2026-09-15, 09-22, 09-29, 10-06, 10-13, 10-27, 11-23, 12-29` — October's
`10-20` is simply absent, so "the fourth Tuesday" and "the fifth entry" are
both wrong while "the last one in October" is right.

"Strictly after the nearest" is the whole collision rule. In the last week of a
month the nearest expiry *is* that month's monthly, and both prefixes would
carry the same numbers:

```
nearest 2026-09-15  ->  monthly 2026-09-29
nearest 2026-09-29  ->  monthly 2026-10-27   (collision, rolled)
```

`near_expiry_ts` and `mth_expiry_ts` are therefore never equal.

## Two strike widths, and they are not interchangeable

| | Width | Why |
|---|---|---|
| Aggregates — PCR, OI totals, max pain, the walls | ATM **±20** | the chain carries 232 strikes; across all of them, strikes 5,000 points away outvote the ones price is near |
| Raw legs stored | ATM **±2** — 5 strikes × CE/PE = **10 rows** | enough to replay the money strikes without storing 41 × 2 × 2 every fifteen minutes |

**The strike step is derived from the sorted strike list, never hardcoded.**
It is 50 today and has not always been — same reasoning as the futures month.

Both chains are centred on the **same** `spot`, passed in rather than taken
from each chain's own `last_price`: two chains fetched seconds apart can report
slightly different prices, and an ATM that differed between them would make
`near_*` and `mth_*` quietly incomparable. `chain_spot` stores the nearest
chain's own `last_price` so any divergence stays visible.

## The buildup label

`buildup` reads price against open interest on the current-month future:

| price | OI | label | meaning |
|---|---|---|---|
| up | up | `LONG_BUILDUP` | new longs, conviction |
| down | up | `SHORT_BUILDUP` | new shorts, conviction |
| up | down | `SHORT_COVERING` | shorts closing, not new buying |
| down | down | `LONG_UNWINDING` | longs closing, not new selling |

The second column is the one that matters: a rally on rising OI is money coming
in, a rally on falling OI is money leaving. **They look identical on a price
chart.**

This classifies; it does not forecast. `daily-market-sentiment` already records
that its own score is not predictive of forward return — treat this the same
way.

## Measured facts

Against the live API on 2026-09-12. Each is load-bearing.

- **The chain is not under the expiry list's path.** `expirylist` is at
  `/v2/optionchain/expirylist`, but the chain itself is the flat
  `/v2/optionchain` — `/v2/optionchain/optionchain` answers
  `404 page not found`. The two URLs are held whole in `config.py` rather than
  as a shared base plus suffixes, because a shared base is exactly the
  assumption that produced the 404.
- **Futures open interest comes back as `open_interest`, not `oi`.** The
  *request* parameter is `oi: true`; the *response* adds a seventh top-level
  array named `open_interest`. Asking `"oi" in payload` reports `False` while
  the data sits right there — which is how this was nearly missed.
- **Charts have no envelope; the chain does.** The six chart arrays are top
  level; the chain returns `{"status", "data"}`.
- **The chain is keyed by strike as a six-decimal string** — `"23950.000000"`,
  not `"23950"` and not a number. The original key is looked up, never
  reconstructed from a float.
- **IV and the greeks come back as `0` when Dhan did not compute them.** An
  illiquid deep-ITM put quoting a 490.6 last price returned
  `implied_volatility: 0` with all four greeks `0`. That is *absent*, not zero
  volatility, and stored as `0` it poisons any average or subtraction —
  `iv_skew` in particular. Mapped to `NULL` in exactly one place, `chain._number()`.
- **The future returns more bars than the index** for the same window — 77
  against 75 on 2026-09-11. The extra two are out of session and are filtered.
- **INDIA VIX carries volume 0 on every bar**, so VWAP is computed for the
  index only.

## Verified end to end

Against captured payloads from the real 2026-09-11 session, two consecutive
runs so the baseline path is exercised — 15/15 checks, including that run 1
writes null deltas and no buildup, run 2 sets both, both expiries differ, ATM
matches across chains, exactly 10 legs land per snapshot, and no `0` reaches an
IV or greek column.

| | 10:00 snapshot | 10:15 snapshot |
|---|---|---|
| spot | 23,271.9 | 23,292.5 |
| basis | 58.0 | 61.1 (0.26%) |
| buildup | — (no baseline) | `SHORT_COVERING` |
| near straddle | 282.75 | 254.70 |
| near PCR (OI) | 1.242 | 1.194 |
| max pain — near / monthly | | 23,450 / 23,800 |

**Values in a mid-session replay are not a live sanity check.** Captured on a
Saturday, the chain is Friday's *close* while the candles replay 10:15, so
`chain_spot` sits 105 points above `spot` — an artefact of replaying a
mid-session bar against an end-of-day chain, and exactly what `chain_spot`
exists to expose.

### Verified against the deployed function

Three invocations on 2026-09-12 against the real API and the real database,
driving `now` to 2026-09-11 15:20 and 15:35. **15:35 is the one coherent
replay point**: it makes `snapshot_ts` the 15:25 closing bar, which the live
end-of-day chain genuinely describes — and `spot` came back 23398.10 against
`chain_spot` 23398.10, with ATM resolving to 23400.

| | 15:15 | 15:25 |
|---|---|---|
| spot / chain_spot | 23,435.1 / 23,398.1 | **23,398.1 / 23,398.1** |
| basis | 51.4 | 79.8 |
| buildup | — (no baseline) | `SHORT_BUILDUP` (price −0.158%, OI +0.135%) |
| near straddle / PCR | 194.0 / 1.068 | 204.0 / 1.112 |
| CE / PE OI change | — | −0.54% / **+3.53%** |

Puts being written into the close while calls thin out, with PCR rising
1.068 → 1.112 — internally consistent. Re-running 15:35 overwrote its own row
rather than adding one, confirming the idempotency the grain is built for. The
weekend gate and the outside-session guard both returned their skip statuses on
the deployed function. Run time 22–23 s, peak memory 105 MB of 256.

Validation rows were deleted afterwards: their `captured_at` claimed Friday
while they were written on Saturday, and leaving that in the table would
mislead any later reader.

**The IV skew is real, not an artefact.** On the coherent 15:25 row, with ATM
23400 genuinely at the money, `near_ce_iv` is 13.43 against `near_pe_iv` 8.77 —
a −4.66 skew. An earlier reading blamed a mismatched ATM; that explanation does
not survive the coherent row. Whether Dhan's ATM IVs are routinely this
asymmetric on a weekly expiry is **an open question for the first live
session**, not a settled one.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `DHAN_CHARTS_BASE` | `https://api.dhan.co/v2/charts/` | |
| `DHAN_OPTIONCHAIN_URL` | `https://api.dhan.co/v2/optionchain` | flat, see above |
| `DHAN_EXPIRYLIST_URL` | `https://api.dhan.co/v2/optionchain/expirylist` | nested |
| `TOKEN_PARAMETER_NAME` | `/algo/dhan/token` | written by `auth-dhan-broker` |
| `NIFTY_SECURITY_ID` / `_INSTRUMENT_TYPE` | `13` / `INDEX` | always passed together |
| `VIX_SECURITY_ID` / `_INSTRUMENT_TYPE` | `21` / `INDEX` | |
| `FUTURES_UNDERLYING_SCRIP` / `_SEG` | `13` / `IDX_I` | |
| `AGGREGATE_STRIKES_PER_SIDE` | `20` | PCR, OI totals, max pain |
| `RAW_STRIKES_PER_SIDE` | `2` | 5 strikes → 10 stored legs |
| `BUILDUP_EPSILON_PCT` | `0.0` | **not set from measurement** — see below |
| `API_PACING_SECONDS` | `4.0` | measured; tighter than the documented 5/s |
| `HTTP_TIMEOUT_SECONDS` | `60` | |

The client id is **not configured** — it comes from the access token's own
`dhanClientId` claim, so it cannot drift out of step with a rotated token.
Unlike `intraday-data-loader`, it is resolved *before* any fetch: that function
defers it so a missing id cannot abort a run whose index candles are already
worth committing, whereas here every call feeds one row written once at the
end, so failing early is clearer.

## Deployment shape

Three EventBridge Scheduler rules, `Asia/Kolkata`, all targeting this function:

```
cron(35,50 9 ? * MON-FRI *)             09:35, 09:50              2
cron(5,20,35,50 10-14 ? * MON-FRI *)    10:05 … 14:50            20
cron(5,20,35 15 ? * MON-FRI *)          15:05, 15:20, 15:35       3
```

Three rather than one because a single `9-15` rule would also fire 09:05, 09:20
and 15:50. The handler keeps an outside-session guard anyway, as a second line
of defence against a manual invocation or a hand-edited rule — not as the
mechanism.

### Two IAM roles, both dedicated — and why reusing the loader's failed

Neither of the existing roles could be reused, and **both would have failed
silently rather than loudly**. Found during this deployment, 2026-09-12:

- **The loader's execution policy is scoped to its own log group.**
  `AWSLambdaBasicExecutionRole-6078fa24…` allows `logs:CreateLogStream` and
  `PutLogEvents` only on
  `arn:aws:logs:…:log-group:/aws/lambda/intraday-data-loader:*`. Attached here,
  this function would run, write its row, and produce **no logs at all** — and
  with no logs, `error-notifier` could never see a failure either.
- **The shared EventBridge Scheduler role is scoped the same way.**
  `Amazon_EventBridge_Scheduler_LAMBDA_22b6c2d00c` allows `lambda:InvokeFunction`
  only on `function:intraday-data-loader`. The three schedules were created
  against it first and would have failed at fire time on a Monday morning, with
  nothing in this function's logs to say so — because it would never have been
  invoked.

So this function has **`intraday-market-sentiment-role`** (execution: logs
scoped to its own group, plus `algo-ssm-read` for the two parameters) and
**`EventBridge-Scheduler-intraday-market-sentiment`** (invoke, scoped to this
function). Widening the shared policies would have coupled the two functions'
blast radius; a dedicated pair costs nothing and keeps them apart.

| | |
|---|---|
| Handler | `handler.lambda_handler` — set under **Code → Runtime settings**, not Configuration → General |
| Runtime | Python 3.14, zip package, 8 modules |
| Layers | `neon-db-driver` + `neon-access` |
| Secrets | `/algo/dhan/token`, `/algo/neon/connection` — no environment variables required |

Upload a zip; never paste into the console editor. A browser paste of a
350-line handler silently truncated at line 44 in this repo and surfaced later
as an unterminated-string error.

Apply [`schema.sql`](schema.sql) before the first run — **confirm the Neon
project id first**, since a second project carries an `algo` schema with the
same table names and incompatible columns.

## Local verification

`pg8000` comes from the layer and `boto3` from the runtime, so both are stubbed
and `neon_access` is loaded from the layer source. Dhan and Postgres are faked;
everything else is the shipped code. See the session scratchpad for
`capture.py` (writes `payloads.json` from the live API) and `test_local.py`.

## Known limitations

- **Max pain is windowed.** It is computed over the same ATM ±20 strikes as
  everything else, so it can only name a strike inside that window. A true max
  pain sweeps the whole chain. An answer sitting *at* the window's edge should
  be read as "at least this far", not as the minimum. Sweeping all 232 strikes
  was rejected because the same far-OTM OI that distorts PCR distorts max pain
  too.
- **`BUILDUP_EPSILON_PCT` is 0.0, meaning pure sign.** The label flips on the
  smallest tick in a quiet fifteen minutes. A sensible floor needs the
  distribution of real 15-minute price and OI moves, which this function has to
  run for a while to produce — it is a tunable so that can be set later without
  touching `sentiment.py`.
- **The monthly expiry at the far end of the list is not trustworthy.** If Dhan
  truncates the expiry list mid-month, that month's last *entry* is not its
  monthly expiry. It does not bite in practice because the monthly chosen is
  always within a month or two of today, where the list is dense.
- **A third expiry would be a schema change.** Both expiries live on one row as
  column pairs; that was chosen deliberately over a row-per-expiry grain, and
  the cost is this.
