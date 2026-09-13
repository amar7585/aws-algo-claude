# Flow charts

← [Back to root README](../README.md) · [Architecture](architecture.md) · [Components](components.md)

## A trading day, end to end

Everything below this section is one plane in detail. This is the order they
actually happen in, and what hands off to what.

```mermaid
flowchart TD
    subgraph BEFORE["Before the session"]
        AUTH["auth-dhan-broker<br/><i>cron, weekdays 08:00</i>"] --> SSM[/"SSM /algo/dhan/token<br/>24h TOTP-minted token"/]
        LOADER["instrument-master-loader<br/><i>cron, monthly</i>"] --> IM[("instrument_master")]
    end

    subgraph SESSION["09:15-15:30"]
        DAILY["daily-market-sentiment<br/><i>cron, 09:50</i>"] --> DTBL[("candle_daily<br/>daily_market_sentiment")]
        DAILY --> TG1{{"Telegram"}}

        CANDLES["intraday-data-loader<br/><i>cron, every 5 min<br/>10:00-15:30 + 15:35</i>"] --> CTBL[("candle_5min<br/>candle_15min<br/>candle_1hr")]

        SENT["intraday-market-sentiment<br/><i>cron, every 15 min<br/>09:35-15:35</i>"] --> STBL[("intraday_market_sentiment<br/>option_chain_snapshot")]
    end

    subgraph STRAT["The strategy plane - chained, not scheduled"]
        MGR["<b>strategy-manager</b><br/><i>invoked, 25x/day</i>"]
        SWEEP["<b>strategy-range-liquidity-sweep</b><br/><i>invoked when RANGE</i>"]
        MGR -->|"Event: the context"| SWEEP
        SWEEP --> LOG{{"CloudWatch log<br/><i>the only output</i>"}}
    end

    SENT -->|"Event: the snapshot it just wrote"| MGR

    SSM -.token.-> DAILY
    SSM -.token.-> CANDLES
    SSM -.token.-> SENT
    SSM -.token.-> MGR
    CTBL -.candles.-> MGR
    DTBL -.daily read.-> MGR
    IM -.identity.-> CANDLES
    IM -.identity.-> SENT
    IM -.identity.-> MGR

    ERR["error-notifier<br/><i>log subscription</i>"] --> TG2{{"Telegram"}}
    LOG -.errors only.-> ERR

    style STBL fill:#14532d,color:#fff
    style CTBL fill:#14532d,color:#fff
    style DTBL fill:#14532d,color:#fff
    style IM fill:#14532d,color:#fff
    style MGR fill:#1e3a8a,color:#fff
    style SWEEP fill:#1e3a8a,color:#fff
```

Read in clock order, a weekday looks like this:

| Time (IST) | What runs | Trigger | Writes |
|---|---|---|---|
| monthly | `instrument-master-loader` | cron | `instrument_master` |
| 08:00 | `auth-dhan-broker` | cron | `/algo/dhan/token` |
| 09:50 | `daily-market-sentiment` | cron | `candle_daily`, `daily_market_sentiment`, Telegram |
| 10:00–15:30 every 5 min, + 15:35 | `intraday-data-loader` | cron | the three candle tables |
| 09:35–15:35 every 15 min | `intraday-market-sentiment` | cron | `intraday_market_sentiment`, `option_chain_snapshot` |
| immediately after each of those 25 runs | `strategy-manager` | **invoke** | nothing |
| immediately after, when the regime is RANGE | `strategy-range-liquidity-sweep` | **invoke** | nothing |
| on failure only | `error-notifier` | log subscription | Telegram |

**Six of the seven run on their own cron. The strategy plane is the exception.**
`strategy-manager`'s input *is* the intraday snapshot, so the function that
writes the snapshot invokes it — a cron there would have to guess how long the
write takes, read the row back, and decide what to do when it is not there yet.

**Nothing in the chain can fail its caller.** Every invoke is `Event`, so
`intraday-market-sentiment` finishing is not a claim that the manager
succeeded, and the manager finishing is not a claim that any playbook did. Each
function has its own log group and `error-notifier` reports from all of them.

**The strategy plane writes nothing at all.** The regime the manager classifies
travels in the invocation payload and the playbook records it in its own log
beside whatever it found, so the decision is recoverable from the consumer
rather than duplicated into a table by the producer.

## Instrument master refresh — monthly

