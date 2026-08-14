"""HTTP API.

Long operations are jobs, never inline requests: extraction takes seconds and
analysis takes minutes per table, which no gateway will hold open. Everything
that reads is synchronous; everything that runs the pipeline returns a job id.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import URL
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from atlas import export
from atlas.adapters.base import UnsupportedDatabase
from atlas.adapters.registry import create_adapter
from atlas.answers import record_answer
from atlas.catalog import (
    Catalog,
    WorkspaceConflict,
    referencing_source,
)

# Aliased: `list_workspaces` is also the name of the route below, and the route
# is the name that belongs to the URL.
from atlas.catalog import list_workspaces as all_workspace_names
from atlas.decisions import record_decision
from atlas.facts import Consequence, Fact, FactStatus
from atlas.jobs import ActiveWorkspaceJob, Job, JobProgress, ProgressReporter, get_registry
from atlas.manifest import InvalidWorkspace, WorkspaceManifest
from atlas.metadata import WorkspaceBusy
from atlas.output import SchemaOutput, assess_facts, build_output
from atlas.questions import Question
from atlas.secrets import get_secret_store
from atlas.semantic_view import build_semantic_view, render_yaml
from atlas.settings import get_settings
from atlas.sources import (
    DuplicateSource,
    Source,
    SourceNotFound,
    get_source_repository,
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load UI-managed credentials when the server starts.

    Deliberately not at import time: importing this module would then read the
    secrets file and mutate the process environment as a side effect, which
    leaks real credentials into anything that merely imports the app — tests
    included.
    """
    loaded = get_secret_store().load_into_environment()
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


class CreateWorkspaceRequest(BaseModel):
    id: str
    source_id: str


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: bool = True
    reset_semantics: bool = Field(
        default=False,
        description="Required when refreshing a workspace that already has semantic state.",
    )


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def _workspace(name: str) -> Catalog:
    try:
        return Catalog(name)
    except InvalidWorkspace as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _existing(name: str) -> Catalog:
    workspace = _workspace(name)
    _ensure_manifest(workspace)
    if not workspace.has_snapshot():
        raise HTTPException(
            status_code=404, detail=f"workspace {name!r} has no snapshot; run extract first"
        )
    _ensure_current(workspace)
    return workspace


def _ensure_manifest(workspace: Catalog) -> WorkspaceManifest:
    if workspace.is_registered():
        return workspace.manifest()
    if not workspace.has_snapshot():
        raise HTTPException(status_code=404, detail=f"workspace {workspace.name!r} does not exist")
    snapshot = workspace.unchecked_snapshot()
    if not snapshot.source_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {workspace.name!r} has no manifest and its snapshot has no source_id; "
                "refusing ambiguous legacy migration"
            ),
        )
    with get_source_repository().lock():
        try:
            get_source_repository().get(snapshot.source_id)
        except SourceNotFound as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"workspace {workspace.name!r} was captured from source {snapshot.source_id!r}, "
                    "but that source is not declared; restore it before migration"
                ),
            ) from exc
        try:
            return workspace.adopt(snapshot.source_id)
        except WorkspaceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _ensure_current(workspace: Catalog) -> None:
    try:
        workspace.snapshot()
    except WorkspaceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _bound_source(workspace: Catalog) -> Source:
    manifest = _ensure_manifest(workspace)
    try:
        return get_source_repository().get(manifest.source_id)
    except SourceNotFound as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"workspace {workspace.name!r} is bound to source {manifest.source_id!r}, "
                "but that source is not declared; cached reads remain available, live operations do not"
            ),
        ) from exc


def _source_url(source: Source) -> str:
    try:
        return source.resolve_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _active_job_conflict(exc: ActiveWorkspaceJob) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"message": "workspace already has an active mutation job", "job_id": exc.job_id},
    )


