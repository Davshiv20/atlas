# Atlas console

The console is the semantic review workbench for Atlas. It is a client-rendered React
application built with TypeScript, Vite, Tailwind CSS, Redux Toolkit, and RTK Query.

It is not a public marketing surface and it is not intended to be a generic database
catalogue or ER-diagram tool. Its job is to help a reviewer resolve consequential
semantic uncertainty efficiently.

## Run locally

```bash
npm install
npm run dev
```

From the repository root:

```bash
make console-dev
make console-typecheck
make console-build
```

Vite proxies `/api` to the FastAPI engine during development. The production Docker
image builds the console and serves the static output from FastAPI on the same origin.

## Current surfaces

### Sources

Create a declared source, store or remove its credentials, test the connection, and
start extraction. Connection health and stored snapshot availability are separate
concepts, although the UI still needs clearer cached/offline labelling.

PostgreSQL is operational. Snowflake is visible as the next adapter but is not yet
implemented by the engine and should not be treated as supported.

### Map

Browse the extracted physical schema and mechanically established relationships. This
is an inspection surface, not the primary validation workflow.

### Review

Review generated claims in consequence-aware order. Each claim exposes its proposal,
evidence-derived confidence, trust-state and factor breakdown, provenance, evidence, and
current decision. Confidence is a trust score—not model certainty or a probability.
Consequence remains separate and drives review priority. The engine rejects attempts to
verify claims that lack sufficient grounding.

The long-term review model is risk-based: business pivots and representative cases,
not approval of every generated field description.

### Questions

Answer or dismiss focused questions whose business meaning cannot be established from
database evidence. Human answers are preserved as semantic decisions rather than being
folded silently into generated prose.

### Semantic view

Preview the derived view intended for agents. It keeps pending or excluded meaning
visible and separates mechanically established relationships from generated semantic
descriptions.

## Source structure

```text
src/
├── api/          OpenAPI document, API types, and error handling
├── components/   Sources, schema map, review queue, questions, semantic preview
├── lib/          Queue, layout, and review helpers
├── store/        Redux store, RTK Query API, and UI state
├── App.tsx       Top-level surface and job coordination
└── index.css     Tailwind theme and shared visual rules
```

## API contract

The engine's OpenAPI schema is the source contract:

```bash
make types
```

This refreshes `src/api/openapi.json`. Fully automated generation of the TypeScript
models from OpenAPI is still pending; do not change an API response without updating and
checking the console contract.

## Long-running jobs

Extraction and analysis run in the engine. The console:

- adopts jobs started by another tab or the CLI;
- polls only while a job is active;
- refreshes output as completed tables are persisted;
- preserves a visible final success or failure state.

A browser tab does not own a job, and refreshing the page must not make active work look
idle.

## Product direction

The current console remains table- and claim-oriented. The target workbench described in
[`../PRODUCT.md`](../PRODUCT.md) adds:

- objective-specific relevant domain maps;
- first-class semantic entities and business concepts;
- ranked high-impact ambiguity;
- review budgets and representative-case validation;
- agent-question previews using `get_context_for_question()`;
- capability-level readiness rather than “percentage of fields approved”;
- drift and agent-failure feedback.

## Interaction rules

- Observed facts and inferred meaning must look different.
- Unknown, stale, and conflicting meaning must stay visible.
- Never imply that inherited class review means every field was individually inspected.
- Surface 409 grounding errors; do not retry them automatically.
- Keep review actions keyboard-accessible.
- Avoid exposing credentials or raw sensitive values in browser state.