Runs on an EventBridge Scheduler cron, independent of any trading session.

```mermaid
flowchart TD
    START(["EventBridge Scheduler<br/>monthly cron"]) --> DL

    DL["Download scrip master CSV<br/><i>~25 MB, 201,666 rows, no auth</i>"]
    DL --> COLCHECK{"Expected<br/>columns<br/>present?"}
    COLCHECK -->|no| FAIL["raise → Lambda error<br/><i>schema drift, alarm fires</i>"]
    COLCHECK -->|yes| GATE

    GATE["Exchange gate<br/><i>NSE*, or BSE SENSEX*</i>"]
    GATE -->|"drops 74,624 rows"| RULES
    RULES["Match against 9 INSTRUMENT_RULES<br/><i>first rule wins</i>"]
    RULES -->|"no rule matches"| DROP(["discarded"])
    RULES -->|"matched"| DEDUPE

    DEDUPE["Drop repeat<br/>(security_id, instrument_type)"]
    DEDUPE --> BATCH["Batch 5,500 rows<br/><i>3 statements</i>"]
    BATCH --> UPSERT[("INSERT … ON CONFLICT DO UPDATE<br/>algo.instrument_master")]
    UPSERT --> COMMIT["single COMMIT"]
    COMMIT --> DONE(["15,338 rows<br/>status: success"])

    UPSERT -->|"any batch fails"| RB["ROLLBACK → raise"]

    style FAIL fill:#7f1d1d,color:#fff
    style RB fill:#7f1d1d,color:#fff
    style DONE fill:#14532d,color:#fff
```

**Why the whole load commits once.** A partially written instrument master is
worse than a stale one: downstream lookups would silently miss contracts rather
than fail loudly, and the monthly cadence check would see fresh `updated_at`
values and decline to retry.

**Why failures raise instead of returning a status code.** Returning
`{"statusCode": 500}` makes Lambda record the invocation as a success — no
error metric, no alarm, no retry. On a job that runs twelve times a year, a
swallowed failure is one you discover from a bad trade.

### Numbers from the last run

Measured 2026-09-09, against the live scrip master and the live database.

| Stage | Value |
|---|---|
| CSV downloaded | 25.4 MB, 201,666 rows, 16 columns |
| Survive exchange gate | 127,042 |
| Match a rule | 15,338 |
| Written | 15,338 — 120 `IDX_I`, 2,675 `NSE_EQ`, 12,543 `NSE_FNO` |
| Upsert statements | 3 |

## Broker token refresh — each weekday morning

Runs on its own EventBridge Scheduler cron, `cron(0 8 ? * MON-FRI *)` in
`Asia/Kolkata`. Independent of the trading session.

```mermaid
flowchart TD
    START(["EventBridge Scheduler<br/>08:00 IST, Mon-Fri"]) --> TOTP

    TOTP["Generate TOTP<br/><i>RFC 6238, stdlib</i>"]
    TOTP --> GEN["POST /app/generateAccessToken<br/><i>clientId + PIN + TOTP</i>"]

    GEN -->|"non-200"| FAIL["raise → Lambda error<br/><i>alarm fires, no token</i>"]
    GEN -->|"200, no accessToken"| FAIL2["raise, quoting Dhan's body<br/><i>e.g. bad TOTP</i>"]
    GEN -->|200| EXP

    EXP{"JWT <b>exp</b><br/>claim<br/>readable?"}
    EXP -->|no| FAILEXP["raise<br/><i>refuse to store a guessed expiry</i>"]
    EXP -->|yes| WRITE

    WRITE[("Write token + expires_at<br/>/algo/dhan/token")]
    WRITE --> DONE(["metadata returned, no token<br/>status: success"])

    style FAIL fill:#7f1d1d,color:#fff
    style FAIL2 fill:#7f1d1d,color:#fff
    style FAILEXP fill:#7f1d1d,color:#fff
    style DONE fill:#14532d,color:#fff
```

**Why there is no renew step.** `/v2/RenewToken` would have let one TOTP login
carry a whole week, but it refuses tokens minted this way — `HTTP 500 DH-905
INVALID_REQUEST "Renewal of token not allowed for this token type"`, measured
2026-09-10. Renewal is offered only for tokens generated by hand from Dhan Web,
and seeding from the portal does not help: the chain breaks every weekend and
Monday's TOTP token is itself unrenewable. The branch was built, tested against
the live API, and deleted.