def _submit_mutation(
    kind: str, workspace: Catalog, manifest: WorkspaceManifest, work
) -> Job:
    registry = get_registry()

    def guarded_work(report: ProgressReporter) -> dict:
        with workspace.mutation():
            return work(report)

    active = registry.active_workspace_job(workspace.name)
    if active:
        raise _active_job_conflict(ActiveWorkspaceJob(active.id))
    try:
        with (
            registry.workspace_guard(workspace.name),
            workspace.mutation(blocking=False),
        ):
            workspace.assert_identity(
                source_id=manifest.source_id,
                incarnation_id=manifest.incarnation_id,
                generation=manifest.snapshot_generation,
            )
            return registry.submit(
                kind,
                workspace.name,
                guarded_work,
                snapshot_generation=manifest.snapshot_generation,
                source_id=manifest.source_id,
                workspace_incarnation=manifest.incarnation_id,
                exclusive=True,
            )
    except ActiveWorkspaceJob as exc:
        raise _active_job_conflict(exc) from exc
    except (WorkspaceBusy, WorkspaceConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _workspace_write_guard(name: str) -> Iterator[None]:
    """Keep synchronous reviews/compiles out of a mutating job's write window."""
    registry = get_registry()
    active = registry.active_workspace_job(name)
    if active:
        raise _active_job_conflict(ActiveWorkspaceJob(active.id))
    with registry.workspace_guard(name):
        # Close the race where a job was submitted after the first check but
        # before this synchronous writer acquired the workspace lock.
        active = registry.active_workspace_job(name)
        if active:
            raise _active_job_conflict(ActiveWorkspaceJob(active.id))
        try:
            with Catalog(name).mutation(blocking=False):
                yield
        except WorkspaceBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


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
        managed=get_secret_store().has(source.url_env),
        health=_health.get(source.id, ConnectionHealth()),
    )


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
    return {"sources": [_status(s) for s in get_source_repository().list()]}


@app.post("/sources", status_code=201, tags=["sources"])
def create_source(source: Source) -> SourceStatus:
    try:
        get_source_repository().add(source)
    except DuplicateSource as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Probe on creation: the answer to "did that work" belongs in the response
    # to the action, not behind a second button the user has to know to press.
    _probe(source)
    return _status(source)


class CredentialRequest(BaseModel):
    url: str = Field(min_length=1, description="Full SQLAlchemy connection URL")


#: Auth methods that need a person at the keyboard. Atlas's real work is a
#: background job — extraction takes seconds and analysis minutes per table —
#: so a credential of this kind authenticates once and then cannot be reused
#: when the job actually runs. MFA token caching narrows the window; it does not
#: close it, because the token expires on the account's schedule and nobody is
#: there to refresh it.
INTERACTIVE_AUTH = frozenset({"mfa_push", "mfa_totp", "external_browser"})


