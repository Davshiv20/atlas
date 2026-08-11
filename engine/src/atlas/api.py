"""HTTP API.

Long operations are jobs, never inline requests: extraction takes seconds and
analysis takes minutes per table, which no gateway will hold open. Everything
that reads is synchronous; everything that runs the pipeline returns a job id.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from sqlalchemy import URL
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from atlas.adapters.base import UnsupportedDatabase
from atlas.adapters.registry import create_adapter
from atlas.answers import record_answer
from atlas.facts import Consequence, Fact, FactStatus, FactStore
from atlas.jobs import Job, JobProgress, ProgressReporter, get_registry
from atlas.output import assess_facts, build_output
from atlas.questions import Question, QuestionLog
from atlas.secrets import clear_secret, has_secret, load_into_environment, set_secret
from atlas.semantic_view import build_semantic_view, render_yaml
from atlas.settings import get_settings
from atlas.sources import (
    DuplicateSource,
    Source,
    SourceNotFound,
    SourceRegistry,
)
from atlas.workspace import InvalidWorkspace, Workspace

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load UI-managed credentials when the server starts.

    Deliberately not at import time: importing this module would then read the
    secrets file and mutate the process environment as a side effect, which
    leaks real credentials into anything that merely imports the app — tests
    included.
    """
    loaded = load_into_environment()
    if loaded:
        logger.info("loaded %d stored credential(s)", loaded)
    # Any job still marked running belongs to a process that is gone. Settling
    # them here is what stops the console polling a run that is not happening.
    get_registry().reconcile()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="atlas",
    version="0.1.0",
    summary="Grounded schema catalogue generation for unfamiliar databases.",
)


# --- request models --------------------------------------------------------


class ExtractRequest(BaseModel):
    source_id: str | None = Field(default=None, description="A source declared in sources.yaml")
    database_url: str | None = Field(
        default=None, description="Direct URL. Falls back to ATLAS_DATABASE_URL."
    )
    schema_name: str | None = Field(default=None, description="Overrides the source's namespace")
    profile: bool = True


class AnalyzeRequest(BaseModel):
    database_url: str | None = None
    limit: int | None = Field(
        default=None, ge=1, le=500, description="How many tables; null means all remaining"
    )
    tables: list[str] | None = Field(
        default=None, description="Specific tables. Overrides limit and ranking."
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "Re-analyze tables that already have claims, discarding their previous "
            "claims and evidence rather than merging. Scoped to the tables in this "
            "run. Off by default: regeneration resets human verdicts on reworded "
            "claims and drops the observations behind them."
        ),
    )






class ReviewRequest(BaseModel):
    decision: FactStatus
    reviewer: str
    claim: str | None = Field(default=None, description="Corrected wording, if editing")


# --- helpers ---------------------------------------------------------------


def _workspace(name: str) -> Workspace:
    try:
        return Workspace(name)
    except InvalidWorkspace as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _existing(name: str) -> Workspace:
    workspace = _workspace(name)
    if not workspace.exists():
        raise HTTPException(
            status_code=404, detail=f"workspace {name!r} has no snapshot; run extract first"
        )
    return workspace


def _resolve_url(supplied: str | None) -> str:
    try:
        return supplied or get_settings().require_database_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



# --- meta ------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/config", tags=["meta"])
def config() -> dict:
    """Effective configuration. Secrets are reported as present or absent only."""
    settings = get_settings()
    return {
        "model": settings.atlas_model,
        "effort": settings.atlas_effort,
        "base_url": settings.openrouter_base_url,
        "api_key_configured": settings.openrouter_api_key is not None,
        "database_url_configured": settings.atlas_database_url is not None,
        "max_turns": settings.atlas_max_turns,
        "max_rows": settings.atlas_max_rows,
        "statement_timeout_ms": settings.atlas_statement_timeout_ms,
    }


# --- workspaces ------------------------------------------------------------


# --- sources ---------------------------------------------------------------


class ConnectionHealth(BaseModel):
    """The result of actually connecting, as distinct from being configured.

    `configured` only means the environment variable holds something. A wrong
    password, an unreachable host, or a schema that does not exist all leave it
    true — so a card reading "credentials found" was claiming more than anyone
    had checked.
    """

    state: Literal["unknown", "connected", "failed"] = "unknown"
    checked_at: datetime | None = None
    detail: str | None = None
    server_version: str | None = None
    table_count: int | None = None


