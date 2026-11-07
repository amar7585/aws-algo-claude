# pattern-recorder IAM (step 5, created by Amar by hand)

**Diffed 2026-11-06 against the live `pattern-detector-role` and mirrored to its shape:**
the same trust policy, and the same three-inline-policy layout with the same Sids. Only
the resources differ: this function's own log group, only the Neon parameter, and the
recorder's ARN.

| File | Where it goes |
|---|---|
| `trust-policy.json` | trust policy of the **new** role **`pattern-recorder-role`** (identical to the detector's) |
| `pattern-recorder-role.algo-ssm-read.json` | inline policy **`algo-ssm-read`** on `pattern-recorder-role`: only `/algo/neon/connection` (the detector's also lists `/algo/telegram/brief`; the recorder sends nothing) + `kms:Decrypt` via SSM only |
| `pattern-recorder-role.lambda-logs.json` | inline policy **`lambda-logs`** on `pattern-recorder-role`: `CreateLogGroup`, plus `CreateLogStream`/`PutLogEvents` on **its own** group `/aws/lambda/pattern-recorder` only |
| `pattern-detector-role.invoke-pattern-recorder.json` | a **new** inline policy **`invoke-pattern-recorder`** on the existing **`pattern-detector-role`**, beside `invoke-strategy-manager` (not edited into it): `lambda:InvokeFunction` on the recorder's ARN only |

**No managed policies are attached** (the detector has none either).

**Why the log scope matters:** a role whose log permission names another function's
group runs silently with **no logs**, so error-notifier never sees its failures
(CLAUDE.md). I verify the created role against these files before creating the
function.
