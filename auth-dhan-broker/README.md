# auth-dhan-broker

AWS Lambda function that keeps a live DhanHQ v2 access token in SSM Parameter
Store, so the session functions never authenticate themselves and nothing is
refreshed by hand.

It sits **outside** the History → Regime → Strategy state machine, on its own
EventBridge Scheduler cron — for the same reason `instrument-master-loader`
does: it runs on a different clock from the trading session, and a token
refresh failure should raise its own alarm rather than fail a session.

## What it does

1. Skips a weekend. Only reachable by hand — the cron is `MON-FRI`.
2. Asks `algo.trading_holiday` whether today is a closure.
3. **On a holiday** — disables all three session schedules
   (`daily-market-sentiment-daily`, `intraday-data-loader-session`,
   `intraday-data-loader-close`), sends a Telegram notice, and stops. No token
   is minted.
4. **Otherwise** — generates a TOTP code from the stored authenticator seed,
   calls `POST /app/generateAccessToken` with client id + PIN + that code,
   reads the expiry out of the returned JWT's `exp` claim, writes token and
   expiry to `/algo/dhan/token`, and **then** enables those three schedules.

Any failure raises. There is no fallback, because there is nothing to fall back
to — see below.

## Why the calendar decision lives here

This is the only thing that runs before the session on **every** weekday, and a
holiday *is* a weekday, so the `MON-FRI` cron still fires on one. That makes a
single symmetric decision point: disable on a holiday, enable on a trading day.
The session functions are never invoked on a holiday, rather than invoked and
skipping.

Concentrating the decision here costs little, because this function is already
a hard dependency of the whole day: every consumer calls `read_token_record()`,
which **raises** on a missing or expired token. A morning where this function
fails is already a dead day, and `error-notifier` already reports it. Attaching
the schedule decision to it adds no new way to lose a session.

**A row in `algo.trading_holiday` means closed; no row means a normal session.**
Absence is the permissive answer on purpose — a calendar nobody reseeded keeps
the system trading, which costs a few no-op invocations, instead of making it go
quiet, which nothing here can detect. See
[trading-calendar](../trading-calendar/README.md).

### The three schedules, and the one that is never touched

`MANAGED_SCHEDULE_NAMES` must never list **this function's own schedule**. It is
the heartbeat that makes the decision, so a run that switched it off could never
switch it back on, and the system would stay dark until someone noticed by hand.

It lists three, not two: `daily-market-sentiment-daily` plus **both** of the
loader's rules, `intraday-data-loader-session` and `intraday-data-loader-close`.
Missing the `-close` rule would leave the 15:00–15:35 runs armed on a holiday.

**The code default used to name no real schedule** — it read
`daily-market-sentiment,intraday-data-loader`, which are function names, not
schedule names, and would have raised on `GetSchedule`. It was never exercised,
because the environment variable is always set, and it failed loudly rather
than quietly; it was corrected to the three schedule names on 2026-09-14 so the
deployed configuration is reproducible from the code alone.

Order matters: the token is stored *before* the schedules are enabled. The other
way round arms a session against a token that was never refreshed, turning a
recoverable auth failure into a whole day of failing invocations.

### The holiday notice

On a holiday the function sends one Telegram message to the same chat as the
daily brief — which is the message it replaces that morning:

```
Market holiday - Mon 14 Sep 2026

Ganesh Chaturthi

No access token minted.
Session schedules disabled:
  daily-market-sentiment-daily (updated)
  intraday-data-loader-session (updated)
  intraday-data-loader-close (updated)

Next session: Tue 15 Sep 2026
```

That is the notice as actually sent on 2026-09-14, the first holiday this gate
saw — one line per managed schedule, so all three appear.

Most rows have no name, and the notice says `unnamed holiday` rather than
guessing — see [trading-calendar](../trading-calendar/README.md).

**Next session is looked up on the connection already open**, because Neon
autosuspends and this is the cold wake-up of the day. It walks forward from
tomorrow, skipping weekends and stored closures, which is how Diwali 2025
(21–22 October, two consecutive weekdays) resolves to Thursday the 23rd. It
decides nothing; only the notice reads it.