**Why one run a day is enough.** A token lives exactly 24 hours, so 08:00
covers the 09:15–15:30 session several times over. An earlier design ran every
12 hours purely to give the renew a margin it turned out not to need.

**Why an unreadable expiry raises.** `expires_at` is what consumers key their
freshness check off. A guessed value would keep a dead token in circulation
with nothing reporting it — the same class of silent wrongness as a 0-row load.

**Why the response carries no token.** It would only have served an on-demand
invoke that cannot fire in practice, and it put a live credential on the
console Test screen.

## Daily read — each weekday at 09:50

```mermaid
flowchart TD
    START(["EventBridge Scheduler<br/>cron(50 9 ? * MON-FRI *) IST"]) --> WKND{"Saturday<br/>or Sunday?"}
    WKND -->|yes| SKIP(["skip"])
    WKND -->|no| TOK["read /algo/dhan/token<br/>raise if expired"]

    TOK --> RESOLVE["resolve NIFTY and INDIA VIX<br/><b>on (security_id, instrument_type)</b>"]
    RESOLVE --> DAILY["fetch daily candles<br/>incremental from MAX(candle_ts),<br/>else 300-day cold start"]
    DAILY --> CD[("candle_daily")]

    DAILY --> INTRA["fetch today's 15-min candles<br/><b>fromDate 00:00, not 09:15</b>"]
    INTRA --> GUARD{"first candle<br/>stamped 09:15?"}
    GUARD -->|no| RAISE(["raise"])
    GUARD -->|yes| READ

    READ["compute the daily read<br/>on the newest COMPLETED candle"]
    READ --> SMA{"sma200<br/>available?"}
    SMA -->|no| RAISE
    SMA -->|yes| WRITE[("daily_market_sentiment")]
    WRITE --> TG(["Telegram"])

    IM[("instrument_master<br/><i>monthly, separate</i>")] -.read.-> RESOLVE
    CD -.read.-> READ

    style SKIP fill:#78350f,color:#fff
    style RAISE fill:#7f1d1d,color:#fff
    style TG fill:#14532d,color:#fff
```

Three things in that diagram are load-bearing and each was learned the hard way:

**Resolve on the pair.** `security_id` alone is not unique — 19 of them carry
more than one `instrument_type`, and `13` is both NIFTY and ABB. The lookup
raises unless exactly one row matches.

**`fromDate 00:00`, not `09:15`.** `fromDate` is exclusive, so starting at the
session open silently drops the 09:15 candle — which *is* the opening range.
The explicit stamp check turns that into a loud failure.

**The read describes yesterday.** Dhan's daily endpoint lags a session, so at
09:50 the newest stored daily candle is the previous session's. Today's open
comes from the intraday call instead.

There is no holiday gate. On a holiday the fetch returns nothing new and the
sentiment row is simply not written — a calendar would be a second source of
truth to keep correct.

## Intraday candles — every 5 minutes, 10:00–15:35

```mermaid
flowchart TD
    START(["EventBridge Scheduler<br/>every 5 min 10:00–15:30<br/>+ 15:35 sweep, IST"]) --> WKND{"Saturday<br/>or Sunday?"}
    WKND -->|yes| SKIP(["skip"])
    WKND -->|no| DUE["intervals due =<br/><b>(now − 09:15) mod I == 0</b><br/>15:35 ⇒ all three"]

    DUE --> TOK["read /algo/dhan/token<br/>raise if expired"]
    TOK --> NIFTY["resolve NIFTY<br/><b>on (security_id, instrument_type)</b>"]

    NIFTY --> WIN["window = MAX(candle_ts) − one interval<br/><b>re-fetches the partial bar</b><br/>else 90-day cold start"]
    WIN --> FETCH["fetch interval candles"]
    FETCH --> FILTER["drop out-of-session bars<br/>09:15 ≤ t &lt; 15:30"]
    FILTER --> ALIGN{"every bar on a<br/>bucket boundary<br/>from 09:15?"}
    ALIGN -->|no| RAISE(["raise"])
    ALIGN -->|yes| UPSERT[("candle_5min<br/>candle_15min<br/>candle_1hr")]

    UPSERT --> EXP["expiry list → nearest expiry's month<br/><b>NIFTY-&lt;MON&gt;&lt;YYYY&gt;-FUT</b>"]
    EXP --> FUT{"contract in<br/>instrument_master?"}
    FUT -->|no| RAISE
    FUT -->|yes| WIN

    IM[("instrument_master<br/><i>monthly, separate</i>")] -.read.-> NIFTY
    IM -.read.-> FUT

    style SKIP fill:#78350f,color:#fff
    style RAISE fill:#7f1d1d,color:#fff
    style UPSERT fill:#14532d,color:#fff
```