class SnowflakeCredentialRequest(BaseModel):
    account_identifier: str = Field(min_length=1, examples=["myorg-myaccount"])
    username: str = Field(min_length=1)
    auth_method: Literal[
        "password", "key_pair", "mfa_push", "mfa_totp", "external_browser"
    ] = "password"
    password: SecretStr | None = Field(default=None, min_length=1)
    passcode: SecretStr | None = Field(default=None, min_length=1)
    # Read by the engine host, never uploaded: a private key must not travel
    # through an unauthenticated API, and the file the connector wants is
    # already on the machine that will use it.
    private_key_file: str | None = Field(
        default=None, min_length=1, examples=["/etc/atlas/snowflake_key.p8"]
    )
    private_key_file_pwd: SecretStr | None = Field(default=None, min_length=1)
    warehouse: str = Field(min_length=1)
    role: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_credentials_for_method(self) -> Self:
        if self.auth_method == "key_pair":
            if not self.private_key_file:
                raise ValueError("private_key_file is required for key-pair authentication")
            return self
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
    if request.auth_method == "key_pair":
        # SNOWFLAKE_JWT with a key file on the engine host. The only method here
        # that needs nobody present, which is what an unattended job requires.
        query["authenticator"] = "SNOWFLAKE_JWT"
        query["private_key_file"] = request.private_key_file or ""
        if request.private_key_file_pwd:
            query["private_key_file_pwd"] = request.private_key_file_pwd.get_secret_value()
        password = None
    elif request.auth_method in {"mfa_push", "mfa_totp"}:
        query["authenticator"] = "username_password_mfa"
        # Asks Snowflake for a reusable MFA token. It is only honoured when the
        # account sets ALLOW_CLIENT_MFA_CACHING and the connector can persist it
        # — which needs `keyring`, hence the secure-local-storage extra.
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
        source = get_source_repository().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc

    get_secret_store().set(source.url_env, request.url.strip())
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
        source = get_source_repository().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    if source.adapter != "snowflake":
        raise HTTPException(status_code=409, detail="source is not a Snowflake connection")

    stored_url = _snowflake_url(source, request)
    get_secret_store().set(source.url_env, stored_url)
    if request.auth_method == "mfa_totp":
        # A TOTP code is single-use and expires quickly. Use it only for this
        # connection attempt; never persist it beside the durable credential.
        health = _probe(source, _snowflake_url(source, request, include_passcode=True))
    else:
        health = _probe(source)

    # A green test on an interactive method proves only that a person was here.
    # The credential Atlas stores has no passcode in it, so the next background
    # job authenticates with whatever survives — and if nothing does, it fails
    # minutes into a run rather than here. Say so while the reader is looking.
    if health.state == "connected" and request.auth_method in INTERACTIVE_AUTH:
        health = health.model_copy(
            update={
                "detail": " ".join(
                    filter(
                        None,
                        [
                            health.detail,
                            (
                                "Connected interactively. Background extraction and analysis "
                                "run with no one present: this keeps working only while "
                                "Snowflake honours a cached MFA token, which needs "
                                "ALLOW_CLIENT_MFA_CACHING on the account and expires on its "
                                "schedule. Use key-pair or a programmatic token for "
                                "unattended runs."
                            ),
                        ],
                    )
                )
            }
        )
    return health


@app.delete("/sources/{source_id}/credentials", status_code=204, tags=["sources"])
def forget_credentials(source_id: str) -> None:
    try:
        source = get_source_repository().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    get_secret_store().clear(source.url_env)
    _health.pop(source_id, None)


@app.delete("/sources/{source_id}", status_code=204, tags=["sources"])
def delete_source(source_id: str) -> None:
    sources = get_source_repository()
    # One lock over both halves. Between "nothing references this" and the
    # delete, a workspace can be created that binds to it — and the workspace
    # would come back pointing at a source that no longer exists.
    with sources.lock():
        referenced = referencing_source(source_id)
        if referenced:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "source is still referenced by workspaces",
                    "workspaces": referenced,
                },
            )
        try:
            sources.remove(source_id)
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc


@app.post("/sources/{source_id}/test", tags=["sources"])
def test_source(source_id: str) -> ConnectionHealth:
    """Connect and report what came back.

    Runs inline rather than as a job: two queries, and a setup form needs the
    answer before the user moves on.
    """
    try:
        source = get_source_repository().get(source_id)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no source {source_id!r}") from exc
    return _probe(source)


class WorkspaceSummary(BaseModel):
    id: str
    source_id: str
    adapter: str | None = None
    namespace: str | None = None
    source_label: str | None = None
    snapshot_generation: int
    snapshot_available: bool
    snapshot_time: datetime | None = None
    source_available: bool
    source_configured: bool = False
    source_health: ConnectionHealth = ConnectionHealth()