If no open weekday is found within `NEXT_SESSION_HORIZON_DAYS`, the notice says
the calendar may need reseeding instead of naming a date. That is the one
condition this design cannot otherwise see, so it is worth the line.

**The notice is sent after the schedules are switched off,** and a Telegram
failure raises. The gate is the job; a Telegram outage must never leave the
schedules armed on a holiday. One consequence: if the run is retried after
failing past that point, the notice is sent twice. A duplicate message is a
cheaper problem than a suppressed one, so it is not deduplicated.

### There is deliberately no calendar check inside the session functions

Do not add one. It would be worse than nothing.

The schedule gate is not the only thing stopping a holiday run — **the missing
token is**. No token is minted on a holiday, and the previous trading day's
token expires at 08:00 that same morning (lifetime is exactly 86,400 s from
08:00, measured), so it is already dead by the time any session function would
run. `read_token_record()` raises on an expired token, and `error-notifier`
reports it, with repeats suppressed for 1800 s so a stuck schedule produces a
handful of alerts rather than one per invocation.

That makes a failed toggle **loud**. A calendar check inside the handlers would
make it silent instead: the schedule would sit wrongly enabled, the handler
would skip politely, and nothing would ever tell you the gate had stopped
working. The second line of defence would hide the failure of the first.

### `UpdateSchedule` replaces, it does not patch

EventBridge **Scheduler** has no `EnableSchedule`/`DisableSchedule` pair — that
is EventBridge **Rules**, a different service. Every field not sent back to
`UpdateSchedule` is dropped, so a hand-built payload silently discards whatever
the schedule was created with: its timezone, retry policy, flexible time window.
The code reads the definition with `GetSchedule` and returns it whole with only
`State` changed, removing just the four keys `UpdateSchedule` rejects (`Arn`,
`CreationDate`, `LastModificationDate`, `ResponseMetadata`).

The response carries **metadata only** — status, expiry, elapsed — never the
token. Consumers read the token from the parameter. Returning it would only
have served an on-demand invoke from `daily-market-sentiment`, and that path
cannot fire in practice: the 08:00 refresh precedes that function's 09:50 run
by nearly two hours and the token lives 24 hours. All a returned token achieved
was putting a live credential on the console Test screen.

## Why there is no renew path

`/v2/RenewToken` exists, and on paper it would let one TOTP login carry a whole
week. It refuses tokens minted this way. Measured 2026-09-10:

```
HTTP 500  DH-905  INVALID_REQUEST
"Renewal of token not allowed for this token type"
```

Renewal is offered only for tokens generated by hand from Dhan Web. Seeding the
parameter once from the web portal and renewing onward does not rescue it
either: the chain breaks every weekend, Monday falls back to TOTP, and a
TOTP-minted token cannot be renewed — so it degrades to this same design after
the first Saturday, having added a manual step on the way.

The branch was built, tested against the live API, and removed. **Do not add it
back without new evidence from Dhan.**

One earlier symptom is worth recording, because it looked like the same problem
and was not. Sending `RenewToken` as POST with an empty body and
`Content-Type: application/json` returned `DH-905 "Missing required fields, bad
values for parameters"` — a malformed *request*, not a rejected token. Dhan's
prose calls the endpoint a POST, but its own worked curl passes neither `-d`
nor `-X POST`, which makes it a GET. Only after switching to GET did the real
`"not allowed for this token type"` answer surface.

## Schedule — once each weekday morning

`cron(0 8 ? * MON-FRI *)`, timezone `Asia/Kolkata`.

A token lives exactly 24 hours (86,400 s, measured), so the 08:00 token covers
the 09:15–15:30 session with hours to spare. Weekends are skipped because
nothing trades then; Monday mints a fresh token like any other weekday.

That costs five TOTP logins a week. An earlier design ran every 12 hours so a
renew could carry the intervening runs — with renew gone, the second daily run
bought nothing and was removed.

## Target

| | |
|---|---|
| Parameter | `/algo/dhan/token` |
| Type | `SecureString` (KMS-encrypted at rest) |

Stored as a single JSON blob:

```json
{
  "access_token": "eyJ...",
  "expires_at": 1789134580,
  "refreshed_at": 1789048180,
  "source": "totp"
}
```

