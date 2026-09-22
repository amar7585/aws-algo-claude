# Daily session-observation runbook

← [Back to root README](../README.md) · [CLAUDE.md](../CLAUDE.md)

**Trigger.** When Amar shares a session chart/screenshot and names a trade or pattern
(or asks to "log today's session"), follow this file end to end. This is the standing
manual workflow for the observation-only phase — validate what Amar spotted against
captured data + futures OI, then hand-log it to `algo.session_observations`.

**Phase.** Observation-only. Nothing is auto-traded. A pattern-detector Lambda is
deployed but **log-only** (writes no table); we still hand-log observations here.
`session_observations` is hand-curated ground truth — the detector must never write it.
Deep lineage is in the memory note `pattern-detector-futures-oi-design`.

---

## Hard rules

- **Neon project `nameless-mountain-15353651`, database `Algo`, schema `algo`, branch
  `production` (default).** Confirm the project id before any SQL — a second Neon project
  has clashing table names. Use the Neon MCP `run_sql`.
- **Every time value is epoch seconds** (bigint). Convert with
  `to_timestamp(ts) AT TIME ZONE 'Asia/Kolkata'`. No timezone columns.
- **Don't decide alone** (CLAUDE.md §0). Surface `pattern_type` / entry model / resolution
  point / `signal_agreement` as judgment calls and get Amar's confirmation **before** INSERT.

## Instruments

- **Index:** `security_id '13'`, `instrument_type 'INDEX'` (NIFTY 50).
- **Active future:** read `intraday_fno_data.fut_security_id` from the latest snapshot —
  it **rolls at monthly expiry**, so never hardcode. (Was `68407` = NIFTY-SEP2026-FUT.)
  In `candle_5min` it is that `security_id`; Dhan segment `NSE_FNO`, instrument `FUTIDX`.

## Where the data is (post schema-split)

| Table | Grain | Holds |
|---|---|---|
| `algo.candle_5min` | 5-min | OHLCV per `(security_id, instrument_type, candle_ts)`. **No OI column.** |
| `algo.intraday_fno_data` | 15-min | `fut_price`, `fut_oi`, `fut_oi_change_pct`, `basis`, `near_pcr_oi`, `near_pe/ce_oi_change_pct`, `orb_high/low`, `day_high/low`, `near_max_oi_call/put`, `near_max_pain`, `sma*`, `rsi`, `vix`, `fut_security_id`. |
| `algo.intraday_sentiments` | 15-min | `buildup`, `regime`, `bias`, `structure`, `volatility`, `swing_high/low`, `score`. |
| `algo.daily_market_sentiment` | daily | previous-day levels (`pd_high`/`pd_low`) when needed. |
| `algo.session_observations` | per event | **write target** (see schema below). |

`buildup` and `fut_oi` live in **different** tables — join them. The 15-min snapshot grid
runs **09:45 → 15:25** (24/day, post-retime); candles are 5-min.

`session_observations` columns: `security_id, instrument_type, event_ts,
nearest_snapshot_ts, pattern_type, price_at_event, resolved_ts, resolved_price,
forward_return_pct, outcome, signal_agreement, notes, created_at`. PK
`(security_id, instrument_type, event_ts, pattern_type)`. `outcome IN
(reversal|continuation|no_signal)`. `signal_agreement IN (confirmed|contradicted|no_data)`
or NULL.

## Pull queries (replace `DATE`, e.g. `2026-09-22`; `<FUT_ID>` from query 1)

**1) Joined 15-min snapshots — levels + OI + buildup + regime:**

    SELECT to_char(to_timestamp(f.snapshot_ts) AT TIME ZONE 'Asia/Kolkata','HH24:MI') t,
      f.snapshot_ts, f.fut_security_id, f.fut_price, f.fut_oi, f.fut_oi_change_pct,
      f.basis, s.buildup, f.near_pcr_oi, f.near_pe_oi_change_pct, f.near_ce_oi_change_pct,
      f.spot, s.regime, s.bias, f.orb_high, f.orb_low, f.day_high, f.day_low,
      f.near_max_oi_call, f.near_max_oi_put, f.near_max_pain, s.swing_high, s.swing_low
    FROM algo.intraday_fno_data f
    LEFT JOIN algo.intraday_sentiments s USING (security_id, instrument_type, snapshot_ts)
    WHERE to_timestamp(f.snapshot_ts) AT TIME ZONE 'Asia/Kolkata' >= 'DATE'::date
      AND to_timestamp(f.snapshot_ts) AT TIME ZONE 'Asia/Kolkata' <  'DATE'::date + 1
    ORDER BY f.snapshot_ts;