def _workspace_summary(name: str) -> WorkspaceSummary:
    workspace = _workspace(name)
    manifest = _ensure_manifest(workspace)
    snapshot = None
    if workspace.has_snapshot():
        _ensure_current(workspace)
        snapshot = workspace.unchecked_snapshot()
    try:
        source = get_source_repository().get(manifest.source_id)
    except SourceNotFound:
        return WorkspaceSummary(
            id=name,
            source_id=manifest.source_id,
            snapshot_generation=manifest.snapshot_generation,
            snapshot_available=snapshot is not None,
            snapshot_time=snapshot.extracted_at if snapshot else None,
            source_available=False,
            source_health=ConnectionHealth(
                state="failed", detail=f"source {manifest.source_id!r} is not declared"
            ),
        )
    health = _health.get(source.id, ConnectionHealth())
    return WorkspaceSummary(
        id=name,
        source_id=manifest.source_id,
        adapter=source.adapter,
        namespace=source.namespace,
        source_label=source.label,
        snapshot_generation=manifest.snapshot_generation,
        snapshot_available=snapshot is not None,
        snapshot_time=snapshot.extracted_at if snapshot else None,
        source_available=source.configured and health.state != "failed",
        source_configured=source.configured,
        source_health=health,
    )


@app.post("/workspaces", status_code=201, tags=["workspaces"])
def create_workspace(request: CreateWorkspaceRequest) -> WorkspaceSummary:
    with get_source_repository().lock():
        try:
            get_source_repository().get(request.source_id)
        except SourceNotFound as exc:
            raise HTTPException(status_code=404, detail=f"no source {request.source_id!r}") from exc
        referenced = referencing_source(request.source_id)
        if referenced and request.id not in referenced:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "source already has a workspace",
                    "workspaces": referenced,
                },
            )
        workspace = _workspace(request.id)
        try:
            workspace.register(request.source_id)
        except WorkspaceConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _workspace_summary(workspace.name)


@app.get("/workspaces", tags=["workspaces"])
def list_workspaces() -> dict[str, list[WorkspaceSummary]]:
    return {"workspaces": [_workspace_summary(name) for name in all_workspace_names()]}


@app.delete("/workspaces/{name}", status_code=204, tags=["workspaces"])
def delete_workspace(
    name: str, _guard: None = Depends(_workspace_write_guard)
) -> None:
    workspace = _workspace(name)
    workspace.delete()
    get_registry().delete_workspace(name)


@app.post("/workspaces/{name}/extract", status_code=202, tags=["pipeline"])
def extract(name: str, request: ExtractRequest = Body(default=ExtractRequest())) -> Job:
    """Capture the physical layer for the workspace's bound source."""
    workspace = _workspace(name)
    manifest = _ensure_manifest(workspace)
    source = _bound_source(workspace)
    url = _source_url(source)
    if workspace.has_snapshot():
        _ensure_current(workspace)
        if workspace.has_semantics() and not request.reset_semantics:
            raise HTTPException(
                status_code=409,
                detail=(
                    "refreshing this workspace would discard semantic state; "
                    "retry with reset_semantics=true to confirm"
                ),
            )
    start_generation = manifest.snapshot_generation

    def work(report: ProgressReporter) -> dict:
        with create_adapter(url) as adapter:
            report(JobProgress(message="Connecting"))
            adapter.test_connection()
            report(JobProgress(message="Reading structure"))
            snapshot = adapter.extract_structure(source.namespace)

            if request.profile:
                # Profiling is the long half and it is per table, so it is
                # reported per table. A single "profiling 20 tables" line left
                # minutes of silence on a warehouse, which reads as a hang and
                # is why the button got pressed again.
                names = [table.name for table in snapshot.tables]
                done: list[str] = []

                def started(name: str) -> None:
                    report(
                        JobProgress(
                            message=f"Profiling {name}",
                            tables=names,
                            completed=list(done),
                            current=[name],
                        )
                    )
                    done.append(name)

                report(JobProgress(message="Profiling columns", tables=names))
                snapshot = adapter.profile(snapshot, on_table=started)

            report(JobProgress(message="Saving snapshot"))
        workspace.assert_identity(
            source_id=manifest.source_id,
            incarnation_id=manifest.incarnation_id,
            generation=start_generation,
        )
        published = workspace.publish(
            snapshot,
            reset_semantics=request.reset_semantics,
            expected_source_id=manifest.source_id,
            expected_incarnation_id=manifest.incarnation_id,
            expected_generation=start_generation,
        )
        # No path in the result. Where the record physically sits is the
        # metadata store's business, and a caller told about it is a caller
        # that will eventually read it.
        return {
            "tables": len(snapshot.tables),
            "snapshot_generation": published.snapshot_generation,
        }

    return _submit_mutation("extract", workspace, manifest, work)


