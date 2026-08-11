# Atlas engine

The engine owns source adapters, physical extraction, privacy-safe profiling, typed
checks, semantic claims, evidence, review state, and agent-facing semantic-view output.
It exposes the same pipeline through a CLI and FastAPI.

## Setup

```bash
uv sync
cp .env.example .env
# Set OPENROUTER_API_KEY. Add source credentials through the console or env.
```

Run the API:

```bash
uv run uvicorn atlas.api:app --reload
```

API documentation is available at `http://localhost:8000/docs`.

## CLI workflow

```bash
uv run atlas preflight
uv run atlas create-workspace elara-review elara
uv run atlas extract elara-review
uv run atlas summary elara-review
uv run atlas analyze elara-review --limit 3
uv run atlas claims elara-review
uv run atlas compile elara-review --markdown out/elara-review/report.md
```

Extraction is deterministic and does not call a model. Analysis uses the configured
OpenRouter model and the live source to run typed checks. Existing analyzed tables are
skipped unless `--regenerate` is provided.

## Adapter architecture

Core code depends on `DatabaseAdapter`, not PostgreSQL:

```python
test_connection()
probe(namespace)
extract_structure(namespace)
profile(snapshot)
execute_check(check)
close()
```

`PostgresAdapter` is the only complete adapter. It owns PostgreSQL connection behavior,
quoting, extraction SQL, profiling SQL, read-only transactions, timeouts, and typed-check
SQL.

`SnowflakeAdapter` supports connection probing, table/view reflection, comments,
declared keys, sampled profiling, typed checks, query tags, and session timeouts. Ordinary
Snowflake keys are recorded as declared but not enforced. Hybrid-table enforcement and
clustering-key metadata are not represented yet. Treat the adapter as early until it has
passed against the target account.

Snowflake password or programmatic-token URL:

```text
snowflake://USER:PASSWORD@ACCOUNT/DATABASE/SCHEMA?warehouse=WAREHOUSE&role=ATLAS_READER
```

For an interactive human login, `auth_method=mfa_push` passes
`authenticator=username_password_mfa` to the Snowflake connector and waits for the user
to approve the second factor. `auth_method=mfa_totp` additionally accepts a current TOTP
passcode for the initial probe. The code is never persisted; both MFA modes request the
connector's MFA token cache for later local connections. `auth_method=external_browser` is reserved for accounts
with a configured SAML identity provider; it creates a passwordless URL with
`authenticator=externalbrowser` and opens the system browser from the engine process.

Use a role with `USAGE` on the warehouse/database/schema and `SELECT` on the intended
objects. Set the source namespace to `DATABASE.SCHEMA`.

Adapters observe values and return `CheckObservation`. Database-independent policy in
`checks.py` computes the verdict, so two engines cannot interpret the same observation
differently.

## Typed validation

The analysis agent has no arbitrary SQL tool. It can propose only typed checks:

- `GrainCheck`
- `JoinCheck`
- `DistributionCheck`
- `NullabilityCheck`
- `OrderingCheck`

Atlas compiles the SQL, executes it through the adapter, and stores:

- the assertion made before execution;
- the observed values;
- complete-scan or sampled scope;
- verdict and limitations;
- query hash and database dialect;
- freshness and invalidation triggers;
- the hypothesis parameters proposed by the agent.

A check can establish structural consistency. It cannot establish business meaning by
itself.

Claim confidence is an evidence-derived trust score, not probability. It combines
directness, authority, coverage, consistency, and freshness. The complete factor
breakdown travels with newly evaluated claims; consequence remains a separate review-
priority signal.

## Sources and credentials

Declared sources live in `sources.yaml` and contain only identifiers and environment
variable names. In the deployment image this resolves to `/data/sources.yaml`, on the
same persistent volume as workspace state and secrets:

```yaml
sources:
  - id: elara
    adapter: postgresql
    url_env: ELARA_DATABASE_URL
    namespace: public
```

Connection strings come from either the declared source's environment variable or
`.secrets.env` when entered through the console. Extraction and analysis never accept a
per-run URL or namespace override; they resolve the immutable source binding from the
workspace manifest.

`.secrets.env`, `.env`, and workspace outputs are gitignored. Never commit customer
credentials or extracted customer metadata.

## Current workspace storage

Per-workspace state lives under `ATLAS_OUTPUT_DIR` (`out/` by default). The manifest
atomically points to one complete generation; prior generations remain recoverable:

| File | Current role |
|---|---|
| `workspace.yaml` | Immutable source binding and active snapshot generation. |
| `generations/<n>/snapshot.yaml` | Physical structure and profiles captured from the source. |
| `generations/<n>/facts.yaml` | Semantic claims and review decisions. |
| `generations/<n>/evidence.yaml` | Immutable observations and claim-evidence links. |
| `generations/<n>/questions.yaml` | Business questions and reviewer answers. |
| `generations/<n>/output.yaml` | Derived review/output document. Never edit directly. |

The API rebuilds output from snapshot, claims, and questions when it is read. A cached
`output.yaml` is an export, not the authoritative state.

This file-backed repository supports one trusted Atlas installation. PostgreSQL and
Snowflake remain isolated in separate source-bound workspaces; it is not a multi-tenant
or multi-instance design.

## HTTP surfaces

The API currently provides:

- source creation, credential management, probing, and deletion;
- source-bound workspace creation, listing, refresh, and deletion;
- extraction and analysis jobs;
- derived output and semantic-view reads;
- claim review;
- question answering and dismissal;
- job discovery and progress.

Long-running extraction and analysis return job IDs. The console polls job state and
refetches workspace output as completed tables are persisted.

## Configuration

| Variable | Default / role |
|---|---|
| `OPENROUTER_API_KEY` | Required for model analysis. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` |
| `ATLAS_MODEL` | `qwen/qwen3.7-plus` |
| `ATLAS_EFFORT` | `medium` |
| `ATLAS_MAX_TURNS` | `40` |
| `ATLAS_MAX_WORKERS` | `6` |
| `ATLAS_MAX_ROWS` | `50` |
| `ATLAS_STATEMENT_TIMEOUT_MS` | `15000` |
| `ATLAS_OUTPUT_DIR` | `out` |
| `ATLAS_SAMPLE_POLICY` | `strict` in code; `.env.example` sets `full` so the review sheet shows raw samples. |

## Security

Use a source role with `SELECT` and nothing else. PostgreSQL checks also run in a
read-only transaction with a statement timeout. The review workflow is easiest with
`ATLAS_SAMPLE_POLICY=full`, because samples are shown in the table sheet. If strict
profiling is enabled, Atlas withholds sensitive, opaque, free-text, key, and
high-cardinality values and shows the withholding reason instead.

These process-level guards are defence in depth. Database permissions remain the
primary security boundary.

## Checks

```bash
uv run pytest -q
uvx ruff check src tests --ignore B008
```

From the repository root, `make check` runs all engine and console checks.
