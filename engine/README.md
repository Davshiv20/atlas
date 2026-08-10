# engine

Extraction, profiling, the analysis agent, and the HTTP API.

## Use

```bash
uv sync
cp .env.example .env          # OPENROUTER_API_KEY, ATLAS_DATABASE_URL

uv run atlas preflight                    # verify model + tool calling
uv run atlas extract  elara --url "postgresql+psycopg://..."
uv run atlas analyze  elara --limit 3
uv run atlas compile  elara --markdown out/elara/catalogue.md

uv run uvicorn atlas.api:app --reload     # same layout, over HTTP
```

## Artifacts

Per workspace, under `ATLAS_OUTPUT_DIR` (default `out/`):

| File | Role |
|---|---|
| `snapshot.yaml` | **Source.** Measured from the database; immutable per capture. |
| `facts.yaml` | **Source.** Claims plus human verdicts. |
| `questions.yaml` | **Source.** Open → answered. |
| `output.yaml` | **Derived.** Rebuilt from the three above. Never edit it. |
| `catalogue.md` | A rendering of `output.yaml`, for human review. |

Corrections belong on the claim they came from. Editing derived output creates
two stores that disagree, with nothing to say which is right.

## Configuration

`.env`, validated at startup by `settings.py`.

| Variable | Default |
|---|---|
| `OPENROUTER_API_KEY` | *(required)* |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` |
| `ATLAS_MODEL` | `qwen/qwen3.7-plus` |
| `ATLAS_EFFORT` | `medium` |
| `ATLAS_DATABASE_URL` | *(required)* — use a `SELECT`-only role |
| `ATLAS_MAX_ROWS` / `ATLAS_STATEMENT_TIMEOUT_MS` / `ATLAS_MAX_TURNS` | `50` / `15000` / `40` |

## Query safety

Agent-authored SQL passes three guards (`query.py`): syntactic (single
statement, `SELECT`/`WITH` only), transactional (`READ ONLY` + statement
timeout), and output redaction. The output layer is weaker than the profiling
policy — an arbitrary `SELECT` has no declared types, only the alias the model
chose, so `SELECT substr(email,1,3)` defeats it. **Connect with a role that has
`SELECT` and nothing else** before pointing this at a database you do not own.

## Portability

`extract.py` uses SQLAlchemy's `Inspector` and is engine-neutral. `profile.py`
is Postgres-specific (`TABLESAMPLE SYSTEM`, `::text`, `SET LOCAL
statement_timeout`) — that file is the adapter seam for a second engine.