**One parameter, not two.** Token and expiry are written together so they
cannot drift apart — a torn write leaving a new token beside an old expiry
would look healthy and behave wrongly.

**`expires_at` is epoch seconds**, read from the JWT's own `exp` claim, never
from the `expiryTime` string the API also returns.

That string carries **no timezone marker, and it is IST**. Measured on the
first live run, 2026-09-10:

| | |
|---|---|
| JWT `exp` | `1789134580` = 2026-09-11 13:49:40 UTC |
| Reported `expiryTime` | `2026-09-11T19:19:40.019` |
| Read as UTC | **+19,800 s — 5.5 hours too late** |
| Read as IST | 0 s — exact match |

Parsing that string as UTC would leave every token looking alive for 5.5 hours
after it died, with `daily-market-sentiment` handing a dead token to Dhan and
nothing reporting it. The string is logged for diagnostics only. If the `exp`
claim cannot be read, the function raises rather than storing a guessed expiry.

## Configuration

| Environment variable | Required | Default |
|---|---|---|
| `DHAN_CLIENT_ID` | yes | — |
| `DHAN_PIN` | yes | — |
| `DHAN_TOTP_SECRET` | yes | — |
| `TOKEN_PARAMETER_NAME` | no | `/algo/dhan/token` |
| `TELEGRAM_PARAMETER_NAME` | no | `/algo/telegram/brief` |
| `NEON_PARAMETER_NAME` | no | `/algo/neon/connection` |
| `NEON_CONNECTION_STRING` | no | — (override; SSM is the source of truth) |
| `DHAN_GENERATE_TOKEN_URL` | no | `https://auth.dhan.co/app/generateAccessToken` |
| `HTTP_TIMEOUT_SECONDS` | no | `30` |
| `MANAGED_SCHEDULE_NAMES` | no | `daily-market-sentiment-daily,intraday-data-loader-session,intraday-data-loader-close` — the deployed value sets the same three explicitly |
| `SCHEDULE_GROUP_NAME` | no | `default` |
| `NEXT_SESSION_HORIZON_DAYS` | no | `10` |

`DHAN_PIN` and `DHAN_TOTP_SECRET` are **permanent, full-trading-authority
credentials** — unlike the token, which dies in 24 hours. Anyone who can read
them can place orders. They are held as function environment variables, which
are encrypted at rest but readable by anyone holding
`lambda:GetFunctionConfiguration`; keep that permission narrow on this
function.

`DHAN_TOTP_SECRET` is the base32 seed shown when you scan the QR at
**Dhan Web → DhanHQ Trading APIs → TOTP setup**. TOTP must be enabled there
before this function can work at all.

## IAM

| Action | Resource |
|---|---|
| `ssm:PutParameter` | the `/algo/dhan/token` parameter ARN |
| `ssm:GetParameter` | `/algo/neon/connection` and `/algo/telegram/brief` |
| `kms:Encrypt` | the key backing the token parameter, via `kms:ViaService` |
| `kms:Decrypt` | the key backing those two read parameters |
| `scheduler:GetSchedule` | each managed schedule ARN |
| `scheduler:UpdateSchedule` | each managed schedule ARN |
| `iam:PassRole` | each managed schedule's **own** execution role |
| `logs:*` | standard Lambda logging |

Three of these are easy to get wrong.

**`iam:PassRole` is not optional.** `UpdateSchedule` re-passes the schedule's
target role, so without it every toggle fails `AccessDenied`. That failure is at
least loud.

**`ssm:GetParameter` and `kms:Decrypt` are back.** They had been removed from
this function on the grounds that it only writes. Reading the holiday calendar
means reading the Neon connection string, so both return.

**Scope to the two schedule ARNs, and widen this function's own policy** — never
attach a role built for another function. Both console-generated roles in this
project are scoped to a single resource and fail *silently* when borrowed: a
shared execution role produces a function that runs and writes but emits no logs
at all, and a shared Scheduler role produces a schedule that shows `ENABLED` and
never fires.

## Cost

Zero, with headroom. Two things keep it that way.

