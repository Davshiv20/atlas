# Atlas

Atlas is a semantic context layer for AI agents over databases the AI team does not own.
It extracts an unfamiliar source, records physical evidence separately from semantic
inference, attempts to falsify generated claims, sends consequential ambiguity to a
human, and emits a compact semantic view.

The target product loop is:

```text
Connect → infer → compress → validate → serve → observe → improve
```

See [`PRODUCT.md`](PRODUCT.md) for the product definition. The current implementation
covers the PostgreSQL source-understanding, claim-generation, evidence, and review
foundations; objective-driven compression, the task context compiler, drift, and the
learning loop are still to be built.

## Repository

```text
atlas/
├── engine/     Python + FastAPI — adapters, extraction, profiling, checks,
│               claims, evidence, review APIs, and semantic-view generation
├── console/    React + TypeScript — source setup and semantic review workbench
└── PRODUCT.md  product direction, MVP scope, and phased roadmap
```

The packages communicate through the engine's OpenAPI contract. The console must not
import engine internals or maintain an independent interpretation of API models.

## Current capabilities

- PostgreSQL extraction through a `DatabaseAdapter` port.
- Canonical physical snapshots containing tables, fields, keys, relationships, and
  privacy-safe profiles.
- Typed grain, join, distribution, nullability, and ordering checks.
- Structured evidence with assertion, observation, scope, verdict, limitations,
  freshness, and query hash.
- Atomic semantic claims with provenance, grounding rules, and factorized confidence as an evidence-derived trust score.
- Deterministic relationship discovery from constraints and schema conventions.
- Human review for claims and questions that database evidence cannot settle.
- A review console with Sources, Map, table-sheet Review, and Questions surfaces.
- A derived semantic view for agent consumption.

PostgreSQL is the established adapter. Snowflake connection, reflection, sampled
profiling, typed checks, query tagging, and timeout support are implemented as an early
adapter; validate it against a real Snowflake account before production use.

## Quick start

Requirements: Python 3.12+, `uv`, Node.js 22+, and npm.

```bash
make install
cp engine/.env.example engine/.env

make dev       # engine on :8000, console on :5173
make check     # lint, engine tests, console typecheck, production build
```

Add PostgreSQL and Snowflake sources from the console's **Sources** view. Atlas stores
UI-managed credentials in `engine/.secrets.env`, which is gitignored. Each workspace is
bound permanently to one declared source, so adding or refreshing Snowflake cannot
replace PostgreSQL state. For headless use, provide each source URL through its configured
environment variable.

Always use a database role with `SELECT` and nothing else.

## Development commands

```bash
make engine-dev
make console-dev
make engine-test
make engine-lint
make console-typecheck
make console-build
make types
```

`make types` refreshes `console/src/api/openapi.json`. The current TypeScript API models
are maintained beside that schema; fully automated OpenAPI-to-TypeScript generation is
still pending.

## Deployment

```bash
make image
make image-run
```

The production image builds the console and serves it from FastAPI on one origin.
Runtime state lives under `/data`; mount it as a persistent volume. This includes
`/data/sources.yaml`, `/data/.secrets.env`, workspace snapshots, evidence, and the job
database. Without a persistent volume, connections and generated work disappear when the
container is replaced.

## Current persistence

Workspace state is reached through the `MetadataRepository` port, which has two
implementations. `ATLAS_DATABASE_URL` chooses between them.

**Atlas-owned PostgreSQL**, when the URL is set. Snapshots are stored as documents because
nothing edits one; claims, questions, and evidence are rows, because people edit those one
at a time. A review writes one row, so two reviewers settling different claims both keep
their verdict — and a composite operation that fails rolls back whole.

```bash
make migrate                        # apply the schema
cd engine && uv run atlas migrate-store   # copy existing workspaces across
```

Setting the URL changes where Atlas looks, not where the data is. Without the copy the
engine comes up on an empty schema and every workspace reads as absent.

**Files**, when it is not. Inspectable and dependency-free, which is why it is still the
default, but it rewrites a whole file per edit and its lock is an advisory `flock` that
only holds within one machine. The layout below is private to that adapter — no caller
outside `atlas/metadata/` names a path:

```text
<workspace>/workspace.yaml                 # immutable source + active generation
<workspace>/generations/<n>/snapshot.yaml
<workspace>/generations/<n>/facts.yaml
<workspace>/generations/<n>/evidence.yaml
<workspace>/generations/<n>/questions.yaml
```

The catalogue document is not among them. It is derived from the snapshot and
the claims, rebuilt on every read, and leaves Atlas only when someone exports
it: `GET /workspaces/<name>/output` for the JSON, `atlas compile <name> --out
FILE` for YAML on disk at a path you choose.

This inspectable file-backed design supports one trusted Atlas installation with multiple
independent data sources. It is not a multi-tenant or multi-instance storage model.

A stored snapshot can exist while its source is disconnected. The console must treat
connection health and snapshot availability as separate states.

## Architectural rules

1. **Observed facts and semantic inference remain separate.** Extraction never writes
   generated meaning into the physical snapshot.
2. **The agent does not receive a generic SQL tool.** It proposes typed checks; Atlas
   compiles and executes database-specific SQL and computes the verdict.
3. **Evidence must be relevant and falsifiable.** A query that merely ran does not
   ground a claim.
4. **Relationships are settled structurally.** Declared constraints are used directly;
   unenforced or inferred candidates require coverage checks.
5. **Unknown remains unknown.** Business meaning that data cannot establish becomes a
   focused human question.
6. **Human attention is spent on consequential pivots.** The goal is not to approve
   every generated field description.
7. **Task context is smaller than the semantic model.** Multiple agents should reuse a
   shared semantic model while receiving objective-specific context views.

## Security defaults

- Read-only source credentials.
- No writes to customer databases.
- Strict sample policy by default.
- Sensitive, opaque, free-text, and high-cardinality values withheld from model input.
- Typed check execution instead of arbitrary model-authored SQL.
- Statement timeouts and bounded result sizes.
- Secrets excluded from Git and API responses.

These controls supplement database permissions; they do not replace a properly
restricted source role.
