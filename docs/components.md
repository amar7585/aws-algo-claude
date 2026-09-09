# Components

← [Back to root README](../README.md) · [Architecture](architecture.md) · [Flow charts](flow.md)

## Register

| Component | Kind | Cadence | Status | Docs |
|---|---|---|---|---|
| `instrument-master-loader` | Lambda function | monthly | **built, running** | [README](../instrument-master-loader/README.md) |
| `neon-db-driver` | Lambda layer | — | **built** | [README](../layers/neon-db-driver/README.md) |
| History task | Lambda function | per session | planned | — |
| Regime task | Lambda function | per session | planned | — |
| Strategy task | Lambda function | per session | planned | — |
| Session state machine | Step Functions | per session | planned | — |

Only the first two exist in this repo. The rest are recorded so the boundaries
are explicit; see [architecture.md](architecture.md) for why the loader is
deliberately outside the state machine.

## Built

### instrument-master-loader

Refreshes `algo.instrument_master` from Dhan's public scrip master.

| | |
|---|---|
| Entry point | `handler.lambda_handler` |
| Runtime | Python 3.12, zip package |
| Layer | `neon-db-driver` |
| Package contents | `handler.py` alone — everything else is stdlib |
| Dependencies | pg8000 (from the layer). No pandas, no `dhanhq`, no compiled wheels |
| Reads | `https://images.dhan.co/api-data/api-scrip-master.csv` (public, unauthenticated) |
| Writes | `algo.instrument_master` |
| Secrets | `NEON_CONNECTION_STRING` only — no broker credentials needed |

Full configuration, deployment notes and the known `FUTIDXBSE` rule gap are in
its [README](../instrument-master-loader/README.md).

### neon-db-driver layer

Carries [pg8000](https://github.com/tlocke/pg8000), the Postgres driver every
function here uses to reach Neon.

Chosen because it is **pure Python**: no libpq, no compiled extension. The layer
builds with a plain `pip install` on any machine, works unchanged on `x86_64`
and `arm64`, and stays inside Lambda's zip limits. psycopg2 would need a binary
matched to the Lambda runtime, and `psycopg2-binary` is explicitly not
recommended for production.

Build and publish steps are in its [README](../layers/neon-db-driver/README.md).

## Planned

Shapes are settled; none of the code is in this repo.

### History task

Fetches candles into `algo.candle_5min` and its aggregates. Gates on the NSE
trading calendar, resolves the current-month index future at runtime from
`instrument_master`, and fetches incrementally from the last stored
`candle_ts` rather than refetching the session.

Needs authenticated Dhan credentials, unlike the loader.

### Regime task

Classifies the session — trending, sideways, volatile expansion — from stored
candles plus structural levels. Reads only; writes its classification back for
the strategy step.

### Strategy task

Evaluates the playbooks valid for the classified regime and emits signals. It
never runs unconditionally: a playbook fired in the wrong regime is the main
way this loses money, so the regime gate comes first.

## Shared conventions

Any new component in this repo is expected to hold to these. They are covered
in more depth in [architecture.md](architecture.md#invariants).

- **Epoch seconds** for every stored time value.
- **Raw Dhan segment codes** in `exchange_segment`.
- **`(security_id, instrument_type)`** as the instrument identity.
- **No compiled dependencies** — pure Python or rethink the component.
- **Fail loudly.** Raise on error so Lambda records a failure; never return a
  500-shaped success. Never let a schema change degrade into a silent 0-row
  write.
- **Secrets from environment variables**, sourced from Secrets Manager or
  encrypted SSM at deploy time. Never committed.

## Source lineage

This is a rewrite, not a greenfield design. Two repos outside this one hold
context that is not derivable from the code here.

| Repo | What it holds |
|---|---|
| `trading-algo` | The legacy production system being ported. Source of truth for `INSTRUMENT_RULES` (`brokers/implementations/dhan/dhan_broker.py`), `ExchangeSegment` (`bin/enums/dhan.py`), and the monthly reload rule (`data/database_service.py`). |
| `AIML` | Architecture notes and the component register the naming here follows, plus a separate Postgres port of the legacy schema that targets a **different** Neon project and is not compatible with this one. |

Agreed scope for the loader is to replicate **all 9** `INSTRUMENT_RULES`, not to
narrow them to Nifty-only — even though most are irrelevant to index trading.
