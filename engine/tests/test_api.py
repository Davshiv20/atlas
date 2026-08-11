from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas.api import app
from atlas.evidence import EvidenceStore
from atlas.facts import Fact, FactStatus, FactStore, Provenance, ProvenanceKind
from atlas.jobs import (
    TERMINAL,
    Job,
    JobProgress,
    JobRegistry,
    JobStatus,
    ProgressReporter,
    get_registry,
)
from atlas.questions import QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Column, Snapshot, Table
from atlas.workspace import InvalidWorkspace, Workspace

CHECK = Provenance(kind=ProvenanceKind.GROUNDED_CHECK, detail="executed: SELECT 1", result="pass")
GUESS = Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from column name")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Isolate every test from the developer's machine.

    Settings read `.env` relative to the working directory, so without the
    chdir a developer with a populated engine/.env sees different results than
    CI — precondition tests pass locally and fail in the pipeline, or worse,
    the reverse.
    """
    monkeypatch.chdir(tmp_path)
    for leaked in (
        "ATLAS_DATABASE_URL",
        "OPENROUTER_API_KEY",
        "ATLAS_MODEL",
        "ATLAS_EFFORT",
        "ELARA_DATABASE_URL",
    ):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    # The registry is cached per process and holds an open path. Without this a
    # later test reuses a database inside an already-deleted tmp directory.
    get_registry.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_registry.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def seed(name: str = "demo") -> Workspace:
    workspace = Workspace(name)
    snapshot = Snapshot(
        database="shop",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(
                schema_name="public",
                name="orders",
                columns=[Column(name="status", data_type="VARCHAR", nullable=False)],
                exact_rows=3,
            )
        ],
    )
    snapshot.write(workspace.snapshot_path)
    FactStore(
        facts=[
            Fact(
                subject="orders",
                aspect="grain",
                claim="One row per order.",
                confidence=0.92,
                provenance=[GUESS, CHECK],
            ),
            Fact(
                subject="orders.status",
                aspect="semantics",
                claim="Lifecycle state.",
                confidence=0.45,
                provenance=[GUESS],
            ),
        ]
    ).write(workspace.facts_path)
    return workspace


def test_health(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_config_never_returns_the_key(client, monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")
    body = client.get("/config").json()
    assert body["api_key_configured"] is True
    assert "sk-or-v1-secret" not in str(body)


def test_workspace_names_are_validated_not_sanitized() -> None:
    for bad in ["../etc", "Has Caps", "", "a" * 64, "with/slash"]:
        with pytest.raises(InvalidWorkspace):
            Workspace(bad)


def test_traversal_name_is_rejected_by_the_api(client) -> None:
    assert client.get("/workspaces/..%2Fetc/questions").status_code in (400, 404)


def test_missing_workspace_is_404(client) -> None:
    response = client.get("/workspaces/nope/output")
    assert response.status_code == 404
    assert "extract first" in response.json()["detail"]


def test_claims_are_ranked_least_certain_first(client) -> None:
    seed()
    claims = client.get("/workspaces/demo/claims").json()["claims"]
    assert [c["confidence"] for c in claims] == [0.45, 0.92]


def test_claims_filter_by_status(client) -> None:
    seed()
    body = client.get("/workspaces/demo/claims", params={"status": "verified"}).json()
    assert body["count"] == 0


def test_grounded_claim_can_be_verified(client) -> None:
    seed()
    response = client.post(
        "/workspaces/demo/claims/orders%23grain/review",
        json={"decision": "verified", "reviewer": "shivam"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "verified"


def test_endorsing_an_ungrounded_claim_grounds_it_rather_than_refusing(client) -> None:
    """The 409 is gone, and what it protected is kept a different way.

    Refusing was the old way of stopping a guess being promoted by a distracted
    reviewer. A human decision is now evidence, so endorsing an ungrounded claim
    grounds it — the record says a person asserted this, under their name,
    against the observations they were shown. What must not happen is the result
    becoming indistinguishable from a claim a check established.
    """
    seed()
    response = client.post(
        "/workspaces/demo/claims/orders.status%23semantics/review",
        json={"decision": "verified", "reviewer": "shivam"},
    )
    assert response.status_code == 200

    fact = response.json()
    human = [p for p in fact["provenance"] if p["kind"] == "human"]
    assert human, "the decision must be recorded on the claim, attributed"
    assert "shivam" in human[-1]["detail"]

    checks = [p for p in fact["provenance"] if p["kind"] == "grounded_check"]
    assert not checks, "asserting must not fabricate an executed check"


def test_ungrounded_claim_can_still_be_rejected(client) -> None:
    seed()
    response = client.post(
        "/workspaces/demo/claims/orders.status%23semantics/review",
        json={"decision": "rejected", "reviewer": "shivam"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_review_persists(client) -> None:
    workspace = seed()
    client.post(
        "/workspaces/demo/claims/orders%23grain/review",
        json={"decision": "verified", "reviewer": "shivam", "claim": "One row per issued order."},
    )
    stored = FactStore.read(workspace.facts_path).by_id("orders#grain")
    assert stored.status is FactStatus.VERIFIED
    assert stored.claim == "One row per issued order."
    assert stored.verified_by == "shivam"


def test_unknown_claim_is_404(client) -> None:
    seed()
    response = client.post(
        "/workspaces/demo/claims/nope%23nope/review",
        json={"decision": "verified", "reviewer": "shivam"},
    )
    assert response.status_code == 404


def test_compile_then_read_output(client) -> None:
    seed()
    assert client.post("/workspaces/demo/compile").json()["tables"] == 1
    output = client.get("/workspaces/demo/output").json()
    assert output["tables"][0]["grain"]["text"] == "One row per order."


def test_analyze_requires_an_api_key(client) -> None:
    seed()
    response = client.post(
        "/workspaces/demo/analyze",
        json={"limit": 1, "database_url": "postgresql+psycopg://u:p@localhost/db"},
    )
    assert response.status_code == 400
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_analyze_requires_a_database_url(client) -> None:
    seed()
    response = client.post("/workspaces/demo/analyze", json={"limit": 1})
    assert response.status_code == 400
    assert "ATLAS_DATABASE_URL" in response.json()["detail"]


def test_workspaces_are_listed_once_extracted(client) -> None:
    seed("alpha")
    seed("beta")
    assert client.get("/workspaces").json()["workspaces"] == ["alpha", "beta"]


def test_unknown_job_is_404(client) -> None:
    assert client.get("/jobs/job_missing").status_code == 404


# --- review at scale -------------------------------------------------------


def seed_wide(name: str = "wide") -> Workspace:
    """A table shaped like the problem: a handful of meaningful columns buried
    in dozens whose meaning their type already fixes."""
    workspace = Workspace(name)
    columns = [Column(name="id", data_type="VARCHAR", nullable=False, is_primary_key=True)]
    columns += [
        Column(name=f"{stem}_at", data_type="VARCHAR", nullable=True)
        for stem in ("created", "updated", "deleted", "archived", "synced")
    ]
    columns += [
        Column(name="status", data_type="VARCHAR", nullable=False),
        Column(name="amount_cents", data_type="INTEGER", nullable=False),
    ]
    Snapshot(
        database="shop",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(schema_name="public", name="orders", columns=columns, exact_rows=100)
        ],
    ).write(workspace.snapshot_path)

    facts = [
        Fact(
            subject=f"orders.{stem}_at",
            aspect="semantics",
            claim=f"ISO-8601 string recording when the row was {stem}.",
            confidence=0.92,
            provenance=[GUESS, CHECK],
            consequence="routine",
            status="auto_accepted",
        )
        for stem in ("created", "updated", "deleted", "archived", "synced")
    ]
    facts.append(
        Fact(
            subject="orders.status",
            aspect="semantics",
            claim="Order lifecycle state.",
            confidence=0.75,
            provenance=[GUESS, CHECK],
            consequence="high",
        )
    )
    FactStore(facts=facts).write(workspace.facts_path)
    return workspace



def test_class_review_never_overwrites_a_deliberate_decision(client) -> None:
    workspace = seed_wide()
    client.post(
        "/workspaces/wide/claims/orders.created_at%23semantics/review",
        json={"decision": "rejected", "reviewer": "shivam"},
    )
    client.post(
        "/workspaces/wide/classes/audit_timestamp:varchar/review",
        json={"decision": "verified", "reviewer": "someone-else"},
    )
    stored = FactStore.read(workspace.facts_path).by_id("orders.created_at#semantics")
    assert stored.status is FactStatus.REJECTED




def test_unknown_class_is_404(client) -> None:
    seed_wide()
    response = client.post(
        "/workspaces/wide/classes/nope:varchar/review",
        json={"decision": "verified", "reviewer": "shivam"},
    )
    assert response.status_code == 404


def test_claims_rank_by_consequence_before_confidence(client) -> None:
    seed_wide()
    claims = client.get("/workspaces/wide/claims").json()["claims"]
    assert claims[0]["subject"] == "orders.status"  # high beats routine
    assert all(c["consequence"] == "routine" for c in claims[1:])


# --- choosing what to analyze ----------------------------------------------


def test_already_analyzed_tables_are_skipped_by_default() -> None:
    """Re-deriving a table costs money and, worse, reworded claims reset the
    human verdicts already recorded against them."""
    from atlas.agent import select_tables

    snapshot = Snapshot(
        database="d", schema_name="public", dialect="postgresql",
        tables=[
            Table(schema_name="public", name=n, columns=[], exact_rows=10)
            for n in ("orders", "customers", "shipments")
        ],
    )
    selected = select_tables(snapshot, already_analyzed={"orders"})
    assert [t.name for t in selected] == ["customers", "shipments"]


def test_named_tables_override_ranking_and_the_skip() -> None:
    from atlas.agent import select_tables

    snapshot = Snapshot(
        database="d", schema_name="public", dialect="postgresql",
        tables=[
            Table(schema_name="public", name=n, columns=[], exact_rows=10)
            for n in ("orders", "customers")
        ],
    )
    selected = select_tables(snapshot, tables=["orders"], already_analyzed={"orders"})
    assert [t.name for t in selected] == ["orders"]


def test_no_limit_means_every_remaining_table() -> None:
    from atlas.agent import select_tables

    snapshot = Snapshot(
        database="d", schema_name="public", dialect="postgresql",
        tables=[
            Table(schema_name="public", name=f"t{i}", columns=[], exact_rows=i)
            for i in range(12)
        ],
    )
    assert len(select_tables(snapshot)) == 12
    assert len(select_tables(snapshot, limit=3)) == 3


def test_migration_bookkeeping_is_never_analyzed() -> None:
    from atlas.agent import select_tables

    snapshot = Snapshot(
        database="d", schema_name="public", dialect="postgresql",
        tables=[
            Table(schema_name="public", name="alembic_version", columns=[], exact_rows=1),
            Table(schema_name="public", name="orders", columns=[], exact_rows=10),
        ],
    )
    assert [t.name for t in select_tables(snapshot)] == ["orders"]


# --- analyze resolves the same source extract used --------------------------


def test_analyze_uses_the_source_its_snapshot_came_from(client, monkeypatch) -> None:
    """The bug behind every /analyze returning 400: extract learned about
    sources and analyze did not, so it still demanded ATLAS_DATABASE_URL."""
    monkeypatch.setenv("ELARA_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    get_settings.cache_clear()

    client.post(
        "/sources",
        json={
            "id": "elara",
            "adapter": "postgresql",
            "url_env": "ELARA_DATABASE_URL",
            "namespace": "public",
        },
    )
    workspace = seed("demo")
    snapshot = workspace.read_snapshot()
    snapshot.model_copy(update={"source_id": "elara"}).write(workspace.snapshot_path)

    # Accepted — the URL resolves from the source, not from ATLAS_DATABASE_URL.
    assert client.post("/workspaces/demo/analyze", json={"limit": 1}).status_code == 202


def test_a_missing_source_credential_explains_itself(client, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    get_settings.cache_clear()
    client.post(
        "/sources",
        json={
            "id": "elara",
            "adapter": "postgresql",
            "url_env": "ELARA_DATABASE_URL",
            "namespace": "public",
        },
    )
    workspace = seed("demo")
    snapshot = workspace.read_snapshot()
    snapshot.model_copy(update={"source_id": "elara"}).write(workspace.snapshot_path)

    response = client.post("/workspaces/demo/analyze", json={"limit": 1})
    assert response.status_code == 400
    assert "No connection string for 'elara'" in response.json()["detail"]


def test_a_missing_model_key_names_the_variable(client, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/x")
    get_settings.cache_clear()
    seed("demo")
    response = client.post("/workspaces/demo/analyze", json={"limit": 1})
    assert response.status_code == 400
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_a_running_job_reports_which_table_it_is_on(tmp_path) -> None:
    """A spinner with nothing behind it is what the console had before: the
    reviewer could not tell which of five tables the run was spending minutes
    on, nor how far through it was."""
    registry = JobRegistry(tmp_path / "jobs.db")
    observed: list[JobProgress] = []

    def work(report: ProgressReporter) -> dict:
        report(JobProgress(message="Reading users", tables=TWO, current=["users"]))
        # Read it back out of the store, not out of the object we just made:
        # the point is that a *reader* can see it.
        observed.append(registry.get(report.job_id).progress)
        report(JobProgress(message="Analyzed users", tables=TWO, completed=["users"]))
        observed.append(registry.get(report.job_id).progress)
        return {"claims": 3}

    job = _finish(registry, registry.submit("analyze", "demo", work))

    assert observed[0].current == ["users"]
    assert observed[1].completed == ["users"]
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"claims": 3}


TWO = ["users", "clients"]


def _finish(registry: JobRegistry, job: Job) -> Job:
    while (current := registry.get(job.id)).status not in TERMINAL:
        pass
    return current


def test_a_job_survives_a_restart_of_the_process(tmp_path) -> None:
    """The whole point of leaving memory: a run used to become invisible the
    moment the engine reloaded, so the console showed an idle workspace."""
    first = JobRegistry(tmp_path / "jobs.db")
    job = _finish(first, first.submit("analyze", "demo", lambda report: {"claims": 1}))

    reopened = JobRegistry(tmp_path / "jobs.db")
    assert reopened.get(job.id).result == {"claims": 1}
    assert [j.id for j in reopened.list("demo")] == [job.id]


def test_an_orphaned_run_is_settled_rather_than_left_running(tmp_path) -> None:
    """A persisted job left at RUNNING is worse than a lost one: its thread died
    with the old process, so the console would poll it forever."""
    registry = JobRegistry(tmp_path / "jobs.db")
    # Written directly: there is no public way to produce a job whose worker
    # died, which is exactly the state a killed process leaves behind.
    stuck = Job(id="job_orphan", kind="analyze", workspace="demo", status=JobStatus.RUNNING)
    registry._insert(stuck)

    assert JobRegistry(tmp_path / "jobs.db").reconcile() == 1

    settled = registry.get(stuck.id)
    assert settled.status is JobStatus.INTERRUPTED
    assert "restarted" in settled.error
    assert settled.finished_at is not None


def test_a_failing_job_reports_the_message_without_the_traceback(tmp_path) -> None:
    registry = JobRegistry(tmp_path / "jobs.db")

    def work(report: ProgressReporter) -> dict:
        raise RuntimeError("connection refused")

    job = _finish(registry, registry.submit("analyze", "demo", work))
    assert job.status is JobStatus.FAILED
    assert job.error == "RuntimeError: connection refused"
    assert "Traceback" not in job.error


def test_a_finished_table_is_readable_before_the_run_ends(client, monkeypatch) -> None:
    """A five-table run used to write nothing until the fifth finished, so the
    console showed "3 of 5" in the header and nothing in the table list — and a
    restart discarded every completed table."""
    from atlas import api
    from atlas.agent import AnalysisSink

    workspace = seed("demo")
    observed: list[list[str]] = []

    def fake_analyze(adapter, snapshot, **kwargs):
        for name in ("alpha", "beta"):
            kwargs["on_table_start"](name)
            sink = AnalysisSink(facts=[_fact(name)])
            kwargs["on_table_done"](name, sink)
            # What a reader would see right now, mid-run.
            observed.append(sorted({f.subject for f in workspace.read_facts().facts}))
        return FactStore(), QuestionLog(), EvidenceStore()

    monkeypatch.setattr(api, "create_adapter", lambda url, **_: _NullAdapter())
    monkeypatch.setattr("atlas.agent.analyze_schema", fake_analyze)
    monkeypatch.setenv("ATLAS_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    get_settings.cache_clear()

    # regenerate so the seeded table is selected; the fake then stands in for
    # a two-table run and reports each one as it lands.
    response = client.post("/workspaces/demo/analyze", json={"regenerate": True})
    assert response.status_code == 202, response.json()
    settled = _finish(get_registry(), Job.model_validate(response.json()))
    assert settled.status is JobStatus.SUCCEEDED, settled.error

    assert observed[0] == ["alpha"]  # visible while beta was still running
    assert observed[1] == ["alpha", "beta"]


def _fact(subject: str) -> Fact:
    return Fact(
        subject=subject,
        aspect="grain",
        claim=f"One row per {subject}.",
        confidence=0.6,
        provenance=[GUESS],
    )


class _NullAdapter:
    def close(self) -> None: ...


def test_a_relationship_claim_does_not_count_as_having_analysed_a_table() -> None:
    """Discovery writes a join claim for nearly every table in the schema.
    Counting those as analysed made a full run select a single table."""
    from atlas.agent import select_tables

    snapshot = Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(schema_name="public", name=name, columns=[], primary_key=["id"])
            for name in ("orders", "customers")
        ],
    )
    facts = FactStore(
        facts=[
            Fact(
                subject="orders",
                aspect="join",
                discriminator="customers.customer_id",
                claim="orders.customer_id references customers.id.",
                confidence=0.6,
                provenance=[GUESS],
            )
        ]
    )
    analyzed = {
        f.subject.split(".")[0] for f in facts.facts if f.aspect not in ("join", "class")
    }

    assert analyzed == set()
    assert [t.name for t in select_tables(snapshot, already_analyzed=analyzed)] == [
        "orders",
        "customers",
    ]


def test_a_job_row_from_an_older_process_still_loads(tmp_path) -> None:
    """Persisting status makes every model change a migration. `current` became
    a list when tables started being read concurrently; rejecting the old shape
    took down the list endpoint, which is the one the console uses to find a
    run it did not start."""
    import json
    import sqlite3

    from atlas.jobs import SCHEMA

    path = tmp_path / "jobs.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        for job_id, current in (("job_old", '"invites"'), ("job_older", "null")):
            connection.execute(
                "INSERT INTO jobs (id, kind, workspace, status, created_at, progress) "
                "VALUES (?, 'analyze', 'demo', 'succeeded', '2026-08-09T00:00:00Z', ?)",
                (job_id, json.dumps(json.loads(f'{{"message":"m","current":{current}}}'))),
            )

    jobs = {j.id: j for j in JobRegistry(path).list("demo")}

    assert jobs["job_old"].progress.current == ["invites"]
    assert jobs["job_older"].progress.current == []
