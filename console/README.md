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
create a source-bound workspace, and start extraction. Connection health and stored
snapshot availability are separate concepts. A workspace's source binding is immutable:
PostgreSQL and Snowflake state cannot overwrite one another, and refreshing a snapshot
requires confirmation before semantic state is reset.

PostgreSQL is established. Snowflake is available as an early adapter for connection,
reflection, profiling, and typed checks; validate it against the target account before
production use. When Snowflake is selected, the source form includes a setup helper that
parses a Snowsight schema URL, fills `DATABASE.SCHEMA`, collects normal connection
fields, and provides read-only grant SQL for an administrator. The engine constructs and
URL-encodes the connection string; users never build it manually. Human users can choose
**Password + authenticator code** for TOTP or **Password + MFA push** and complete
Snowflake's second factor while Atlas waits. A TOTP code is used only for the initial
connection and is never persisted. Corporate browser SSO is also available when the
account has a SAML identity provider configured.
Deployed connections should use non-interactive credentials such as a programmatic token
or key pair.

### Map

Browse the extracted physical schema and mechanically established relationships. This
is an inspection surface, not the primary validation workflow.

### Review

Review one table at a time in a spreadsheet-like sheet. All fields stay visible, but only
risky rows are highlighted. Yellow means the row deserves human attention; red means
conflicting evidence or very weak support on an important claim. Routine rows can stay
inferred without becoming review work.

Each row shows source shape, suggested meaning, sample values, trust score, review reason,
and an expandable lineage chain from source column to evidence to claim to YAML output.
If a snapshot was extracted with samples withheld, the row says why and points to
`ATLAS_SAMPLE_POLICY=full` as an explicit opt-in for trusted raw-sample review. Confidence
is a trust score—not model certainty or a probability. Consequence remains separate and
drives the highlighting. A review is recorded as human-decision evidence; Atlas keeps a
human assertion distinct from a claim established by a database check.

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
- Surface workspace conflicts such as concurrent mutations or unsafe refreshes; do not retry them automatically.
- Keep review actions keyboard-accessible.
- Avoid exposing credentials or raw sensitive values in browser state.
