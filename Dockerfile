# Two stages so the shipped image has no Node in it: the console is built here
# and only its output crosses over.

# npm, not bun: that is what console/package-lock.json is, and a Dockerfile
# that installs with a different tool than the developer does is a second
# dependency resolution nobody is looking at.
FROM node:22-slim AS console
WORKDIR /console

# Dependencies first, and on their own layer — source changes far more often
# than the lockfile, and this is what keeps a rebuild from reinstalling.
COPY console/package.json console/package-lock.json ./
RUN npm ci

COPY console/index.html console/vite.config.ts console/tsconfig*.json ./
COPY console/src ./src
RUN npm run build && \
    if find dist -name '*.map' -print -quit | grep -q .; then \
        echo "source maps must not reach production" >&2; exit 1; \
    fi


FROM python:3.12-slim AS engine
WORKDIR /app

# uv rather than pip: the lockfile is what makes the deploy reproducible, and
# `--frozen` fails loudly if it has drifted from pyproject.toml.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY engine/pyproject.toml engine/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY engine/src ./src
RUN uv sync --frozen --no-dev

COPY --from=console /console/dist ./static

# Read-only at runtime except the workspace: catalogues, the job database and
# UI-managed credentials all live under here, so it is the one path that needs
# a volume in a real deployment.
ENV ATLAS_OUTPUT_DIR=/data/out \
    ATLAS_SOURCES_FILE=/data/sources.yaml \
    ATLAS_SECRETS_FILE=/data/.secrets.env \
    ATLAS_STATIC_DIR=/app/static \
    PATH="/app/.venv/bin:$PATH"
RUN mkdir -p /data && useradd --system --uid 10001 atlas && chown -R atlas /data
USER atlas

EXPOSE 8000
# Single worker on purpose: jobs run as threads in this process and the job
# registry is a SQLite file beside them. More workers means several registries
# disagreeing about what is running.
CMD ["uvicorn", "atlas.api:app", "--host", "0.0.0.0", "--port", "8000"]