**Use a standard parameter, and leave higher throughput off.** Standard
parameters carry no storage charge and no API interaction charge — but enabling
the *higher throughput* setting starts billing them at $0.05 per 10,000
interactions. The default is off; leave it there. Advanced parameters
($0.05/parameter/month) are for values over 4KB; the token record is around
1KB, so it must also stay under 4KB to remain standard.

**KMS requests stay inside the free tier.** `SecureString` uses the AWS-managed
`aws/ssm` key, which has no monthly charge. Requests are $0.03 per 10,000 with
the first 20,000 per month free:

| | Runs/month | KMS ops each | Requests |
|---|---|---|---|
| `auth-dhan-broker` | 1/day × ~22 weekdays | encrypt | 22 |
| `daily-market-sentiment` | 1/day × ~22 weekdays | 3 decrypts | 66 |
| `instrument-master-loader` | 1/month | 1 decrypt | 1 |
| | | | **~89 of 20,000** |

An earlier version of this table assumed a consumer running every 5 minutes
(1,650 decrypts). That cadence belongs to the intraday function, which does not
exist yet; when it does, at 75 runs/day it would add ~1,650 and the total would
still be inside the free tier.

Figures from [Systems Manager
pricing](https://aws.amazon.com/systems-manager/pricing/) and [KMS
pricing](https://aws.amazon.com/kms/pricing/), checked 2026-09-10.

## Reserved concurrency: not set, and cannot be

An earlier version of this README specified `1`. **The account cannot reserve
concurrency at all.** AWS requires at least 100 *unreserved* concurrent
executions to remain available, and this account is on the default limit of
100 — so reserving even one leaves 99 and is refused with *"The unreserved
account concurrency can't go below 100."* That is an account-wide limit, not
something about this function: no Lambda here can reserve concurrency until
the *Concurrent executions* quota (`L-B99A9384`) is raised. Measured
2026-09-14.

What it would have bought: two concurrent runs would each burn a TOTP login
and race to write the parameter, last write winning. Nothing corrupts — the
record is a single JSON blob, so there is no torn write — it is just wasted
work against an endpoint we would rather not hammer.

What actually keeps that from happening is the schedule. One `cron(0 8 ? *
MON-FRI *)` run a day cannot overlap itself, so the only way to get two
concurrent runs is to invoke by hand while the scheduled one is in flight. If
you need to test during the 08:00 minute, wait for it to finish rather than
reaching for this setting.

## Deployment shape

- Runtime: Python 3.14, handler `handler.lambda_handler`.
- **Two layers: `neon-db-driver` and `neon-access`.** This function needed none
  until it began reading the holiday calendar, and that is the whole cost of the
  change — both ARNs are pinned and must be repointed together when either is
  republished. `boto3` is still pre-installed in the runtime; everything outside
  the layers is stdlib.
- The function package is seven modules: `handler.py` (the order things happen
  in), `config.py`, `params.py` (SSM), `dhan.py` (TOTP + auth), `db.py` (the
  holiday reads), `notify.py` (Telegram) and `schedules.py` (EventBridge
  Scheduler). It was one file until the holiday gate and the notice roughly
  doubled it; the split matches the layout every other function here uses.

**Upload a zip rather than pasting into the console editor.** A browser paste of
this file silently truncated at line 44 of 350, producing
`unterminated triple-quoted string literal` — the editor accepted a partial file
without complaint. A zip upload cannot arrive truncated.

## Deploy

All console. Placeholders: `<account>` = AWS account id, `<region>` = e.g.
`ap-south-1`. Both appear in any resource ARN — colon-separated field 4 is the
region, field 5 the account.

### 0. Enable TOTP at Dhan

**Dhan Web → My Profile → DhanHQ Trading APIs → TOTP setup.** Scan the QR into
an authenticator app and **keep the base32 seed shown at scan time** — that is
`DHAN_TOTP_SECRET`, and it is not displayed again.

### 1. IAM policy

**IAM → Policies → Create policy → JSON tab**, replacing `<region>` and
`<account>`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TokenParameter",
      "Effect": "Allow",
      "Action": ["ssm:PutParameter"],
      "Resource": "arn:aws:ssm:<region>:<account>:parameter/algo/dhan/token"
    },
    {
      "Sid": "SsmDefaultKey",
      "Effect": "Allow",
      "Action": ["kms:Encrypt"],
      "Resource": "*",
      "Condition": {
        "StringEquals": { "kms:ViaService": "ssm.<region>.amazonaws.com" }
      }
    }
  ]
}
```

Name it `auth-dhan-broker-token-access`.

The parameter is named `/algo/dhan/token` with a leading slash, but in the ARN
that slash is **not** doubled — `parameter/algo/dhan/token`. Writing
`parameter//algo/dhan/token` produces an access-denied that is annoying to
trace.

