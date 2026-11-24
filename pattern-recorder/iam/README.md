# pattern-recorder IAM (created by Amar by hand)

**Diffed 2026-11-06 against the live `pattern-detector-role` and mirrored to its shape:**
the same trust policy, and the same three-inline-policy layout with the same Sids. Only
the resources differ: this function's own log group, its parameters, and its targets.

| File | Where it goes |
|---|---|
| `trust-policy.json` | trust policy of the role **`pattern-recorder-role`** (identical to the detector's) |
| `pattern-recorder-role.algo-ssm-read.json` | inline policy **`algo-ssm-read`** on `pattern-recorder-role`: `/algo/neon/connection` and **`/algo/dhan/token`** (added for v2.1), plus `kms:Decrypt` via SSM only. **`/algo/telegram/brief` removed 2026-11-23** with the recorder's Telegram code (it was added for v2.1) |
| `pattern-recorder-role.lambda-logs.json` | inline policy **`lambda-logs`** on `pattern-recorder-role`: `CreateLogGroup`, plus `CreateLogStream`/`PutLogEvents` on **its own** group `/aws/lambda/pattern-recorder` only |
| `pattern-recorder-role.invoke-risk-manager.json` | **P2b (2026-11-23 17:22 IST):** inline policy **`invoke-risk-manager`** on `pattern-recorder-role`: `lambda:InvokeFunction` on `risk-manager`'s ARN only. It **replaced** `invoke-strategy-manager` (removed the same minute), so the recorder cannot bypass the risk manager |
| `pattern-recorder-role.invoke-strategy-manager.json` | **REMOVED from the role 2026-11-23 (P2b)** - kept here as the rollback document.  **v2.1:** a **new** inline policy **`invoke-strategy-manager`** on `pattern-recorder-role`: `lambda:InvokeFunction` on `strategy-manager`'s ARN only. The same document as the detector's policy of that name |
| `intraday-fno-poller-role.invoke-pattern-recorder.json` | **v2.1:** a **new** inline policy **`invoke-pattern-recorder`** on the existing **`intraday-fno-poller-role`**, beside its managed `intraday-fno-poller-access` (not edited into it): `lambda:InvokeFunction` on the recorder's ARN only |
| `pattern-detector-role.invoke-pattern-recorder.json` | v2.0: inline policy **`invoke-pattern-recorder`** on `pattern-detector-role`. Unused once v2.1 runs on the poller (the detector's `PATTERN_RECORDER_FUNCTION_NAME` is unset). **Removed from the live role 2026-11-19 17:42 IST** (Amar's cleanup; the live document equalled this file). Kept here as the record — re-create it with `put-role-policy` only if the detector is ever set to hand off to the recorder again |

**No managed policies are attached** to `pattern-recorder-role` (the detector has none either).

**Why the log scope matters:** a role whose log permission names another function's
group runs silently with **no logs**, so error-notifier never sees its failures
(CLAUDE.md). I verify the created role against these files before creating the
function.

**Why each grant is needed, and how it would fail without it:**
- **`/algo/dhan/token`:** v2.1 reads today's candles from Dhan. Without the grant every run
  raises `AccessDenied` on the token read, before any detection.
- **`/algo/telegram/brief`** (v2.1 until 2026-11-23): no longer granted. The recorder sends
  nothing to Telegram since then.
- **Invoking the manager** (strategy-manager, risk-manager since P2b): without it the first manager dispatch raises, in the same
  way.
- **The poller invoking the recorder:** without it, setting the poller's
  `PATTERN_RECORDER_FUNCTION_NAME` makes every poller run raise *after* its row is
  committed. Retries are off, so the row stands, but nothing reaches v2.1. Grant first,
  then set the env var.
