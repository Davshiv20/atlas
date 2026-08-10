# atlas

Generates an agent-ready business-context catalogue from an unfamiliar database,
where every non-physical claim is either grounded by an executed check or
flagged for a human.

```
atlas/
├── engine/     Python — extraction, profiling, relationship discovery, the
│               analysis agent, the emitted semantic view, HTTP API
└── console/    React + TypeScript — the review workbench
```

Two packages because they have different release cadences and different
reviewers. They are coupled by exactly one thing: the engine's OpenAPI schema,
from which the console's types are generated (`make types`). No shared source
package, so neither can quietly reach into the other's internals.

## Quick start

```bash
make install                          # engine + console dependencies
cp engine/.env.example engine/.env    # OPENROUTER_API_KEY

make dev                              # both servers; Ctrl-C stops both
make check                            # lint, tests, typecheck, build
```

Databases are added from the console (Sources), not from `.env` — the
connection string is stored by the engine and tested before anything runs.

## Deploying

```bash
make image        # multi-stage: the console is built in, only its output ships
make image-run    # one origin on :8000, workspace persisted in ./data
```

Development runs two origins — Vite serves the console and proxies `/api` to
the engine. The image runs one: the console is built in a Node stage, the
artifact is copied into the Python image, and the engine mounts it at `/`.
No Node in the shipped image, and no CORS to configure.

The mount is registered after every router, because a mount at `/` matches
everything. `/data` is the only writable path — catalogues, the job database
and UI-managed credentials all live there, so it is what needs a volume.

## direnv

Three `.envrc` files — root, `engine/`, `console/`. The package files
`source_up` the root, so shared values live in one place.

```bash
direnv allow . && direnv allow engine && direnv allow console
```

Run that once, and again whenever an `.envrc` changes — direnv refuses to
execute a file it has not been shown, which is the point of it.

With direnv active, `cd engine` puts `.venv/bin` on PATH and loads `.env`;
`cd console` puts `node_modules/.bin` on PATH. So `pytest`, `atlas`, `uvicorn`,
`vite`, and `tsc` all run without a `uv run` or `npx` prefix. The Makefile keeps
those prefixes so it works with or without direnv.

Machine-specific overrides go in `.envrc.local` (gitignored — see
`.envrc.local.example`). The `.envrc` files themselves are tracked: they are how
the project is set up, not a personal preference.

## Why it is built this way

Pointing an LLM at `information_schema` produces fluent, confident, wrong
descriptions that a reviewer rubber-stamps because nothing contradicts them.
Three structural defences:

- **Layer separation.** The physical snapshot is extracted and measured only —
  no inference ever writes into it. Claims are assertions *about* it, each
  carrying provenance and a review verdict.
- **Grounding before review.** A claim reaches a human with a check that could
  have falsified it already executed, and the result shown alongside. Ungrounded
  claims are capped below the verification threshold — enforced in the model,
  not by convention.
- **Evidence relevance.** A cited query must be capable of supporting the claim,
  not merely have run. A row sample grounds nothing.

- **Relationships are never inferred by the model.** They are derived from
  declared constraints and settled by join checks before analysis starts, so the
  agent spends its budget on meaning rather than on rediscovering foreign keys.

Without query logs the model's most valuable output is **questions, not
answers**: *"`process_id_ref` holds `P1`, `PR-02`, `P001`, `Inc-1` — one
identifier space or several?"* costs five seconds to answer and cannot be
inferred from the schema at any effort.

Answering one is the only thing that lifts a claim about business meaning above
what data alone can establish — no query settles what a column means to the
organisation.

See `PRODUCT.md` for the full product definition.