# Health is deliberately in memory. The engine reads connection URLs from its
# environment at startup, so a check result is only meaningful for this
# process — persisting it would let a restart serve a status that no longer
# reflects what the process can reach.
_health: dict[str, ConnectionHealth] = {}


class SourceStatus(BaseModel):
    """A declared source, whether its variable is populated, and whether anyone
    has managed to connect with it.

    Never carries the URL: this endpoint is unauthenticated, and the registry
    exists precisely so credentials do not travel through it.
    """

    id: str
    adapter: str
    url_env: str
    namespace: str
    label: str | None = None
    configured: bool
    # True when Atlas holds the credential itself, rather than reading one the
    # operator exported. Determines whether the UI offers to edit it.
    managed: bool = False
    health: ConnectionHealth = ConnectionHealth()


def _status(source: Source) -> SourceStatus:
    return SourceStatus(
        **source.model_dump(),
        configured=source.configured,
        managed=has_secret(source.url_env),
        health=_health.get(source.id, ConnectionHealth()),
    )


def _url_for_workspace(workspace: Workspace, supplied: str | None) -> str:
    """Where this workspace's database lives.

    In order: an explicit URL, then the source its snapshot was captured from,
    then ATLAS_DATABASE_URL. The middle step is what `analyze` was missing —
    `extract` learned about sources and `analyze` did not, so every run failed
    with "ATLAS_DATABASE_URL is not set" once sources replaced that variable.
    """
    if supplied:
        return supplied

    source_id = workspace.read_snapshot().source_id if workspace.exists() else None
    if source_id:
        try:
            source = SourceRegistry.read().get(source_id)
        except SourceNotFound as exc:
            raise HTTPException(
                status_code=400,
                detail=f"this workspace was captured from source {source_id!r}, "
                f"which no longer exists",
            ) from exc
        try:
            return source.resolve_url()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _resolve_url(None)


def _readable(exc: Exception) -> str:
    """One line a person can act on.

    psycopg reports every address it tried, so a wrong password arrives as five
    lines saying the same thing plus a documentation URL. The first FATAL is
    the whole message.
    """
    text = str(exc)
    for line in text.splitlines():
        if "FATAL:" in line:
            return line.split("FATAL:", 1)[1].strip() or line.strip()
    first = text.split("\n")[0].strip()
    first = first.removeprefix("(psycopg.OperationalError)").strip()
    return (first[:180] + "…") if len(first) > 180 else first


