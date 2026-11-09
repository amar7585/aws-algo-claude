# pattern-recorder IAM (created by Amar by hand)

**Diffed 2026-11-06 against the live `pattern-detector-role` and mirrored to its shape:**
the same trust policy, and the same three-inline-policy layout with the same Sids. Only
the resources differ: this function's own log group, its parameters, and its targets.

| File | Where it goes |
|---|---|
| `trust-policy.json` | trust policy of the role **`pattern-recorder-role`** (identical to the detector's) |
| `pattern-recorder-role.algo-ssm-read.json` | inline policy **`algo-ssm-read`** on `pattern-recorder-role`: `/algo/neon/connection`, **`/algo/dhan/token`** and **`/algo/telegram/brief`** (the last two added for v2.1), plus `kms:Decrypt` via SSM only |
| `pattern-recorder-role.lambda-logs.json` | inline policy **`lambda-logs`** on `pattern-recorder-role`: `CreateLogGroup`, plus `CreateLogStream`/`PutLogEvents` on **its own** group `/aws/lambda/pattern-recorder` only |
| `pattern-recorder-role.invoke-strategy-manager.json` | **v2.1:** a **new** inline policy **`invoke-strategy-manager`** on `pattern-recorder-role`: `lambda:InvokeFunction` on `strategy-manager`'s ARN only. The same document as the detector's policy of that name |
| `intraday-fno-poller-role.invoke-pattern-recorder.json` | **v2.1:** a **new** inline policy **`invoke-pattern-recorder`** on the existing **`intraday-fno-poller-role`**, beside its managed `intraday-fno-poller-access` (not edited into it): `lambda:InvokeFunction` on the recorder's ARN only |
| `pattern-detector-role.invoke-pattern-recorder.json` | v2.0: inline policy **`invoke-pattern-recorder`** on `pattern-detector-role`. Unused once v2.1 runs on the poller (the detector's `PATTERN_RECORDER_FUNCTION_NAME` is unset); it can be removed later |

**No managed policies are attached** to `pattern-recorder-role` (the detector has none either).

**Why the log scope matters:** a role whose log permission names another function's
group runs silently with **no logs**, so error-notifier never sees its failures
(CLAUDE.md). I verify the created role against these files before creating the
function.

**Why each grant is needed, and how it would fail without it:**
- **`/algo/dhan/token`:** v2.1 reads today's candles from Dhan. Without the grant every run
  raises `AccessDenied` on the token read, before any detection.
- **`/algo/telegram/brief`:** without it the first alert raises on the send. The claim then
  rolls back, so the alert is not lost: it goes out on the retry once the grant exists.
- **Invoking strategy-manager:** without it the first manager dispatch raises, in the same
  way.
- **The poller invoking the recorder:** without it, setting the poller's
  `PATTERN_RECORDER_FUNCTION_NAME` makes every poller run raise *after* its row is
  committed. Retries are off, so the row stands, but nothing reaches v2.1. Grant first,
  then set the env var.
