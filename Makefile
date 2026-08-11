# Task runner across the monorepo. Every target is safe to run from the root.
#
# Targets are prefixed by the package they act on (engine- / console-), so
# `make test` never leaves you guessing which half it ran.

ENGINE_PORT ?= 8000
PG_CONTAINER ?= atlas-test-pg
PG_PORT ?= 55432
PG_USER ?= postgres
PG_DB ?= atlas
CONSOLE_PORT ?= 5173

# The throwaway database's password is read from engine/.env, which is never
# tracked, rather than written here with a default. It only ever protects a
# container that lives for the length of one test run — but a literal
# `user:password@host` in a tracked file is a credential to every scanner that
# reads the repository, and arguing about which hardcoded passwords are real is
# a losing position. There is no fallback: an unset value fails the target
# rather than quietly starting a container with a guessable one.
ENGINE_ENV ?= engine/.env
PG_PASSWORD ?= $(shell [ -f $(ENGINE_ENV) ] && \
  sed -n 's/^ATLAS_TEST_PG_PASSWORD=//p' $(ENGINE_ENV) | tail -1)
# Assembled from parts so the connection string is never spelled out in full.
PG_URL = postgresql+psycopg://$(PG_USER):$(PG_PASSWORD)@localhost:$(PG_PORT)/$(PG_DB)

.DEFAULT_GOAL := help
.PHONY: help install dev \
        engine-dev engine-test engine-test-postgres engine-lint engine-shell \
        console-dev console-build console-typecheck \
        types check image image-run clean

help:  ## Show available targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@printf '\n  engine :%s   console :%s   (override: make console-dev CONSOLE_PORT=3000)\n' \
	  "$(ENGINE_PORT)" "$(CONSOLE_PORT)"

# --- setup -----------------------------------------------------------------

install: console/node_modules  ## Install dependencies for both packages
	cd engine && uv sync

# Sentinel target: npm install re-runs only when package.json is newer, so
# `make console-dev` on a fresh clone works without a separate setup step.
console/node_modules: console/package.json
	cd console && npm install --no-fund --no-audit
	@touch console/node_modules

# --- development -----------------------------------------------------------

engine-dev:  ## Serve the engine API with reload
	cd engine && uv run uvicorn atlas.api:app --reload --port $(ENGINE_PORT)

console-dev: console/node_modules  ## Serve the console with HMR
	cd console && npm run dev -- --port $(CONSOLE_PORT)

dev: console/node_modules  ## Run both dev servers; Ctrl-C stops both
	@echo "engine  → http://localhost:$(ENGINE_PORT)/docs"
	@echo "console → http://localhost:$(CONSOLE_PORT)"
	@# The trap is what makes Ctrl-C kill the engine too. Without it the API
	@# keeps the port and the next `make dev` fails with "address in use".
	@trap 'kill 0' EXIT INT TERM; \
	  (cd engine && uv run uvicorn atlas.api:app --reload --port $(ENGINE_PORT)) & \
	  (cd console && npm run dev -- --port $(CONSOLE_PORT)) & \
	  wait

# --- checks ----------------------------------------------------------------

engine-test:  ## Run the engine test suite (no database needed)
	cd engine && uv run pytest -q -m "not postgres"

# The generated SQL, executed. Kept off the default loop because it needs a
# database: everything else runs in about a second and should stay that way.
engine-test-postgres: ## Run the SQL tests against a throwaway PostgreSQL
	@[ -n "$(PG_PASSWORD)" ] || { \
	  echo "ATLAS_TEST_PG_PASSWORD is not set in $(ENGINE_ENV)."; \
	  echo "Add a line like ATLAS_TEST_PG_PASSWORD=<anything> — the container is"; \
	  echo "created and destroyed by this target, so the value is yours to pick."; \
	  echo "See engine/.env.example."; \
	  exit 1; \
	}
	@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true
	@docker run -d --rm --name $(PG_CONTAINER) -e POSTGRES_PASSWORD=$(PG_PASSWORD) \
	  -e POSTGRES_DB=$(PG_DB) -p $(PG_PORT):5432 postgres:16-alpine >/dev/null
	@until docker exec $(PG_CONTAINER) pg_isready -U $(PG_USER) >/dev/null 2>&1; do sleep 1; done
	-cd engine && ATLAS_TEST_DATABASE_URL=$(PG_URL) uv run pytest -q -m postgres
	@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true

engine-lint:  ## Lint the engine (B008 is the idiomatic Typer/FastAPI default-arg pattern)
	cd engine && uvx ruff check src tests --ignore B008

console-typecheck: console/node_modules  ## Typecheck the console
	cd console && npm run typecheck

console-build: console/node_modules  ## Production build of the console
	cd console && npm run build

# --- contract --------------------------------------------------------------

types:  ## Regenerate the console's copy of the engine's OpenAPI schema
	@mkdir -p console/src/api
	cd engine && uv run python -c "import json, atlas.api as a; print(json.dumps(a.app.openapi(), indent=2))" > ../console/src/api/openapi.json
	@echo "wrote console/src/api/openapi.json — generate types from it with openapi-typescript"

# --- housekeeping ----------------------------------------------------------

check: engine-lint engine-test console-typecheck console-build  ## Everything CI would run
	@printf "\nall green\n"

engine-shell:  ## Open a Python REPL with the engine importable
	cd engine && uv run python

image:  ## Build the deployable image (console built in, API serves it)
	docker build -t atlas:latest .

image-run: image  ## Run it on $(ENGINE_PORT), workspace persisted in ./data
	docker run --rm -p $(ENGINE_PORT):8000 -v "$$PWD/data:/data" --env-file engine/.env atlas:latest

clean:  ## Remove caches and build output (leaves node_modules and catalogues)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf engine/.pytest_cache engine/.ruff_cache console/dist console/.vite console/.tsbuild