The KMS statement uses `Resource: "*"` with a `kms:ViaService` condition rather
than naming the key: an IAM policy cannot reference the `alias/aws/ssm` managed
key by alias ARN.

### 2. IAM role

**IAM → Roles → Create role.**

| Field | Value |
|---|---|
| Trusted entity type | AWS service |
| Use case | Lambda |
| Permissions | `auth-dhan-broker-token-access` **and** `AWSLambdaBasicExecutionRole` |
| Role name | `auth-dhan-broker-role` |

### 3. Create the function

**Lambda → Functions → Create function → Author from scratch.**

| Field | Value |
|---|---|
| Function name | `auth-dhan-broker` |
| Runtime | Python 3.14 |
| Architecture | either — `arm64` is cheaper and works, the package is pure Python |
| Execution role | *Change default execution role* → **Use an existing role** → `auth-dhan-broker-role` |

### 4. Upload the code

Zip the seven `.py` files, then **Code tab → Upload from → .zip file**. They
must sit at the archive root, not inside a folder — Lambda imports them as
top-level modules, and `handler.py`'s name must match the handler setting
(`handler.py` for `handler.lambda_handler`).

Don't paste into the inline editor — see [Deployment shape](#deployment-shape).

### 5. Set the handler

**Code** tab → **Runtime settings** → **Edit** → Handler:
`handler.lambda_handler`.

Under the **Code** tab, *not* Configuration → General configuration. Two
similar-looking failures mean opposite things:
`Handler 'handler' missing on module 'handler'` — file found, wrong function
name. `Unable to import module 'handler': No module named 'handler'` — no file
by that name in the package.

### 6. General configuration

**Configuration → General configuration → Edit.** Memory **256 MB**, timeout
**1 min**. Observed usage is ~100 MB, so the 128 MB default is too tight.

No VPC — the function needs the public internet to reach Dhan.

### 7. Environment variables

**Configuration → Environment variables → Edit.**

| Key | Value |
|---|---|
| `DHAN_CLIENT_ID` | your Dhan client id |
| `DHAN_PIN` | your 6-digit PIN |
| `DHAN_TOTP_SECRET` | base32 seed from step 0 |

### 8. Test

The SSM parameter does not need creating by hand — the first successful run
creates it. If you do create it yourself, use type **SecureString**, KMS key
`alias/aws/ssm`, and value `{}` as a placeholder.

**Test** tab → **Create new event** → name `bootstrap`, event JSON `{}` →
**Save** → **Test**.

Expect `"status": "success"`, `"source": "totp"`, and `expires_in_hours` of 24.
The response is safe to copy — it holds no token.

### 9. Verify what was stored

**Systems Manager → Parameter Store → `/algo/dhan/token`.** The **Overview** tab
shows *Last modified* and the type without revealing anything. Only use **Show
decrypted value** if you need to inspect it — that puts a live credential on
screen.

### 10. Schedule

**Amazon EventBridge → Scheduler → Schedules → Create schedule.**

| Field | Value |
|---|---|
| Name | `auth-dhan-broker-daily` |
| Occurrence | Recurring schedule |
| Schedule type | Cron-based schedule |
| Cron expression | `0 8 ? * MON-FRI *` |
| Timezone | `Asia/Kolkata` |
| Flexible time window | **Off** |

The form previews the **next 10 trigger dates** — check them before continuing.
They must read 08:00, Monday to Friday only. That preview is the reliable way to
confirm the expression whatever wrapper format the field expects.

Next page: target **AWS Lambda → Invoke** → `auth-dhan-broker`.

Settings page:

