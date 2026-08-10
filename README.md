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
- Atomic semantic claims with provenance and grounding rules.
- Deterministic relationship discovery from constraints and schema conventions.
- Human review for claims and questions that database evidence cannot settle.
- A review console with Sources, Map, Review, and Questions surfaces.
- A derived semantic view for agent consumption.

Snowflake appears in the source model and UI as the next adapter, but the driver and
`SnowflakeAdapter` are not implemented yet. It is not currently a supported runtime
source.

## Quick start

Requirements: Python 3.12+, `uv`, Node.js 22+, and npm.

```bash
make install
cp engine/.env.example engine/.env

make dev       # engine on :8000, console on :5173
make check     # lint, engine tests, console typecheck, production build
```

Add a PostgreSQL source from the console's **Sources** view. Atlas stores UI-managed
credentials in `engine/.secrets.env`, which is gitignored. For headless use, provide a
connection URL through the source's configured environment variable or
`ATLAS_DATABASE_URL`.

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
Runtime state and UI-managed credentials live under `/data`; mount it as a persistent
volume.

## Current persistence

Workspace state is currently file-backed under `ATLAS_OUTPUT_DIR`:

```text
<workspace>/snapshot.yaml
<workspace>/facts.yaml
<workspace>/evidence.yaml
<workspace>/questions.yaml
<workspace>/output.yaml
```

This is inspectable and useful during product development, but it is not the intended
multi-user storage model. The next persistence step is an Atlas-owned PostgreSQL
control-plane database behind a `MetadataRepository` port. YAML should then become an
export/archive format rather than the source of truth.

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
