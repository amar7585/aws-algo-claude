# error-notifier

AWS Lambda function that pushes failures from every other function in this
system to Telegram.

| | |
|---|---|
| Trigger | CloudWatch Logs subscription filters on the four other log groups |
| Invocations | **only on failure** — zero on a healthy day |
| Reads | `/algo/telegram/brief` |
| Writes | nothing |
| Layers | **none** — stdlib plus the runtime's boto3 |
| Database | none — it never touches Neon |

## Why a log subscription

Two alternatives were considered and rejected.

**A `try/except` in each function** cannot see the two failures that matter
most:

- a **timeout** kills the process, so no `except` ever runs — a hung Dhan call
  looks like silence
- an **import error** fires before the handler module loads at all, which is
  exactly what a detached layer produces (`No module named 'neon_access'`)

Both still reach the log. A subscription also touches **none** of the four
existing functions — no redeploys, no regression risk, no Telegram code copied
into four places, and one place to handle noise.

**A CloudWatch alarm** can only say `Errors >= 1`. The log event carries the
actual exception, so a message can name the instrument, the interval and the
HTTP status. Alarms also cost money per alarm; this costs nothing.

The filter pattern was validated against the live log groups before being
chosen — it matched the five real `AccessDenied` failures of 2026-09-11 and
none of the healthy `REPORT` lines:

```
?ERROR ?Traceback ?"Task timed out" ?"Unable to import"
```

## The loop guard

**This function's own log group must never be subscribed to this function.**
It would log, which would trigger itself, which would log — an unbounded loop
that bills for itself.

The README saying so is not enough, so `lambda_handler` refuses any payload
whose `logGroup` equals its own `context.log_group_name` and logs a warning
naming the subscription to delete. Creating the bad subscription is then inert
rather than expensive.

## Noise

A Dhan outage fails every intraday run. At 68 runs a day that is 68 identical
messages, so repeats of the same signature inside `SUPPRESSION_SECONDS`
(default 30 min) are dropped. The signature flattens request ids and numbers
first, so the same failure with a different request id still counts as a
repeat — verified: 68 identical failures produce **one** message.

**This is best effort, not a guarantee.** The window lives in a module global,
so it holds only while Lambda keeps the container warm; a cold start forgets
it. It turns a flood into a trickle rather than promising exactly one message.

A batch carrying several distinct failures is sent as one message, showing the
first `MAX_EVENTS_PER_MESSAGE` and a count of the rest.

## What it sends

```
[!] intraday-data-loader
12 Sep 2026 13:30:00 IST

[ERROR] AccessDeniedException: An error occurred (AccessDeniedException) when
calling the GetParameter operation: User: ... is not authorized to perform:
ssm:GetParameter on resource: arn:aws:ssm:ap-south-1:...:parameter/algo/dhan/token

stream 2026/09/12/[$LATEST]abc123
```

## What it does not catch

- **Its own failure.** Nothing watches the watcher. With Telegram as the only
  channel there is no second path — an SNS topic with an email subscription
  alongside would have covered it, and was deliberately not built.
- **Silence.** A schedule that stops firing produces no error because nothing
  runs. Detecting that means alarming on `Invocations < 1`, which needs to know
  when a run was *expected* — and this repo has no trading-holiday calendar, so
  it would cry wolf on every market holiday. Left as a known gap rather than
  half-solved.

## Layout

```
handler.py   entry point, decode, the loop guard, suppression, formatting
config.py    this function's tunables
notify.py    Telegram
```

**No `neon-access` layer, deliberately.** That layer's package imports pg8000
at module load, so attaching it would drag the database driver into a function
that never touches the database — and would require *both* layers just to
import. This is stdlib plus boto3, the same shape as `auth-dhan-broker`.

`notify.py` is a trimmed copy of `daily-market-sentiment/notify.py` rather than
a shared module, for the same reason: sharing it would mean putting it in that
layer. Twenty duplicated lines is the cheaper trade, and it keeps this function
independently deployable.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_PARAMETER_NAME` | `/algo/telegram/brief` | the same parameter the daily brief uses |
| `SUPPRESSION_SECONDS` | `1800` | repeat window, best effort |
| `SIGNATURE_CHARS` | `120` | how much of a normalised message keys a repeat |
| `MAX_EVENTS_PER_MESSAGE` | `5` | distinct failures shown per message |
| `MAX_MESSAGE_CHARS` | `3500` | Telegram caps a message at 4096 |
| `MAX_EVENT_CHARS` | `600` | per-event truncation |
| `HTTP_TIMEOUT_SECONDS` | `20` | |

## Local verification

No test suite and no build step. `boto3` comes from the runtime and is normally
absent locally, so stub it, then replace the sender:

```python
import sys, types
boto3 = types.ModuleType("boto3"); boto3.client = lambda *a, **k: None
sys.modules["boto3"] = boto3
sys.path.insert(0, "error-notifier")
import handler
handler.send_telegram = lambda text: print(text)
```

Build an event with `gzip.compress` + `base64.b64encode` over the CloudWatch
payload shape. `decode_payload()`, `signature()`, `should_send()` and
`format_message()` are pure. Pass an object with a `log_group_name` attribute
as `context` to exercise the loop guard.

## Deployment shape

| | |
|---|---|
| Handler | `handler.lambda_handler` |
| Runtime | Python 3.14, zip package |
| Layers | none |
| Package | the three `.py` files at the zip root |
| Timeout | 30 s |
| Memory | 128 MB |

IAM needs `ssm:GetParameter` + `kms:Decrypt` for `/algo/telegram/brief`.

Then, **per log group** — `auth-dhan-broker`, `daily-market-sentiment`,
`instrument-master-loader`, `intraday-data-loader`, and **never
`error-notifier` itself**:

```bash
aws lambda add-permission \
  --function-name error-notifier \
  --statement-id logs-intraday-data-loader \
  --principal logs.amazonaws.com \
  --action lambda:InvokeFunction \
  --source-arn 'arn:aws:logs:ap-south-1:709458364771:log-group:/aws/lambda/intraday-data-loader:*' \
  --source-account 709458364771

aws logs put-subscription-filter \
  --log-group-name /aws/lambda/intraday-data-loader \
  --filter-name error-notifier \
  --filter-pattern '?ERROR ?Traceback ?"Task timed out" ?"Unable to import"' \
  --destination-arn arn:aws:lambda:ap-south-1:709458364771:function:error-notifier
```

In the console the same thing is CloudWatch → Log groups → *(select)* →
Subscription filters → Create Lambda subscription filter. A filter sends one
`CONTROL_MESSAGE` on creation to prove it can reach the destination; the
handler ignores those.

## Cost

Effectively nothing, and measured rather than assumed. Subscription filters
carry no per-filter charge — the only cost AWS attaches to them is
**cross-region** data transfer, and everything here is in `ap-south-1`.

A subscription does **not** re-ingest anything: it reads log events already
being written and paid for. Measured ingestion across all four functions
projects to **2–3 MB/month** against a 5 GB free tier, at $0.50/GB beyond it.

The only genuinely new cost is this function's own invocations, which happen
only on failure — zero on a healthy day, and even a catastrophic 68-failure day
is 2,000 invocations a month against a 1M free-tier allowance.

## Log retention

All log groups are set to **30 days**. Cost is not the reason — at this volume
it is rounding error. It is that failures in this system are routinely noticed
late, through wrong rows rather than an alert, and the INFO lines are what
explain them. One day would have been too short: the filter pattern above was
validated by searching three days back.