| Field | Value |
|---|---|
| Schedule state | Enabled |
| Retry attempts | `2` |
| Maximum age of event | `1 hour` |
| Permissions | **Create new role for this schedule** |

`Flexible time window: Off` matters — the 08:00 run has to land before the 09:15
market open, and a flexible window would let it drift.

Retries are capped at 2 rather than the default 185: a transient network failure
is worth retrying, but a broken TOTP seed should surface as an alarm within the
hour instead of retrying all day.

### 11. Confirm the schedule fired

After the next 08:00 IST slot, **Lambda → auth-dhan-broker → Monitor → View
CloudWatch logs**.

A healthy line reads `dhan token refreshed: {...}`. The token itself is never
logged — only its 12-character prefix.

## Local verification

The package imports `boto3` and `neon_access`, which the runtime and the
neon-access layer supply. Stub both the way CLAUDE.md stubs `pg8000`, and put
the package directory on `sys.path` so the modules can import each other:

```python
import sys, types
b = types.ModuleType("boto3"); b.client = lambda *a, **k: FakeClient()
sys.modules["boto3"] = b
n = types.ModuleType("neon_access")
n.SSL_CONTEXT = None; n.connect = lambda *a, **k: FakeConn()
n.get_parameter = lambda name: '{"bot_token": "t", "chat_id": "1"}'
n.read_neon_connection_string = lambda: "postgresql://stub"
n.today_ist = lambda: datetime.date(2026, 10, 21)
sys.modules["neon_access"] = n

sys.path.insert(0, "auth-dhan-broker")
import handler, dhan, notify
```

`handler` holds its own references — it does `from dhan import generate_token`,
not `import dhan` — so patch the network calls on `handler`, not on the module
they came from:

```python
handler.generate_token = lambda *a, **k: FAKE_JWT
handler.send_telegram = lambda text: sent.append(text)
```

The TOTP implementation is checkable without any credentials, against the
RFC 6238 test vectors:

```python
import base64
secret = base64.b32encode(b"12345678901234567890").decode()
assert dhan.totp_now(secret, at=59) == "287082"
assert dhan.totp_now(secret, at=1111111109) == "081804"
assert dhan.totp_now(secret, at=1234567890) == "005924"
assert dhan.totp_now(secret, at=2000000000) == "279037"
```

Covered before deploy: those four vectors, JWT expiry extraction and its two
failure modes, the three shapes Dhan returns on refusal (non-200, a 200 carrying
`{message, status}` and no token, and a non-JSON 200), that the response never
contains the token, and that a token with an unreadable expiry raises without
writing anything.

**Fake the `_post` function, not `urllib`** — Dhan's refusal bodies are the
thing worth asserting on, and they are what those tests replay.

## Verified against the live API, 2026-09-10

| | |
|---|---|
| TOTP generation | works, 0.56–0.71 s typical |
| Token lifetime | exactly 86,400 s |
| `expiryTime` timezone | IST, no marker — do not parse it |
| `RenewToken` | refuses TOTP-minted tokens |

One anomaly remains unexplained: a single run returned HTTP 200 with a
`{message, status}` envelope and no token, after 10.6 s rather than the usual
sub-second. It has not recurred. The suspicion at the time was rate limiting,
but nothing since supports that and repeated generation has been fine. The error
path now quotes Dhan's response body, so if it happens again the log will say
what it was.

Still unknown: **whether generating a token invalidates previously issued ones.**
It has not mattered so far, but it would decide whether two consumers could ever
hold their own tokens.

## Porting notes

Where this departs from the legacy `trading-algo` system:

1. **The token is fetched, not supplied.** Legacy read a pre-existing token from
   `DHAN_ACCESS_TOKEN` via `brokers/implementations/dhan/dhan_broker.py`,
   leaving the daily refresh a manual step. This function owns the refresh, so
   there is no manual step.
2. **No `dhanhq` SDK.** The endpoint is plain HTTP called with `urllib`. The SDK
   pulls in pandas/numpy, which is what pushed an earlier iteration of this
   project onto ECS Fargate.
3. **TOTP is inlined**, not taken from `pyotp` — RFC 6238 is about ten lines of
   `hmac`/`struct`, and this keeps the package stdlib-only.