**2) 5-min candles (index + future) for the event window:**

    SELECT security_id, to_char(to_timestamp(candle_ts) AT TIME ZONE 'Asia/Kolkata','HH24:MI') t,
      open, high, low, close, volume
    FROM algo.candle_5min
    WHERE security_id IN ('13','<FUT_ID>')
      AND to_timestamp(candle_ts) AT TIME ZONE 'Asia/Kolkata' BETWEEN 'DATE HH:MM' AND 'DATE HH:MM'
    ORDER BY candle_ts, security_id;

## Analysis method — the "two-clock" read

For each pattern Amar flags, characterize **both clocks**:

- **Same-tick clock — future 5-min price + VOLUME.** A climax / collapse / divergence in the
  **future's traded volume** marks the turn on the same candle price turns, usually cleaner
  than the index. This is the reliable same-tick tell.
- **Lagging clock — futures `buildup` / OI on the 15-min grid.**
  - **Reversals:** futures `buildup` typically confirms **one 15-min tick late** (SHORT_BUILDUP
    at a low → LONG_BUILDUP next tick; SHORT_COVERING at a top → LONG_UNWINDING next tick).
    Bottoms are sometimes confirmed only by **option OI** (PCR collapse / PE unwind), not
    futures buildup — weigh futures buildup + option PCR + futures volume together.
  - **Continuations (breakouts):** OI usually agrees **same-tick** (no lag); note whether the
    move is short-covering (falling OI, rising PCR) vs fresh long-buildup — covering-driven
    moves tend to fade.

**Level proximity.** A pattern matters near a real level: `orb_high/low`, `day_high/low`,
`pd_high/pd_low`, `near_max_oi_call/put`, `near_max_pain`, `swing_high/low`. "Near" has **no
fixed threshold** (deliberately unset — same posture as `BUILDUP_EPSILON_PCT` / the RSI-weight
deferral): **annotate the nearest level + distance** in the note, don't hard-drop.

## Filling the row

- `event_ts` — the 5-min candle where the pattern triggered (epoch).
- `nearest_snapshot_ts` — the 15-min snapshot that best reflects the event state.
- `price_at_event` — index **close** of the event candle, unless Amar specifies a different entry.
- `resolved_ts` / `resolved_price` — **data-driven, not a fixed window**: the leg resolves at the
  next reversal / leg-end on the same session, or the **15:25 close** if none. Intraday only.
- `forward_return_pct` = `(resolved_price - price_at_event) / price_at_event * 100`.
- `outcome` — what price **did** (`reversal|continuation|no_signal`).
- `signal_agreement` — did the stored `buildup`/PCR/OI **at `nearest_snapshot_ts`** agree with the
  outcome (`confirmed|contradicted|no_data`)? Use the state **at the nearest snapshot** for the
  label (keeps rows comparable); put any +1-tick flip in `notes`. `no_data` before 09:45.
- `notes` — the full two-clock read, levels + distances, raw numbers, and any live
  pattern-detector cross-check.

## Pattern vocabulary logged so far

- **Reversals:** `evening_star`, `double_top`, `double_bottom`, `breakout_fade_reclaim` (incl.
  downside failed-breakdown reclaim), `orb_breakout_fade_reclaim`.
- **Continuation:** `orb_breakout_retest`.

Introduce a new `pattern_type` only with Amar's sign-off.

## Optional — Dhan REST cross-check (raw 5-min OI, not stored in the DB)

Only when Amar asks or the OI read is ambiguous. The token **expires** — ask Amar for a fresh
access token. Direct REST (not the Dhan MCP, per Amar's preference):

    POST https://api.dhan.co/v2/charts/intraday
    header: access-token: <token>
    body (future): {"securityId":"<FUT_ID>","exchangeSegment":"NSE_FNO","instrument":"FUTIDX",
                    "interval":"5","oi":true,"fromDate":"DATE","toDate":"DATE+1"}
    body (index):  {"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX",
                    "interval":"5","oi":false,"fromDate":"DATE","toDate":"DATE+1"}

Response arrays: `open/high/low/close/volume/timestamp/open_interest`; timestamps epoch, IST =
`+19800s`. Use per-candle `dOI` to distinguish shorts-pressing-the-low from short-covering thrusts.

## Workflow for a session

1. Amar attaches the chart and says which trade(s)/pattern(s) he saw.
2. Confirm the Neon project id; run queries 1 & 2 for today; confirm the active `fut_security_id`.
3. Two-clock read + level proximity for each pattern.
4. Cross-check the live pattern-detector's decision if useful.
5. **Propose** the `session_observations` row(s), values filled, and **surface the judgment calls**
   (pattern_type name, entry/event candle, resolution point, signal_agreement).
6. On Amar's OK, INSERT; then update the memory note `pattern-detector-futures-oi-design` with
   the session's result and the new row count.

## State so far

`session_observations` has **7 rows** — 4× 2026-09-17, 2× 2026-09-18, 1× 2026-09-21, all NIFTY
INDEX; verified against Dhan REST where noted. Standing plan: keep hand-logging each session until
the rules are clear enough to automate; the detector is already deployed log-only.