def _probe(source: Source, connection_url: str | None = None) -> ConnectionHealth:
    """Connect, confirm, and record. The single place a source's state changes."""
    try:
        url = connection_url or source.resolve_url()
    except RuntimeError as exc:
        health = ConnectionHealth(
            state="failed", checked_at=datetime.now(UTC), detail=str(exc)
        )
        _health[source.id] = health
        return health

    try:
        with create_adapter(url) as adapter:
            adapter.test_connection()
            info = adapter.probe(source.namespace)
    except UnsupportedDatabase as exc:
        health = ConnectionHealth(state="failed", checked_at=datetime.now(UTC), detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - any driver error is a failed check
        # Driver messages carry host and user, which is the useful part of a
        # connection failure; they do not carry the password.
        health = ConnectionHealth(
            state="failed", checked_at=datetime.now(UTC), detail=_readable(exc)
        )
    else:
        health = ConnectionHealth(
            state="connected",
            checked_at=datetime.now(UTC),
            server_version=info.server_version,
            table_count=info.table_count,
            detail=(
                f"{info.server_version} · {info.table_count} tables in {info.namespace}"
                if info.table_count
                else f"{info.server_version} · no tables in {info.namespace}"
            ),
        )

    _health[source.id] = health
    return health


@app.get("/sources", tags=["sources"])
def list_sources() -> dict[str, list[SourceStatus]]:
    return {"sources": [_status(s) for s in SourceRegistry.read().sources]}


@app.post("/sources", status_code=201, tags=["sources"])
def create_source(source: Source) -> SourceStatus:
    registry = SourceRegistry.read()
    try:
        registry.add(source)
    except DuplicateSource as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    registry.write()
    # Probe on creation: the answer to "did that work" belongs in the response
    # to the action, not behind a second button the user has to know to press.
    _probe(source)
    return _status(source)


class CredentialRequest(BaseModel):
    url: str = Field(min_length=1, description="Full SQLAlchemy connection URL")


class SnowflakeCredentialRequest(BaseModel):
    account_identifier: str = Field(min_length=1, examples=["myorg-myaccount"])
    username: str = Field(min_length=1)
    auth_method: Literal["password", "mfa_push", "mfa_totp", "external_browser"] = "password"
    password: SecretStr | None = Field(default=None, min_length=1)
    passcode: SecretStr | None = Field(default=None, min_length=1)
    warehouse: str = Field(min_length=1)
    role: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_password_for_password_auth(self) -> Self:
        if self.auth_method != "external_browser" and self.password is None:
            raise ValueError("password is required for password, token, or MFA authentication")
        if self.auth_method == "mfa_totp":
            code = self.passcode.get_secret_value() if self.passcode else ""
            if len(code) != 6 or not code.isdigit():
                raise ValueError("a 6-digit authenticator code is required for TOTP authentication")
        return self


def _snowflake_url(
    source: Source, request: SnowflakeCredentialRequest, *, include_passcode: bool = False
) -> str:
    parts = [part for part in source.namespace.split(".") if part]
    if len(parts) != 2:
        raise HTTPException(
            status_code=400,
            detail="Snowflake source schema must be DATABASE.SCHEMA",
        )
    database, schema = parts
    query = {"warehouse": request.warehouse, "role": request.role}
    password = request.password.get_secret_value() if request.password else None
    if request.auth_method in {"mfa_push", "mfa_totp"}:
        query["authenticator"] = "username_password_mfa"
        query["client_request_mfa_token"] = "true"
        if include_passcode and request.passcode:
            query["passcode"] = request.passcode.get_secret_value()
    elif request.auth_method == "external_browser":
        query["authenticator"] = "externalbrowser"
        password = None
    return URL.create(
        "snowflake",
        username=request.username,
        password=password,
        host=request.account_identifier,
        database=f"{database}/{schema}",
        query=query,
    ).render_as_string(hide_password=False)


@app.put("/sources/{source_id}/credentials", tags=["sources"])
def set_credentials(source_id: str, request: CredentialRequest) -> ConnectionHealth:
    """Store the connection string and immediately re-probe.

    Writing it also sets it in this process's environment, so there is no
    restart between saving a credential and finding out whether it works.
    """
    try:
        source = SourceRegistry.read().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc

    set_secret(source.url_env, request.url.strip())
    return _probe(source)


@app.put("/sources/{source_id}/credentials/snowflake", tags=["sources"])
def set_snowflake_credentials(
    source_id: str, request: SnowflakeCredentialRequest
) -> ConnectionHealth:
    """Build and encode a Snowflake URL from normal credential fields.

    People should never have to know URL-encoding rules for passwords. The
    password is accepted as a secret field, encoded by SQLAlchemy, persisted in
    the existing engine-side secret store, and never returned by the API.
    """
    try:
        source = SourceRegistry.read().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    if source.adapter != "snowflake":
        raise HTTPException(status_code=409, detail="source is not a Snowflake connection")

    stored_url = _snowflake_url(source, request)
    set_secret(source.url_env, stored_url)
    if request.auth_method == "mfa_totp":
        # A TOTP code is single-use and expires quickly. Use it only for this
        # connection attempt; never persist it beside the durable credential.
        return _probe(source, _snowflake_url(source, request, include_passcode=True))
    return _probe(source)


@app.delete("/sources/{source_id}/credentials", status_code=204, tags=["sources"])
def forget_credentials(source_id: str) -> None:
    try:
        source = SourceRegistry.read().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    clear_secret(source.url_env)
    _health.pop(source_id, None)


@app.delete("/sources/{source_id}", status_code=204, tags=["sources"])
def delete_source(source_id: str) -> None:
    registry = SourceRegistry.read()
    try:
        registry.remove(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    registry.write()


@app.post("/sources/{source_id}/test", tags=["sources"])
def test_source(source_id: str) -> ConnectionHealth:
    """Connect and report what came back.

    Runs inline rather than as a job: two queries, and a setup form needs the
    answer before the user moves on.
    """
    try:
        source = SourceRegistry.read().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    return _probe(source)


@app.get("/workspaces", tags=["workspaces"])
def list_workspaces() -> dict:
    return {"workspaces": Workspace.list_all()}


@app.post("/workspaces/{name}/extract", status_code=202, tags=["pipeline"])
def extract(name: str, request: ExtractRequest = Body(default=ExtractRequest())) -> Job:
    """Capture the physical layer. Deterministic; no model involved."""
    workspace = _workspace(name)
    source = None
    if request.source_id:
        try:
            source = SourceRegistry.read().get(request.source_id)
        except SourceNotFound as exc:
            raise HTTPException(
                status_code=404, detail=f"no source {request.source_id!r}"
            ) from exc
        try:
            url = source.resolve_url()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        url = _resolve_url(request.database_url)

    namespace = request.schema_name or (source.namespace if source else "public")

    def work(report: ProgressReporter) -> dict:
        with create_adapter(url) as adapter:
            adapter.test_connection()
            report(JobProgress(message="Reading structure"))
            snapshot = adapter.extract_structure(namespace)
            if source:
                snapshot = snapshot.model_copy(update={"source_id": source.id})
            if request.profile:
                report(JobProgress(message=f"Profiling {len(snapshot.tables)} tables"))
                snapshot = adapter.profile(snapshot)
        snapshot.write(workspace.snapshot_path)
        return {"tables": len(snapshot.tables), "path": str(workspace.snapshot_path)}

    return get_registry().submit("extract", name, work)


@app.post("/workspaces/{name}/analyze", status_code=202, tags=["pipeline"])
def analyze(name: str, request: AnalyzeRequest = Body(default=AnalyzeRequest())) -> Job:
    """Run the analysis agent. Minutes per table — always poll the job."""
    from atlas.agent import AnalysisSink, analyze_schema, select_tables
    from atlas.relationships import as_claims, by_table, discover

    workspace = _existing(name)
    url = _url_for_workspace(workspace, request.database_url)
    if get_settings().openrouter_api_key is None:
        raise HTTPException(
            status_code=400,
            detail="No model API key configured. Set OPENROUTER_API_KEY in engine/.env "
            "and restart the engine.",
        )

    def work(report: ProgressReporter) -> dict:
        snapshot = workspace.read_snapshot()
        existing = workspace.read_facts()
        # A table is analysed when something was *read* about it, not merely
        # when a claim names it. Relationship discovery writes a join claim for
        # almost every table in the schema, and counting those made a fresh
        # workspace look fully analysed: a run with no limit selected one table.
        analyzed = {
            f.subject.split(".")[0]
            for f in existing.facts
            if f.aspect not in ("join", "class")
        }

        selected = select_tables(
            snapshot,
            request.limit,
            request.tables,
            set() if request.regenerate else analyzed,
        )
        if not selected:
            return {"claims": 0, "questions": 0, "skipped": sorted(analyzed), "tables": []}

        names = {t.name for t in selected}
        dropped = workspace.drop(names) if request.regenerate else {}

        planned = [t.name for t in selected]

        # Relationships first, and without a model. Every join the agent used
        # to hypothesise is derivable from the schema, and re-deriving them per
        # table was ~40% of the check budget in earlier runs.
        report(JobProgress(message="Mapping relationships", tables=planned))
        mapper = create_adapter(url)
        try:
            discovery = discover(mapper, snapshot, database=snapshot.database)
        finally:
            mapper.close()
        joins, join_links = as_claims(discovery)
        workspace.absorb_relationships(joins, join_links, discovery.evidence)

        def describe() -> str:
            if not reading:
                return "Starting"
            listed = ", ".join(sorted(reading)[:3])
            more = len(reading) - 3
            return f"Reading {listed}{f' +{more}' if more > 0 else ''}"

        def starting(table: str) -> None:
            reading.add(table)
            report(
                JobProgress(
                    message=describe(),
                    tables=planned,
                    completed=list(done),
                    current=sorted(reading),
                )
            )

        def finished(table: str, sink: AnalysisSink) -> None:
            # Written now, not at the end of the run. Until this moved, a table
            # could be finished for twenty minutes with nothing on disk to show
            # for it, and a restart threw the whole run away.
            workspace.absorb(table, sink.facts, sink.questions, sink.evidence)
            reading.discard(table)
            done.append(table)
            if sink.truncated:
                partial.append(table)
            report(
                JobProgress(
                    message=describe(),
                    tables=planned,
                    completed=list(done),
                    current=sorted(reading),
                )
            )

        done: list[str] = []
        partial: list[str] = []
        reading: set[str] = set()
        workers = get_settings().atlas_max_workers
        adapter = create_adapter(url, concurrency=workers)
        try:
            store, questions, evidence = analyze_schema(
                adapter,
                snapshot,
                tables=planned,
                on_table_start=starting,
                on_table_done=finished,
                relationships=by_table(discovery),
                workers=workers,
            )
        finally:
            adapter.close()

        # Each table was persisted as it finished, so the export reads what is
        # stored rather than what this run happened to accumulate.
        report(
            JobProgress(message="Compiling output", tables=planned, completed=list(done))
        )
        document = build_output(
            snapshot,
            workspace.read_facts(),
            workspace.read_questions(),
            workspace.read_evidence(),
        )
        document.write(workspace.output_path)
        return {
            "claims": len(store.facts),
            "questions": len(questions.questions),
            "evidence": len(evidence.records) + len(discovery.evidence.records),
            "relationships": len(discovery.verified),
            "discarded": dropped,
            "tables": planned,
            # Tables the model was cut off on. Their claims are a partial
            # reading, so a run that returns few claims is explained rather
            # than silently thin.
            "partial": partial,
            "skipped": sorted(analyzed - {t.name for t in selected}),
            "output": str(workspace.output_path),
        }

    return get_registry().submit("analyze", name, work)


@app.post("/workspaces/{name}/compile", tags=["pipeline"])
def compile_output(name: str) -> dict:
    """Rebuild output.yaml from the stored snapshot and claims. Fast; inline."""
    workspace = _existing(name)
    document = build_output(
        workspace.read_snapshot(),
        workspace.read_facts(),
        workspace.read_questions(),
        workspace.read_evidence(),
    )
    document.write(workspace.output_path)
    return {"tables": document.table_count, "claims": document.claim_count}


# --- reading ---------------------------------------------------------------


@app.get("/workspaces/{name}/output", tags=["catalogue"])
def get_output(name: str) -> dict:
    """Built on read, never served from the stored file.

    Output is derived from the snapshot and the claims. Serving a cached copy
    means a reviewer approves a claim and the catalogue still shows it as
    generated — the two stores disagree and the UI is the one that looks broken.
    `compile` still writes output.yaml, but as an export, not a source.
    """
    workspace = _existing(name)
    document = build_output(
        workspace.read_snapshot(),
        workspace.read_facts(),
        workspace.read_questions(),
        workspace.read_evidence(),
    )
    return document.model_dump(mode="json", exclude_none=True)


@app.get("/workspaces/{name}/semantic-view", tags=["catalogue"])
def get_semantic_view(name: str, table: str | None = None) -> dict:
    """The emitted view — what an agent would actually be given.

    Distinct from `/output`, which is the catalogue: every claim with its
    evidence, confidence and review state. This is only what survived review,
    phrased for something writing SQL. Both the structured form and the
    rendered YAML are returned, because the console shows the second and an
    agent consumes the first.
    """
    workspace = _existing(name)
    document = build_output(
        workspace.read_snapshot(),
        workspace.read_facts(),
        workspace.read_questions(),
        workspace.read_evidence(),
    )
    view = build_semantic_view(document)
    if table and not any(t.name == table for t in view.tables):
        raise HTTPException(status_code=404, detail=f"{table!r} has not been analysed")
    return {
        "view": view.model_dump(mode="json"),
        "yaml": render_yaml(view, only=table),
        "ready": len(view.ready),
        "tables": len(view.tables),
    }


@app.get("/workspaces/{name}/questions", tags=["review"])
def get_questions(name: str) -> dict[str, list[Question]]:
    return {"questions": _existing(name).read_questions()}


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1)
    reviewer: str = "unknown"


@app.post("/workspaces/{name}/questions/{question_id}/answer", tags=["review"])
def answer_question(name: str, question_id: str, request: AnswerRequest) -> dict:
    """Settle a question, and let the answer count as evidence.

    This is the only path that lifts a business claim past the OBSERVED
    ceiling. `ClaimPolicy` has always allowed it for a human decision; until
    this endpoint existed nothing could produce one, so every semantics claim
    in the catalogue sat at 0.65 with no mechanism able to move it.
    """
    workspace = _existing(name)
    log = QuestionLog(questions=workspace.read_questions())
    question = log.get(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail=f"no question {question_id!r}")
    if question.settled:
        raise HTTPException(
            status_code=409,
            detail=f"already {question.status.value} by {question.answered_by}",
        )

    settled = question.answered(request.answer, request.reviewer)
    facts, evidence, claim = record_answer(
        settled, workspace.read_facts(), workspace.read_evidence()
    )
    facts.write(workspace.facts_path)
    evidence.write(workspace.evidence_path)
    log.replace(settled).write(workspace.questions_path)
    return {"question": settled.model_dump(mode="json"), "claim": claim.model_dump(mode="json")}


@app.post("/workspaces/{name}/questions/{question_id}/dismiss", tags=["review"])
def dismiss_question(name: str, question_id: str, request: AnswerRequest) -> dict:
    """Set a question aside without answering it.

    Not the same as answering: nothing is established, no claim moves, and no
    evidence is recorded. It only stops the question being asked again — the
    column is dead, the distinction does not matter here, or nobody knows.
    """
    workspace = _existing(name)
    log = QuestionLog(questions=workspace.read_questions())
    question = log.get(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail=f"no question {question_id!r}")

    settled = question.dismissed(request.answer, request.reviewer)
    log.replace(settled).write(workspace.questions_path)
    return {"question": settled.model_dump(mode="json")}


@app.get("/workspaces/{name}/claims", tags=["review"])
def get_claims(name: str, status: FactStatus | None = None) -> dict:
    """Claims, highest-impact first: least certain at the top, since a claim
    nobody doubts is not worth a reviewer's attention."""
    workspace = _existing(name)
    facts = assess_facts(workspace.read_facts(), workspace.read_evidence()).facts
    if status:
        facts = [f for f in facts if f.status is status]

    # Consequence outranks confidence: a highly trusted critical claim still
    # matters more than a weakly supported routine one. Confidence is evidence-
    # derived here, never the model's opinion of itself.
    order = {Consequence.CRITICAL: 0, Consequence.HIGH: 1, Consequence.ROUTINE: 2}
    ranked = sorted(facts, key=lambda f: (order[f.consequence], f.confidence))
    return {"count": len(ranked), "claims": [f.model_dump(mode="json") for f in ranked]}


# --- review ----------------------------------------------------------------


@app.post("/workspaces/{name}/claims/{claim_id}/review", tags=["review"])
def review_claim(name: str, claim_id: str, request: ReviewRequest) -> Fact:
    """Record a human verdict.

    Verifying an ungrounded claim is refused: the model invariant forbids it,
    and surfacing that as a 409 is the point — it is the rule that stops a
    confident guess from being promoted to fact by a distracted reviewer.
    """
    workspace = _existing(name)
    store = workspace.read_facts()
    existing = store.by_id(claim_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no claim {claim_id!r}")

    payload = existing.model_dump()
    payload.update(status=request.decision, verified_by=request.reviewer)
    if request.claim is not None:
        payload["claim"] = request.claim

    try:
        updated = Fact.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "this claim has no executed check behind it, so it cannot be marked verified. "
                "Ground it first, or reject it."
            ),
        ) from exc

    FactStore(facts=[f for f in store.facts if f.id != claim_id] + [updated]).write(
        workspace.facts_path
    )
    return updated