Four things in that diagram are load-bearing:

**One rule picks the intervals.** Dhan's intraday buckets are session-aligned
from 09:15, not clock-aligned — measured, by shortening `toDate` until the
candle count changed. So a run fetches interval *I* exactly when `(now − 09:15)`
is a whole multiple of *I* minutes, and the hourly trigger times fall out of
that rather than being written down separately. `assert_alignment()` raises if
the data ever stops matching.

**The window steps back one interval.** `fromDate` is exclusive, so resuming
from the newest stored `candle_ts` would never re-fetch that bar — and that bar
may be partial. Stepping back one whole interval re-fetches exactly it.

**The 15:35 sweep is not optional.** At 15:30 the 15:25 five-minute, 15:15
fifteen-minute and 15:15 hourly bars have only just closed. Without a later run
they would stay partial in the database permanently.

**The index is stored before the future is resolved.** Resolving the future
costs an extra API call. If it fails, the exception still propagates — but the
index candles are already committed rather than lost alongside it.

There is no holiday gate, for the same reason as the daily read: on a holiday
the fetch returns nothing new and nothing is written.

## Strategy routing — on each snapshot, 25× a day

No schedule. `intraday-market-sentiment` invokes `strategy-manager` once its
row is committed, and the manager invokes the playbooks the regime allows.

```mermaid
flowchart TD
    START(["intraday-market-sentiment<br/><b>Event invoke, the snapshot as payload</b>"]) --> VAL{"snapshot carries<br/>snapshot_ts,<br/>security_id,<br/>instrument_type?"}
    VAL -->|no| RAISE(["raise"])
    VAL -->|yes| ASOF["<b>as_of = snapshot_ts + 5 min</b><br/>every read bounded by this,<br/>never by the wall clock"]

    ASOF --> TOK["read /algo/dhan/token"]
    TOK --> RES["resolve instrument<br/><b>on (security_id, instrument_type)</b>"]
    RES --> C15["candle_15min: newest 260 CLOSED bars<br/><i>candle_ts + interval &le; as_of</i>"]

    C15 --> FRESH{"newest closed bar<br/>within 1 bar<br/>of the snapshot?"}
    FRESH -->|"no - stalled loader"| RAISE
    FRESH -->|"yes - or the :35 write/read race"| BARS{"&ge; 200<br/>closed bars?"}
    BARS -->|no| RAISE
    BARS -->|yes| IND["SMA 20/50/100/200, Wilder RSI"]

    IND --> MISS{"any of sma20/50/100/200,<br/>rsi absent on the<br/>newest bar?"}
    MISS -->|yes| RAISE
    MISS -->|no| CLS["<b>classify</b><br/>score -3..+3, bias, regime,<br/>confidence"]

    CLS --> C5["candle_5min: newest 200 closed bars"]
    C5 --> DLY["daily_market_sentiment row<br/>+ Wilder true ATR14 from candle_daily"]
    DLY --> LIVE["Dhan /v2/charts/intraday<br/><b>keeps the forming bar</b> - the live price"]

    LIVE --> REG{"regime in<br/>STRATEGY_REGISTRY?"}
    REG -->|"no - drift"| RAISE
    REG -->|"TREND &rarr; empty list"| NONE(["no playbook is valid<br/>on this kind of day"])
    REG -->|"RANGE"| SIZE{"context<br/>&le; 256 KB?"}
    SIZE -->|no| RAISE
    SIZE -->|yes| INV["<b>Event invoke</b> each eligible playbook<br/><i>all attempted, failures raised together</i>"]

    INV --> GATE["strategy-range-liquidity-sweep<br/><b>asserts context_version</b>"]
    GATE --> FINE{"regime RANGE<br/>VIX within &plusmn;5%<br/>adr &lt; 0.78 &times; ATR14<br/>opening range &ge; 0.15%<br/>09:45-15:00?"}
    FINE -->|"any fails"| DOWN(["stand down,<br/>naming every failed check"])
    FINE -->|all pass| POOLS["8 pools: 15min H/L, PDH/PDL,<br/>1Hr H/L, session H/L"]
    POOLS --> SCAN["replay the session's sweeps in order:<br/>penetration 0.04-0.30%, rejection,<br/>no extension, reclaim"]
    SCAN --> BROKE{"a level<br/>accepted through?"}
    BROKE -->|yes| DEAD(["both sides dead -<br/>the range no longer exists"])
    BROKE -->|no| RR{"RR &ge; 1.5 against T2?"}
    RR -->|no| REJ(["rejected, with the reason"])
    RR -->|yes| OUT{{"SWEEP CANDIDATE<br/>entry, stop, T1/T2/T3, grade<br/><i>to the log</i>"}}

    style RAISE fill:#7f1d1d,color:#fff
    style NONE fill:#78350f,color:#fff
    style DOWN fill:#78350f,color:#fff
    style DEAD fill:#78350f,color:#fff
    style REJ fill:#78350f,color:#fff
    style OUT fill:#14532d,color:#fff
```