@app.post("/workspaces/{name}/analyze", status_code=202, tags=["pipeline"])
def analyze(name: str, request: AnalyzeRequest = Body(default=AnalyzeRequest())) -> Job:
    """Run the analysis agent. Minutes per table — always poll the job."""
    from atlas.agent import (
        AnalysisSink,
        analyze_schema,
        completely_described_tables,
        repair_columns_by_table,
        select_tables,
    )
    from atlas.relationships import as_claims, by_table, discover

    workspace = _existing(name)
    manifest = workspace.manifest()
    source = _bound_source(workspace)
    url = _source_url(source)
    start_generation = manifest.snapshot_generation
    if get_settings().openrouter_api_key is None:
        raise HTTPException(
            status_code=400,
            detail="No model API key configured. Set OPENROUTER_API_KEY in engine/.env "
            "and restart the engine.",
        )

    def assert_current() -> None:
        workspace.assert_identity(
            source_id=manifest.source_id,
            incarnation_id=manifest.incarnation_id,
            generation=start_generation,
        )

    def work(report: ProgressReporter) -> dict:
        assert_current()
        snapshot = workspace.snapshot()
        existing = workspace.facts()
        # A table is complete only when every physical column has a semantics
        # claim. Counting any non-join fact as "analyzed" made an early model
        # finish permanent: the warning named missing columns, but every later
        # run skipped the table that needed repair.
        analyzed = completely_described_tables(snapshot, existing.facts)
        repairs = (
            {} if request.regenerate else repair_columns_by_table(snapshot, existing.facts)
        )

        selected = select_tables(
            snapshot,
            request.limit,
            request.tables,
            set() if request.regenerate else analyzed,
        )
        if not selected:
            return {"claims": 0, "questions": 0, "skipped": sorted(analyzed), "tables": []}

        names = {t.name for t in selected}
        assert_current()
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
        assert_current()
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
            assert_current()
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
                repair_columns=repairs,
                workers=workers,
            )
        finally:
            adapter.close()

        # Nothing is compiled here. Each table was persisted as it finished and
        # the catalogue is built from the record on every read, so there is no
        # projection left to write at the end of a run. The last check is that
        # the workspace this run wrote to is still the one it started on.
        assert_current()
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
        }

    return _submit_mutation("analyze", workspace, manifest, work)


# There is no `compile` endpoint. It existed to write output.yaml into the
# store, and the store no longer keeps a projection — `GET /output` builds one
# from the record on every request, so there was nothing left for it to do.
# Exporting the document to a file is `atlas compile --out`, where the caller
# names the path, which a server has no way to do.


def _build(workspace: Catalog) -> SchemaOutput:
    """The projection, from the stored record. Rebuilt per request: serving a
    cached copy means a reviewer approves a claim and the catalogue still shows
    it as generated, and the UI is the one that looks broken."""
    return build_output(
        workspace.snapshot(),
        workspace.facts(),
        workspace.questions().questions,
        workspace.evidence(),
    )


# --- reading ---------------------------------------------------------------


@app.get("/workspaces/{name}/output", tags=["catalogue"])
def get_output(name: str) -> dict:
    """Built on read, never served from the stored file.

    Output is derived from the snapshot and the claims. Serving a cached copy
    means a reviewer approves a claim and the catalogue still shows it as
    generated — the two stores disagree and the UI is the one that looks broken.
    `compile` still writes output.yaml, but as an export, not a source.
    """
    return _build(_existing(name)).model_dump(mode="json", exclude_none=True)


