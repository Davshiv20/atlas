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

PostgreSQL is the established source adapter. Snowflake has an early adapter for
connection, reflection, profiling, typed checks, query tagging, and timeouts. It still
needs live-account integration validation, hybrid-table enforcement metadata, and
production cost testing.

## Product invariants

1. Physical observations and semantic inference are separate.
2. Semantic claims are atomic, attributable, reviewable, and versioned.
3. The model must not decide whether its own evidence passed.
4. Confidence is an evidence-derived trust score with an inspectable factor breakdown; it is never model self-confidence or probability.
5. Review priority is separate from confidence and is driven by consequence and task impact.
6. Ungrounded claims cannot be presented as verified.
7. Unknown meaning remains explicit; never manufacture certainty.
8. Human review targets consequential business pivots, not every field description.
9. Multiple agents reuse one shared semantic model through objective-specific context
   views.
10. Source access is read-only and privacy-safe by default.
11. Agents receive typed checks and semantic interfaces, not unrestricted database SQL.
12. Derived output is never edited as an authoritative source.

## Current persistence

Workspace state sits behind the `MetadataRepository` port (`atlas/metadata/base.py`).
`YamlMetadataRepository` is the only implementation today, writing YAML under
`ATLAS_OUTPUT_DIR`; `atlas/metadata/registry.py` is the single place that names it. The
intended product store is Atlas-owned PostgreSQL behind the same port, after which YAML
becomes export/archive only.

Policy about a workspace lives in `atlas/catalog.py`, not in the store: what may be
published, what a re-analysed table replaces, what a regeneration may discard. Nothing
outside `atlas/metadata/` may name a path, and the domain models do not serialize
themselves — a model that knows how to write itself to disk has already picked a store.

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