Six things in that diagram are load-bearing:

**`as_of` comes from the snapshot, not the clock.** Every candle read is
bounded by the instant the snapshot's own 5-minute bar closed, so two runs over
the same snapshot see the same bars and reach the same decision. That is what
makes re-running the manager meaningful rather than merely repeated.

**The freshness check allows exactly one bar, and only because of a race.**
`intraday-data-loader` fires every five minutes and
`intraday-market-sentiment` at :35/:50/:05/:20, so the loader's write of the
bar the snapshot describes and the manager's read of it fall in the same
minute. Demanding equality would raise on the ordinary case. Beyond one bar it
is not a race but a stalled loader, and the SMAs would then be computed from a
series that ends before the market does.

**A missing SMA raises rather than defaulting to zero.** The legacy builder ran
every indicator through a `safe_value()` mapping NaN to `0.0`, which makes
`close > sma100 > sma200` read as `close > 0 > 0` — False — so the longer-SMA
test silently contributes nothing and the score caps at ±2. Nothing raises and
nothing looks wrong. This is the same defence `daily_market_sentiment` already
carries as `sma200 NOT NULL`.

**An unknown regime raises; a regime mapping to `[]` does not.** Those are
different things. `TREND → []` is a deliberate "no playbook is valid today",
which is the correct answer for every playbook built so far. A regime *missing
from the map* means `classify.py` and the registry have drifted apart, and
routing nothing would otherwise look exactly like a correct stand-down.

**Both gates run, and that is not redundancy.** The manager's registry answers
"is this playbook valid for this kind of day at all"; the playbook's own gate
answers what only it can know. A strategy that trusted an upstream gate it
cannot see would fire on a bad day the moment that gate moved.

**Acceptance kills both sides, not just one.** Two consecutive 5-minute closes
beyond a level means the range broke — and if the opening-range high has been
accepted through, a later sweep of the range low is not a range trade either,
because there is no longer a range. Measured on the real 2026-09-11 session:
the high was accepted through at 10:10, which stands down the otherwise-
qualifying 10:20 sweep.

## How a failure reaches you

Every row in the table below ends in a raised exception, which is the point:
raising is what makes Lambda record an error and write a traceback to the log.
A function returning a `{"statusCode": 500}` shape would count as a **success**
— no error metric, no log line worth matching, nothing to alert on. The "fail
loudly" rule is what makes the alerting below possible at all.

```mermaid
flowchart LR
    F1["auth-dhan-broker"] --> LG1[/"log group"/]
    F2["daily-market-sentiment"] --> LG2[/"log group"/]
    F3["instrument-master-loader"] --> LG3[/"log group"/]
    F4["intraday-data-loader"] --> LG4[/"log group"/]
    F5["intraday-market-sentiment"] --> LG5[/"log group"/]
    F6["strategy-manager"] --> LG6[/"log group"/]
    F7["strategy-range-liquidity-sweep"] --> LG7[/"log group"/]

    LG1 & LG2 & LG3 & LG4 & LG5 & LG6 & LG7 -->|"subscription filter<br/>?ERROR ?Traceback<br/>?Task timed out<br/>?Unable to import"| EN["error-notifier"]
    EN --> TG(["Telegram"])
    EN -.->|"never subscribe<br/>its own log group"| EN

    style TG fill:#14532d,color:#fff
```