@app.get("/workspaces/{name}/semantic-view", tags=["catalogue"])
def get_semantic_view(name: str, table: str | None = None) -> dict:
    """The emitted view — what an agent would actually be given.

    Distinct from `/output`, which is the catalogue: every claim with its
    evidence, confidence and review state. This is only what survived review,
    phrased for something writing SQL. Both the structured form and the
    rendered YAML are returned, because the console shows the second and an
    agent consumes the first.
    """
    view = build_semantic_view(_build(_existing(name)))
    if table and not any(t.name == table for t in view.tables):
        raise HTTPException(status_code=404, detail=f"{table!r} has not been analysed")
    return {
        "view": view.model_dump(mode="json"),
        "yaml": render_yaml(view, only=table),
        "ready": len(view.ready),
        "tables": len(view.tables),
    }


@app.get("/workspaces/{name}/export", tags=["catalogue"])
def export_view(
    name: str,
    request: Request,
    format: Literal["yaml", "json"] = "yaml",
    include: Literal["ready", "all"] = "ready",
    table: str | None = None,
    download: bool = False,
) -> Response:
    """The semantic view, packaged to leave.

    One URL serving two uses. Without `download` it is a resource an agent or a
    CI job can keep fetching, tagged so an unchanged view costs a 304; with it,
    the browser saves a file. They are the same bytes, and making them two
    endpoints would let the file and the served view drift.

    Gated on review by default. `/semantic-view` carries every captured table
    with its state in comments, which is right for a console showing progress —
    but a file that leaves the building is read by something that does not read
    comments, so what has not passed review is held back unless `include=all`
    says otherwise, and the header then says so in the file itself.
    """
    workspace = _existing(name)
    output = _build(workspace)
    try:
        view, excluded = export.select(
            output, ready_only=include == "ready", table=table
        )
    except export.UnknownTable as exc:
        raise HTTPException(
            status_code=404, detail=f"{table!r} is not in this workspace"
        ) from exc

    generation = workspace.manifest().snapshot_generation
    tag = export.etag(view, excluded, generation=generation, fmt=format)
    # Checked before rendering: the point of the tag is to avoid the work, and
    # rendering first to then throw it away would only avoid the transfer.
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})

    body = export.render(
        view,
        excluded,
        workspace=name,
        generation=generation,
        total=len(output.tables),
        fmt=format,
    )
    headers = {"ETag": tag}
    if download:
        # The generation is in the filename because a file on someone's disk
        # has to be able to say which capture it came from; nothing else about
        # it will.
        suffix = f"-{table}" if table else ""
        headers["Content-Disposition"] = (
            f'attachment; filename="{name}{suffix}-gen{generation}-semantic-view.{format}"'
        )
    return Response(
        content=body,
        media_type="application/yaml" if format == "yaml" else "application/json",
        headers=headers,
    )


@app.get("/workspaces/{name}/questions", tags=["review"])
def get_questions(name: str) -> dict[str, list[Question]]:
    return {"questions": _existing(name).questions().questions}


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1)
    reviewer: str = "unknown"


@app.post("/workspaces/{name}/questions/{question_id}/answer", tags=["review"])
def answer_question(
    name: str,
    question_id: str,
    request: AnswerRequest,
    _guard: None = Depends(_workspace_write_guard),
) -> dict:
    """Settle a question, and let the answer count as evidence.

    This is the only path that lifts a business claim past the OBSERVED
    ceiling. `ClaimPolicy` has always allowed it for a human decision; until
    this endpoint existed nothing could produce one, so every semantics claim
    in the catalogue sat at 0.65 with no mechanism able to move it.
    """
    workspace = _existing(name)
    log = workspace.questions()
    question = log.get(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail=f"no question {question_id!r}")
    if question.settled:
        raise HTTPException(
            status_code=409,
            detail=f"already {question.status.value} by {question.answered_by}",
        )

    settled = question.answered(request.answer, request.reviewer)
    facts, evidence, claim = record_answer(settled, workspace.facts(), workspace.evidence())
    workspace.record_answered_question(settled, facts, evidence)
    return {"question": settled.model_dump(mode="json"), "claim": claim.model_dump(mode="json")}


