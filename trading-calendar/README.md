# trading-calendar

Reference data, not a Lambda: the weekday dates on which the exchange is
closed. `algo.trading_holiday` is read by the session functions to decide
whether today is a trading day at all.

| | |
|---|---|
| Table | `algo.trading_holiday` |
| Project | **AI Trader APP** `nameless-mountain-15353651`, database `Algo` |
| Rows | 16 weekday holidays, all of 2026 — applied 2026-09-13 |
| Source | NSE's annual trading holiday circular |
| Refresh | by hand, once a year — there is no loader |

## The one rule that matters

**A row means closed. No row means a normal session.**

Absence is the safe default, and that is the whole design. A calendar nobody
reseeded answers "no row" for every date, so the system keeps trading — which
costs a handful of no-op invocations on a holiday. The opposite convention
(absence means closed) would make a forgotten reseed look exactly like a quiet
Sunday, and no alarm in this system can see a function that was never invoked.

Weekends are not stored. Every cron here is `MON-FRI`, so a Saturday is
excluded before this table is consulted; storing ~104 rows a year to restate
that would be noise.

## `trade_date` is a `date`, not epoch seconds

This departs from the repo-wide rule that every stored time value is epoch
seconds (`bigint`), and the departure is deliberate.

That rule exists so stored **instants** are unambiguous — a candle timestamp
epoch-encoded cannot be misread. A trading holiday is not an instant, it is a
calendar date. Epoch-encoding it forces every reader to agree on "midnight in
which zone", and a reader that picks UTC is off by 5.5 hours against a system
that thinks in IST — silently, which is precisely the failure the epoch rule
was written to prevent.

`updated_at` **is** an instant, so it stays `bigint` epoch seconds.

## Reseeding

**NSE's circular is the source of truth.** It publishes the following year's
holidays around December, with names. Transcribe it into a `seed_<year>.sql`
alongside `seed_2026.sql`, and apply it. Apply `schema.sql` once first; the
insert is an upsert, so re-running either is safe.

### Cross-check the transcription

`generate_seed.py` exists to check the hand-transcribed list, not to replace
it — a mistyped or missed date is silent, and this is the cheapest way to catch
one:

```bash
python -m venv .venv && ./.venv/bin/pip install exchange_calendars
./.venv/bin/python generate_seed.py 2027 > /tmp/check_2027.sql
# then diff the dates against your seed
```

It earned its keep: for 2026 it produced **exactly** the same 16 dates as the
official circular, independently. It cannot supply names, so the circular is
still what gets committed.

`exchange_calendars` is a laptop-only dependency — it drags in pandas and
numpy, which is exactly the weight this repo keeps out of its functions.
Nothing at runtime imports it.

**The generator refuses to emit a year the package cannot cover**, rather than
emitting a short one. A silently-truncated seed is a system that goes quiet on
a date nobody wrote down. Note that the package trails the circular: it only
gains a year once a release picks the circular up, so in December the
hand-transcribed list will be ahead of it and there will be nothing to check
against until then.

## Coverage ends 2026-12-31

From 2027-01-01 this table answers "no row" for every date, which reads as
"trading day" — so the system keeps running and treats 2027's holidays as
normal sessions until someone reseeds. Noisy, not silent, which is the right
way round, but it does need doing.

The holiday notice makes the cliff announce itself: `auth-dhan-broker` reports
the next session in its Telegram message, and when it cannot find one inside
its horizon it says the calendar may need reseeding. That only fires on a
holiday, though, so it is a backstop, not a reminder — January is the month to
do this.

## What this table deliberately does not cover

- **Holiday names, if seeded from the generator.** XBOM carries its holidays as
  bare date lists, so generated rows have `description NULL`. The 2026 rows are
  named because they came from the circular instead. Never fill a name in by
  guessing which festival fell on a movable date — that is how a seed file
  starts lying.
- **Muhurat trading.** The package models no non-standard session at all —
  every session 2024-2026 is 09:15–15:30, and the Diwali muhurat evenings
  (2024-11-01, 2025-10-21) are marked as *not sessions*. A muhurat day is a
  trading day with unusual hours, which is not what a holiday table describes.
  It needs its own mechanism and is not built.
- **Special weekend sessions.** 2025-02-01, a Budget Saturday, was a real
  session. Nothing here can honour it: every cron is `MON-FRI`.

## Layout

```
schema.sql        the table
seed_2026.sql     16 rows, transcribed from NSE's circular — the seed
generate_seed.py  cross-check for a transcription (laptop only, never in Lambda)
```
