# pattern-detector v2 — offline replay

← [pattern-detector README](../README.md) · [v2 definitions](../v2-pattern-definitions.md) · [Phase A results](../v2-phase-a-replay.md)

Local, offline tooling for the v2 pattern library (`../structure.py`, `../patterns.py`).
**It is not part of the Lambda.** The detector's zip is built from an explicit module
list and never includes this folder. Nothing here writes to Neon or touches AWS beyond
one SSM read.

| File | What it does |
|---|---|
| `checks.py` | Hand-built logic checks, one or more per shape and family, plus a no-look-ahead test (detection on truncated bars must equal full-session detection). These synthetic bars test logic only, not evidence. |
| `fetch.py` | **Read-only** pull of the tables the replay needs into `data/`. It sets the session `READ ONLY` and verifies the target (database `Algo`, text `security_id`) before reading. |
| `replay.py` | Runs the library over every stored session and writes `out/replay_report.md` + `out/events.json`. |

`data/`, `driver/` and `out/` are git-ignored.

## Running it

From the repo root, in Git Bash:

```bash
python pattern-detector/replay/checks.py
```

```bash
python -I -m zipfile -e layers/neon-db-driver/neon-driver.zip pattern-detector/replay/driver
```

```bash
NEON_CONNECTION_STRING="$(MSYS_NO_PATHCONV=1 aws ssm get-parameter --name /algo/neon/connection --with-decryption --query Parameter.Value --output text --region ap-south-1)" python -I pattern-detector/replay/fetch.py
```

```bash
python pattern-detector/replay/replay.py
```

- **The driver is the repo's own `neon-db-driver` layer zip** (pg8000, pure Python),
  unpacked locally, so nothing is downloaded.
- **`MSYS_NO_PATHCONV=1` is required in Git Bash.** Without it, Git Bash rewrites
  `/algo/neon/connection` into a Windows path and SSM answers `ParameterNotFound`, which
  looks like a missing secret. Measured 2026-11-05.
- **The connection string only ever lives in that one process's environment.** It is
  never printed or written.
- An expired aws login fails as `Your session has expired`; run `aws login` again.

## What the replay assumes

- **The active future each day** is the stored `intraday_fno_data.fut_security_id`
  where rows exist (from 2026-09-15). Before that, it is the highest-volume contract.
- **Run clocks** are the stored `captured_at` where rows exist. Before that, they are
  simulated as each quarter-hour + 70 s (the measured median) from 09:45, plus 15:35.
- **Damaged bars:** the SEP future (68407) on sessions from 2026-09-11 has wrong
  open/high/low/volume on 24 of 75 bars a session (the old loader window bug; closes are
  right). Dhan serves no bars for an expired contract, so it cannot be repaired. Those
  sessions are reported separately, and events touching a damaged slot are flagged.
- **Today's session is skipped** (incomplete).