@app.post("/workspaces/{name}/questions/{question_id}/dismiss", tags=["review"])
def dismiss_question(
    name: str,
    question_id: str,
    request: AnswerRequest,
    _guard: None = Depends(_workspace_write_guard),
) -> dict:
    """Set a question aside without answering it.

    Not the same as answering: nothing is established, no claim moves, and no
    evidence is recorded. It only stops the question being asked again — the
    column is dead, the distinction does not matter here, or nobody knows.
    """
    workspace = _existing(name)
    log = workspace.questions()
    question = log.get(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail=f"no question {question_id!r}")

    settled = question.dismissed(request.answer, request.reviewer)
    workspace.settle_question(settled)
    return {"question": settled.model_dump(mode="json")}


@app.get("/workspaces/{name}/claims", tags=["review"])
def get_claims(name: str, status: FactStatus | None = None) -> dict:
    """Claims, highest-impact first: least certain at the top, since a claim
    nobody doubts is not worth a reviewer's attention."""
    workspace = _existing(name)
    facts = assess_facts(workspace.facts(), workspace.evidence()).facts
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
def review_claim(
    name: str,
    claim_id: str,
    request: ReviewRequest,
    _guard: None = Depends(_workspace_write_guard),
) -> Fact:
    """Record a human verdict, as evidence.

    A decision is written to the evidence store and linked to the claim; where
    the claim now stands is derived from that record, never stored on it. See
    `APPROVAL.md`.

    This no longer refuses an ungrounded claim. The 409 it used to raise existed
    because `enforce_grounding_rules` forbids `verified` without an executed
    check — but a human decision *is* grounding (`Fact.is_grounded` accepts
    `ProvenanceKind.HUMAN`), so the condition is not reachable. What the refusal
    protected against is preserved instead by keeping the states apart: a claim a
    check established reads as `verified`, a claim only a person asserted reads
    as `authoritative`, and the emitted view says which.
    """
    workspace = _existing(name)
    store = workspace.facts()
    existing = store.by_id(claim_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no claim {claim_id!r}")

    evidence = workspace.evidence()
    updated, evidence = record_decision(
        existing,
        evidence,
        reviewer=request.reviewer,
        decision=request.decision,
        text=request.claim,
    )

    # One claim and its decision, not the whole store rewritten around them.
    # Two reviewers settling different claims a second apart both keep their
    # verdict; the wholesale write kept only whichever landed second.
    workspace.record_review(updated, evidence)
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

#: The prefix the console addresses the engine under.
#:
#: `console/src/store/api.ts` uses `/api`, and in development `vite.config.ts`
#: proxies it away before the request arrives. In the built image there is no
#: proxy — this process serves both — so without the middleware below every
#: console fetch asked for `/api/workspaces/...`, found no route, fell through
#: to the static mount and came back 404. The console worked in development and
#: could not reach its own engine in the artifact we ship.
#:
#: Stripping it here rather than re-declaring every route under a prefix keeps
#: one URL correct in development, in the image, and for anything outside
#: holding a link to an export.
API_PREFIX = "/api"


class ApiPrefixMiddleware:
    """Route `/api/x` as `/x`.

    Deliberately ASGI rather than a FastAPI dependency: the path has to change
    before routing happens, and by the time a dependency runs the route has
    already been chosen — or, in this case, already failed to be.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
                # `raw_path` is what Starlette prefers when present, so leaving
                # it stale would route the original path regardless of `path`.
                scope = {**scope, "path": path[len(API_PREFIX) :] or "/"}
                scope.pop("raw_path", None)
        await self.app(scope, receive, send)


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

# Outermost, so the prefix is gone before routing and before the static mount
# at "/" gets a chance to answer an API path with index.html.
app.add_middleware(ApiPrefixMiddleware)
