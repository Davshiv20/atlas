# Atlas development guidance

## Product source of truth

Read [`PRODUCT.md`](PRODUCT.md) before changing product behavior. Atlas is a living
semantic context layer, not a one-shot schema-description generator or a general-purpose
data catalogue.

The product loop is:

```text
Connect → infer → compress → validate → serve → observe → improve
```

The primary product metric is agent accuracy gain per minute of human review.

## Architecture

Use hexagonal architecture with explicit ports and adapters.

- Core semantic policy must not import database-specific implementations.
- Source adapters own connection behavior, identifier quoting, extraction, profiling,
  timeouts, database-specific SQL, and typed-check execution.
- The adapter observes; database-independent core policy computes verdicts.
- Keep functions small and single-purpose. Prefer explicit data models to raw dictionaries.
- Do not create an abstraction before there is a real boundary or second implementation.

Current packages:

```text
engine/   FastAPI + Python 3.12 + uv
console/  React + TypeScript + Tailwind CSS + Redux Toolkit
```

PostgreSQL is the only implemented source adapter. Snowflake is planned and appears in
some source/UI scaffolding, but it is not operational until `SnowflakeAdapter`, its
driver dependencies, cost controls, and integration tests exist.

## Product invariants

1. Physical observations and semantic inference are separate.
2. Semantic claims are atomic, attributable, reviewable, and versioned.
3. The model must not decide whether its own evidence passed.
4. Ungrounded claims cannot be presented as verified.
5. Unknown meaning remains explicit; never manufacture certainty.
6. Human review targets consequential business pivots, not every field description.
7. Multiple agents reuse one shared semantic model through objective-specific context
   views.
8. Source access is read-only and privacy-safe by default.
9. Agents receive typed checks and semantic interfaces, not unrestricted database SQL.
10. Derived output is never edited as an authoritative source.

## Current persistence

Workspace state is currently stored as YAML under `ATLAS_OUTPUT_DIR`. This is a temporary,
inspectable development repository. The intended product store is Atlas-owned PostgreSQL
behind a `MetadataRepository` port; YAML then becomes export/archive only.

Never commit `.env`, `.secrets.env`, workspace snapshots, evidence, profiles, or customer
sample data.

## Development

```bash
make install
make dev
make check
```

Useful package commands:

```bash
make engine-dev
make engine-test
make engine-lint
make console-dev
make console-typecheck
make console-build
make types
```

Use `direnv` for local environment activation. Do not use TypeScript `any`; use `unknown`
and narrow it. Add Python type hints at boundaries and use Pydantic/dataclasses for
structured data.