@app.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str) -> Job:
    job = get_registry().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return job


@app.get("/jobs", tags=["jobs"])
def list_jobs(workspace: str | None = None) -> dict:
    return {"jobs": [j.model_dump(mode="json") for j in get_registry().list(workspace)]}


# --- the console -----------------------------------------------------------


class SinglePageApp(StaticFiles):
    """The built console, with the fallback a client-side router needs.

    A path like `/workspaces/elara` exists only in the browser's router, so the
    server has to answer it with `index.html`. But only for a *navigation*: a
    missing `/workspaces/typo` requested by fetch must still come back as a 404,
    not as a page of HTML that the caller will try to parse as JSON.
    """

    @staticmethod
    def _is_navigation(scope: Scope) -> bool:
        headers = Headers(raw=scope["headers"])
        wants_html = "text/html" in headers.get("accept", "").lower()
        destination = headers.get("sec-fetch-dest", "").lower()
        return wants_html and destination in {"", "document"}

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and self._is_navigation(scope):
                return await super().get_response("index.html", scope)
            raise


def mount_console(application: FastAPI, directory: Path | None = None) -> None:
    """Serve the console from this process, if it has been built.

    Mounted last, and this is load-bearing: a mount at "/" matches everything,
    so registering it before the routers would swallow the entire API.

    Missing directory is not an error. In development the console runs on its
    own port with a proxy, and there is nothing to serve here.
    """
    static = directory or get_settings().atlas_static_dir
    if not static.is_dir():
        logger.info("no console build at %s — serving the API only", static)
        return
    # `html=True` resolves "/" and any directory to its index.html directly.
    # Without it the bare root only worked because a browser asks for HTML and
    # fell through to the navigation fallback below — which left "/" returning
    # 404 to anything that did not.
    application.mount(
        "/",
        SinglePageApp(directory=static, check_dir=False, html=True),
        name="console",
    )


mount_console(app)