A log subscription rather than a `try/except` inside each function, because a
**timeout** kills the process before any `except` runs and an **import error**
fires before the handler module loads — the two failures least likely to be
noticed, and the two a catch block can never see. Both still reach the log.

`error-notifier`'s own log group is deliberately not subscribed: it would log,
trigger itself, and loop. The handler refuses payloads from its own log group so
the mistake is inert rather than expensive.

**Subscribing every log group is not optional for the chained plane.** The two
strategy functions are invoked asynchronously, so nothing upstream fails when
they do: `intraday-market-sentiment` returning success says only that its row
was written and the invoke was accepted. Their own log groups are the only
place their failures appear. An unsubscribed `strategy-manager` would stop
routing and no alarm would fire anywhere.

This compounds with the execution-role trap. A borrowed role allows
`logs:PutLogEvents` on one log group ARN only, so a function using it produces
**no logs at all** — and for `strategy-range-liquidity-sweep`, whose log *is*
its entire output, that means it produces nothing whatsoever while appearing to
succeed.

**Two gaps remain.** Nothing watches the notifier itself, and nothing detects
*silence* — a schedule that stops firing raises no error because nothing runs.
The chained plane widens that second gap: a manager that is never invoked, or
a playbook never dispatched because the registry lost its regime, is silent in
exactly the same way as a quiet market.

## Failure handling

| Failure | Detected by | Result |
|---|---|---|
| Dhan renames a CSV column | column presence check | raise — no silent 0-row load |
| CSV unreachable / slow | 120 s download timeout | raise |
| Duplicate PK inside a batch | dedupe before batching | prevented |
| Any batch rejected | `ROLLBACK`, then raise | nothing committed |
| Neon compute suspended | — | first `connect()` absorbs the wake-up |
| TOTP generation fails | non-200 from `/app/generateAccessToken` | raise — no token exists |
| Dhan refuses with a 200 envelope | no `accessToken` in body | raise, quoting the body |
| `intraday-data-loader` has stalled | newest closed bar > 1 bar behind the snapshot | raise — no routing on stale SMAs |
| Fewer than 200 closed 15-min bars | `MIN_BARS_TO_CLASSIFY` | raise — not a score capped at ±2 |
| An SMA absent on the newest bar | explicit `None` check | raise — never defaulted to `0.0` |
| `classify.py` and the registry drift apart | regime absent from `STRATEGY_REGISTRY` | raise — not an empty shortlist |
| The dispatched context exceeds 256 KB | checked before `invoke` | raise, naming the candle count |
| A strategy invoke is refused | `StatusCode` / `FunctionError` | every invoke attempted, then one raise naming all failures |
| The manager's payload shape changes | `context_version` assertion in each strategy | raise — never a gate passing on a vanished input |
| A strategy fails after dispatch | its own log group → `error-notifier` | reported from there; the manager cannot see it and does not claim to |
| Token JWT has no `exp` claim | claim check before write | raise — nothing stored |
| Dhan token expired | `expires_at` check before any call | raise — never call with a dead token |
| `security_id` matches 0 or 2+ rows | exactly-one check in `resolve_instrument` | raise — no silent wrong instrument |
| Opening candle missing | first 15-min candle stamped ≠ 09:15 | raise — opening range would be wrong |
| Fewer than 200 daily candles | `sma200 is None` before classifying | raise, and `NOT NULL` in the schema |
| Chart arrays disagree in length | per-field length check in `to_candles` | raise |
| Dhan rate limit `DH-904` | retry with exponential backoff, then raise | — |
| Stray post-close candles | 09:15–15:30 session filter | dropped |
| Dhan changes interval alignment | every bar checked against the 09:15 origin | raise — the schedule no longer matches the data |
| Expiry list empty, or every date past | check before deriving the month | raise — cannot resolve the future |
| Futures contract absent from the master | exactly-one check in `resolve_by_symbol` | raise, naming the stale instrument master |
| No Dhan client id available | checked before the expiry call | raise |
| Candle table name not one of the three | whitelist check before any query | raise |
| Newest bar left partial at the close | the 15:35 closing sweep | prevented |
| Resume window exceeds Dhan's 90-day cap | clamped before the call | prevented |
