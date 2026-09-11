# neon-access layer

The plumbing every function in this repo needs, in one place: epoch/IST
handling, the Neon connection, and the shared parameter reads.

It is deliberately small. Only things used by **more than one function** belong
here — anything specific to a single function stays in that function's own
package. The rule has teeth because layer versions are pinned per function:
changing this means publishing a new version *and* repointing every function
that uses it, so churn here costs more than churn anywhere else.

## What's in it

| Module | Provides |
|---|---|
| `neon_access.clock` | `IST`, `ist_midnight_epoch`, `ist_datetime`, `today_ist`, `now_epoch` |
| `neon_access.neon` | `connect`, `SSL_CONTEXT` |
| `neon_access.ssm` | `get_parameter`, `read_neon_connection_string` |

All are re-exported from `neon_access`, so `from neon_access import connect`
works.

### Why these three and nothing else

**`clock`** — every stored time value in this system is epoch seconds, and
getting that wrong does not raise. It writes a plausible number five and a half
hours away from the truth. `ist_midnight_epoch()` verifies
`(epoch + 19800) % 86400 == 0` before returning and raises otherwise, so a
day-grain value built by hand upstream is caught rather than stored.

**`neon`** — pg8000 takes discrete kwargs rather than a `postgres://` DSN, and
Neon requires TLS. One parsing implementation, not three.

**`ssm`** — only the Neon connection string, because it is the one parameter
more than one function reads. The Dhan token and Telegram config stay with the
function that owns them.

Note the module is `ssm`, **not `secrets`**. A module named `secrets.py` shadows
the stdlib module of that name, and in a Lambda package the zip root is first on
`sys.path` — so it shadows it for boto3 too. This was not theoretical: a local
`secrets.py` broke `import secrets` during development.

## Relationship to `neon-db-driver`

Separate layers, both attached to any function that touches the database.
`neon-db-driver` carries the third-party pg8000 package; this one carries our
code. Keeping them apart means a pg8000 upgrade and a change to our helpers are
independent events.

## Building

Lambda expects a Python layer's packages under `python/` at the zip root. From
the repo root:

```bash
python -c "
import zipfile, os, glob
z = zipfile.ZipFile('layers/neon-access/neon-access.zip','w',zipfile.ZIP_DEFLATED)
for p in sorted(glob.glob('layers/neon-access/python/**/*.py', recursive=True)):
    z.write(p, os.path.relpath(p,'layers/neon-access').replace('\\\\','/'))
z.close()"
```

Publishing:

```bash
aws lambda publish-layer-version \
  --layer-name neon-access \
  --description "Shared epoch/IST, Neon connection and SSM reads" \
  --zip-file fileb://layers/neon-access/neon-access.zip \
  --compatible-runtimes python3.12 python3.13 python3.14 \
  --compatible-architectures x86_64 arm64
```

Pure Python, so it is architecture-independent.

## After publishing

**Publishing does not update any function.** Each function's layer ARN is
version-pinned, so every consumer has to be repointed explicitly. That is the
cost of this layer existing; it is the reason not to put churning code in it.

Consumers today:

| Function | Also needs |
|---|---|
| `daily-market-sentiment` | `neon-db-driver` |
| `instrument-master-loader` | `neon-db-driver` |

`auth-dhan-broker` does not use it — it touches neither Neon nor a shared
parameter.

## Local testing

Lambda mounts a layer's `python/` directory onto `sys.path`. Reproduce that by
putting the same directory on the path ahead of the function's own:

```python
sys.path.insert(0, "layers/neon-access/python")
sys.path.insert(0, "daily-market-sentiment")
```

Then stub `pg8000` and `boto3` as usual and import the function's modules.
