"""One set of expectations, run against every metadata store.

A port is only worth having if its implementations agree, and agreement is not
something a docstring establishes. Everything here is written once and
parametrised over the adapters, so a second store either satisfies the contract
the first one taught the callers to expect or fails saying which clause it
broke.

The PostgreSQL cases need a live database at `ATLAS_TEST_DATABASE_URL` and are
skipped otherwise, so the everyday loop stays free of a Docker dependency. That
skip is also the risk: a green run locally proves the YAML store conforms and
says nothing about the other one. `make engine-test-postgres` is what closes it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from atlas.evidence import (
    Assertion,
    Authority,
    ClaimEvidence,
    EvidenceRecord,
    EvidenceStore,
    EvidenceType,
    Freshness,
    LinkKind,
    Scope,
    Verdict,
)
from atlas.facts import Fact, FactStatus, FactStore, Provenance, ProvenanceKind
from atlas.manifest import WorkspaceManifest
from atlas.metadata.base import (
    NoSnapshot,
    UnknownWorkspace,
    WorkspaceBusy,
    WorkspaceExists,
)
from atlas.metadata.postgres_store import PostgresMetadataRepository
from atlas.metadata.yaml_store import YamlMetadataRepository
from atlas.questions import Question, QuestionLog
from atlas.snapshot import Column, Snapshot, Table

DATABASE_URL = os.environ.get("ATLAS_TEST_DATABASE_URL", "")
NOW = datetime(2026, 8, 13, tzinfo=UTC)
GUESS = Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from the name")
CHECK = Provenance(kind=ProvenanceKind.GROUNDED_CHECK, detail="executed", result="pass")


# --- the stores under test -------------------------------------------------


def _yaml(tmp_path) -> YamlMetadataRepository:
    return YamlMetadataRepository(tmp_path)


def _postgres(_tmp_path) -> PostgresMetadataRepository:
    """A schema migrated from scratch, so the tests run against what
    `alembic upgrade head` actually produces rather than a hand-built copy of
    it that can drift."""
    # Absolute, because tests chdir into their own tmp directory and a
    # relative script_location would resolve against that instead.
    engine_root = Path(__file__).resolve().parent.parent
    config = Config(engine_root / "alembic.ini")
    config.set_main_option("script_location", str(engine_root / "migrations"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    repository = PostgresMetadataRepository(DATABASE_URL)
    with repository._engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    command.upgrade(config, "head")
    return repository


STORES = [
    pytest.param(_yaml, id="yaml"),
    pytest.param(
        _postgres,
        id="postgres",
        marks=[
            pytest.mark.postgres,
            pytest.mark.skipif(
                not DATABASE_URL, reason="ATLAS_TEST_DATABASE_URL is not set"
            ),
        ],
    ),
]


@pytest.fixture(params=STORES)
def store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    repository = request.param(tmp_path)
    yield repository
    if isinstance(repository, PostgresMetadataRepository):
        repository.dispose()


# --- fixtures --------------------------------------------------------------


def snapshot(*names: str) -> Snapshot:
    return Snapshot(
        database="shop",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(
                schema_name="public",
                name=name,
                columns=[Column(name="id", data_type="INTEGER", nullable=False)],
                exact_rows=1,
            )
            for name in names
        ],
    )


def fact(subject: str, *, claim: str = "One row per thing.", **overrides) -> Fact:
    return Fact(
        subject=subject,
        aspect=overrides.pop("aspect", "grain"),
        claim=claim,
        confidence=overrides.pop("confidence", 0.5),
        provenance=overrides.pop("provenance", [GUESS]),
        **overrides,
    )


def question(subject: str, table: str, text: str = "What does this mean?") -> Question:
    return Question(subject=subject, question=text, evidence="e", table=table)


def record(marker: int, *subjects: str) -> EvidenceRecord:
    return EvidenceRecord(
        type=EvidenceType.DETERMINISTIC_CHECK,
        authority=Authority.MEASURED,
        subjects=list(subjects) or ["relation:public.orders"],
        assertion=Assertion(description=f"observation {marker}"),
        observation={"total": marker},
        scope=Scope(complete_scan=True, rows_examined=marker),
        verdict=Verdict.PASSED,
        freshness=Freshness(valid_as_of=NOW),
    )


def extracted(store, name: str = "demo", *tables: str) -> None:
    """A registered workspace at generation 1. Almost every clause below is
    about a workspace in this state, because it is the state a workspace is in
    for all of its working life."""
    store.create(name, WorkspaceManifest(id=name, source_id="shop"))
    store.publish_snapshot(name, snapshot(*(tables or ("orders",))))


# --- registration ----------------------------------------------------------


def test_an_unknown_workspace_is_absent_rather_than_empty(store) -> None:
    assert store.exists("demo") is False
    assert store.has_snapshot("demo") is False
    assert store.list_workspaces() == []
    with pytest.raises(UnknownWorkspace):
        store.read_manifest("demo")


def test_creating_twice_is_refused(store) -> None:
    manifest = WorkspaceManifest(id="demo", source_id="shop")
    store.create("demo", manifest)
    with pytest.raises(WorkspaceExists):
        store.create("demo", manifest)


def test_a_created_workspace_is_registered_but_holds_nothing(store) -> None:
    store.create("demo", WorkspaceManifest(id="demo", source_id="shop"))

    assert store.exists("demo") is True
    assert store.has_snapshot("demo") is False
    assert store.has_semantics("demo") is False
    assert store.list_workspaces() == ["demo"]
    assert store.read_manifest("demo").snapshot_generation == 0
    with pytest.raises(NoSnapshot):
        store.read_snapshot("demo")


def test_the_manifest_survives_a_round_trip(store) -> None:
    manifest = WorkspaceManifest(id="demo", source_id="shop")
    store.create("demo", manifest)
    loaded = store.read_manifest("demo")

    assert (loaded.id, loaded.source_id) == ("demo", "shop")
    assert loaded.incarnation_id == manifest.incarnation_id
    assert loaded.created_at == manifest.created_at


def test_deleting_takes_the_semantics_with_it(store) -> None:
    extracted(store)
    store.upsert_facts("demo", [fact("orders")])

    store.delete("demo")

    assert store.exists("demo") is False
    assert store.list_workspaces() == []


def test_deleting_something_absent_is_silent(store) -> None:
    store.delete("never-existed")


# --- snapshots -------------------------------------------------------------


def test_publishing_advances_the_generation_and_stamps_the_snapshot(store) -> None:
    store.create("demo", WorkspaceManifest(id="demo", source_id="shop"))

    manifest = store.publish_snapshot("demo", snapshot("orders"))

    assert manifest.snapshot_generation == 1
    stored = store.read_snapshot("demo")
    assert (stored.generation, stored.source_id) == (1, "shop")
    assert [t.name for t in stored.tables] == ["orders"]


def test_a_second_publication_serves_the_newer_snapshot(store) -> None:
    extracted(store, "demo", "orders")

    store.publish_snapshot("demo", snapshot("invoices"))

    assert store.read_manifest("demo").snapshot_generation == 2
    assert [t.name for t in store.read_snapshot("demo").tables] == ["invoices"]


def test_semantics_do_not_follow_a_workspace_into_the_next_generation(store) -> None:
    """Claims are about columns a re-extraction may have removed. Carrying them
    across would attach a reviewed claim to a schema nobody checked it against.
    """
    extracted(store)
    store.upsert_facts("demo", [fact("orders")])
    assert store.has_semantics("demo") is True

    store.publish_snapshot("demo", snapshot("orders"))

    assert store.has_semantics("demo") is False
    assert store.read_facts("demo").facts == []


# --- reading an empty workspace --------------------------------------------


def test_an_unanalysed_workspace_reads_as_empty_not_as_an_error(store) -> None:
    extracted(store)

    assert store.read_facts("demo").facts == []
    assert store.read_questions("demo").questions == []
    assert store.read_evidence("demo").records == []
    assert store.has_semantics("demo") is False


# --- scoped writes ---------------------------------------------------------


def test_upserting_one_claim_leaves_its_siblings_alone(store) -> None:
    extracted(store)
    store.upsert_facts("demo", [fact("orders"), fact("invoices")])

    store.upsert_facts(
        "demo",
        [fact("orders", claim="One row per placed order.", provenance=[GUESS, CHECK],
              status=FactStatus.VERIFIED, verified_by="shivam")],
    )

    stored = store.read_facts("demo")
    assert stored.by_id("orders#grain").verified_by == "shivam"
    assert stored.by_id("invoices#grain").status is FactStatus.UNVERIFIED


def test_removing_claims_reports_how_many_existed(store) -> None:
    extracted(store)
    store.upsert_facts("demo", [fact("orders"), fact("invoices")])

    assert store.remove_facts("demo", {"orders#grain", "absent#grain"}) == 1
    assert [f.subject for f in store.read_facts("demo").facts] == ["invoices"]


def test_removing_nothing_touches_nothing(store) -> None:
    extracted(store)
    store.upsert_facts("demo", [fact("orders")])

    assert store.remove_facts("demo", set()) == 0
    assert store.remove_questions("demo", set()) == 0
    assert len(store.read_facts("demo").facts) == 1


def test_a_claim_keeps_every_field_it_was_stored_with(store) -> None:
    extracted(store)
    original = fact(
        "orders.status",
        aspect="semantics",
        claim="Lifecycle state.",
        confidence=0.9,
        provenance=[GUESS, CHECK],
        status=FactStatus.AUTO_ACCEPTED,
        consequence="routine",
    )

    store.upsert_facts("demo", [original])

    assert store.read_facts("demo").by_id("orders.status#semantics") == original


def test_the_derived_endorsement_is_never_stored(store) -> None:
    """A derived value on disk outlives the code that produced it. Renaming one
    of its states once left every stored workspace unreadable."""
    from atlas.decisions import record_decision

    extracted(store)
    reviewed, evidence = record_decision(
        fact("orders"), EvidenceStore(), reviewer="shivam", decision=FactStatus.VERIFIED
    )
    assert reviewed.endorsement is not None, "still derived in memory"

    store.upsert_facts("demo", [reviewed])
    store.append_evidence("demo", evidence)

    # Re-derived on read, never read back off the record.
    assert store.read_facts("demo").by_id("orders#grain").endorsement is None


def test_questions_are_addressed_one_at_a_time(store) -> None:
    extracted(store)
    first = question("orders.status", "orders")
    second = question("invoices.total", "invoices")
    store.upsert_questions("demo", [first, second])

    store.upsert_questions("demo", [first.answered("It is the state.", "shivam")])

    stored = {q.id: q for q in store.read_questions("demo").questions}
    assert stored[first.id].answer == "It is the state."
    assert stored[second.id].answer is None


def test_removing_questions_reports_how_many_existed(store) -> None:
    extracted(store)
    asked = question("orders.status", "orders")
    store.upsert_questions("demo", [asked])

    assert store.remove_questions("demo", {asked.id, "no-such-question"}) == 1
    assert store.read_questions("demo").questions == []


# --- evidence --------------------------------------------------------------


def test_appending_the_same_evidence_twice_stores_it_once(store) -> None:
    """A record is content-addressed and a link is identified by the claim, the
    record and the relationship — so a caller may hand over the whole store it
    was working from rather than a delta it had to compute."""
    extracted(store)
    observation = record(1)
    evidence = EvidenceStore()
    evidence.add(observation)
    evidence.link(
        ClaimEvidence(
            claim_id="orders#grain",
            evidence_id=observation.id,
            relationship=LinkKind.SUPPORTS,
            rationale="the assertion held",
        )
    )

    store.append_evidence("demo", evidence)
    store.append_evidence("demo", store.read_evidence("demo"))

    stored = store.read_evidence("demo")
    assert len(stored.records) == 1
    assert len(stored.links) == 1


def test_evidence_survives_a_round_trip_with_its_links(store) -> None:
    extracted(store)
    first, second = record(1), record(2, "relation:public.invoices")
    evidence = EvidenceStore(records=[first, second])
    evidence.link(
        ClaimEvidence(
            claim_id="orders#grain",
            evidence_id=first.id,
            relationship=LinkKind.CONTRADICTS,
            rationale="24 of 26 rows have no match",
        )
    )
    store.append_evidence("demo", evidence)

    stored = store.read_evidence("demo")
    assert {r.id for r in stored.records} == {first.id, second.id}
    assert stored.by_id(first.id).observation == {"total": 1}
    pairs = stored.for_claim("orders#grain")
    assert len(pairs) == 1
    assert pairs[0][0].relationship is LinkKind.CONTRADICTS
    assert pairs[0][1].id == first.id


# --- wholesale writes ------------------------------------------------------


def test_writing_a_collection_replaces_what_was_there(store) -> None:
    extracted(store)
    store.upsert_facts("demo", [fact("orders"), fact("invoices")])

    store.write_facts("demo", FactStore(facts=[fact("customers")]))

    assert [f.subject for f in store.read_facts("demo").facts] == ["customers"]


def test_writing_evidence_replaces_records_and_links_together(store) -> None:
    extracted(store)
    first = record(1)
    seeded = EvidenceStore(records=[first])
    seeded.link(
        ClaimEvidence(
            claim_id="orders#grain",
            evidence_id=first.id,
            relationship=LinkKind.SUPPORTS,
            rationale="held",
        )
    )
    store.append_evidence("demo", seeded)

    store.write_evidence("demo", EvidenceStore(records=[record(9)]))

    stored = store.read_evidence("demo")
    assert [r.observation["total"] for r in stored.records] == [9]
    assert stored.links == []


def test_writing_questions_replaces_the_log(store) -> None:
    extracted(store)
    store.upsert_questions("demo", [question("orders.status", "orders")])

    store.write_questions(
        "demo", QuestionLog(questions=[question("invoices.total", "invoices")])
    )

    assert [q.table for q in store.read_questions("demo").questions] == ["invoices"]


def test_clearing_semantics_reports_what_went(store) -> None:
    extracted(store)
    store.upsert_facts("demo", [fact("orders")])
    store.upsert_questions("demo", [question("orders.status", "orders")])
    store.append_evidence("demo", EvidenceStore(records=[record(1)]))

    removed = store.clear_semantics("demo")

    assert set(removed) == {"facts", "questions", "evidence"}
    assert store.has_semantics("demo") is False
    assert store.read_facts("demo").facts == []
    # The snapshot is not semantic state and stays put.
    assert store.has_snapshot("demo") is True


def test_clearing_an_empty_workspace_reports_nothing(store) -> None:
    extracted(store)
    assert store.clear_semantics("demo") == {}


# --- concurrency -----------------------------------------------------------


def test_a_transaction_is_reentrant_within_a_thread(store) -> None:
    """The request path takes the lock in the write guard and again in the
    scoped write underneath. A lock that is not re-entrant waits for itself,
    and the symptom is a request that never returns."""
    extracted(store)
    # Deliberately nested rather than combined: the second entry happening
    # while the first is open is the whole subject of the test.
    with store.transaction("demo"):  # noqa: SIM117
        with store.transaction("demo"):
            store.upsert_facts("demo", [fact("orders")])

    assert len(store.read_facts("demo").facts) == 1


def test_a_second_writer_is_refused_rather_than_queued(store) -> None:
    """`blocking=False` is what lets the API answer 409 instead of holding a
    request open behind a ten-minute analysis."""
    import threading

    extracted(store)
    entered = threading.Event()
    release = threading.Event()
    refused: list[bool] = []

    def hold() -> None:
        with store.transaction("demo"):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(timeout=5)
    try:
        with store.transaction("demo", blocking=False):
            refused.append(False)
    except WorkspaceBusy:
        refused.append(True)
    release.set()
    holder.join(timeout=5)

    assert refused == [True]


# --- moving from one store to the other ------------------------------------


@pytest.mark.postgres
@pytest.mark.skipif(not DATABASE_URL, reason="ATLAS_TEST_DATABASE_URL is not set")
def test_migrating_the_store_carries_a_workspace_across_whole(tmp_path, monkeypatch) -> None:
    """Setting the URL changes where Atlas looks, not where the data is. A
    workspace that does not come across reads as absent, which nobody can tell
    from deleted."""
    from typer.testing import CliRunner

    from atlas.cli import app as cli_app
    from atlas.settings import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ATLAS_DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    database = _postgres(tmp_path)
    try:
        files = YamlMetadataRepository(tmp_path)
        files.create("demo", WorkspaceManifest(id="demo", source_id="shop"))
        files.publish_snapshot("demo", snapshot("orders"))
        # A second generation, so the pointer is not the trivial 1.
        files.publish_snapshot("demo", snapshot("orders", "invoices"))
        files.upsert_facts("demo", [fact("orders"), fact("invoices")])
        files.upsert_questions("demo", [question("orders.status", "orders")])
        observation = record(3)
        evidence = EvidenceStore(records=[observation])
        evidence.link(
            ClaimEvidence(
                claim_id="orders#grain",
                evidence_id=observation.id,
                relationship=LinkKind.SUPPORTS,
                rationale="held",
            )
        )
        files.append_evidence("demo", evidence)
        original = files.read_manifest("demo")

        dry = CliRunner().invoke(cli_app, ["migrate-store", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "WOULD COPY" in dry.output
        assert database.exists("demo") is False, "a dry run must write nothing"

        result = CliRunner().invoke(cli_app, ["migrate-store"])
        assert result.exit_code == 0, result.output

        carried = database.read_manifest("demo")
        assert carried.snapshot_generation == original.snapshot_generation == 2
        assert carried.incarnation_id == original.incarnation_id
        assert [t.name for t in database.read_snapshot("demo").tables] == [
            "orders",
            "invoices",
        ]
        assert {f.subject for f in database.read_facts("demo").facts} == {
            "orders",
            "invoices",
        }
        assert len(database.read_questions("demo").questions) == 1
        assert len(database.read_evidence("demo").links) == 1

        # Idempotent, and the files are still there to go back to.
        again = CliRunner().invoke(cli_app, ["migrate-store"])
        assert "SKIPPED" in again.output
        assert files.read_manifest("demo").snapshot_generation == 2
    finally:
        database.dispose()
        get_settings.cache_clear()


# --- the whole stack on the database store ---------------------------------


@pytest.mark.postgres
@pytest.mark.skipif(not DATABASE_URL, reason="ATLAS_TEST_DATABASE_URL is not set")
def test_the_api_serves_a_workspace_that_lives_in_postgres(tmp_path, monkeypatch) -> None:
    """Conformance proves the adapter satisfies the port. This proves the
    engine reaches it — that `ATLAS_DATABASE_URL` is all it takes, and that
    nothing between the route and the row still assumes a directory."""
    from fastapi.testclient import TestClient

    from atlas.api import app
    from atlas.jobs import get_registry
    from atlas.metadata.registry import reset_repositories
    from atlas.settings import get_settings
    from atlas.sources import Source, SourceRegistry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ATLAS_DATABASE_URL", DATABASE_URL)
    get_settings.cache_clear()
    get_registry.cache_clear()
    reset_repositories()
    try:
        _postgres(tmp_path).dispose()
        SourceRegistry(
            sources=[Source(id="shop", adapter="postgresql", url_env="SHOP_URL")]
        ).write()
        client = TestClient(app)

        assert client.post(
            "/workspaces", json={"id": "demo", "source_id": "shop"}
        ).status_code == 201

        # Extraction needs a live source, so the snapshot is published through
        # the store the API is about to read from.
        from atlas.catalog import Catalog

        workspace = Catalog("demo")
        workspace.publish(snapshot("orders"))
        workspace.write_facts(FactStore(facts=[fact("orders")]))

        listed = client.get("/workspaces").json()["workspaces"]
        assert [w["id"] for w in listed] == ["demo"]
        assert listed[0]["snapshot_generation"] == 1

        output = client.get("/workspaces/demo/output").json()
        assert output["tables"][0]["grain"]["text"] == "One row per thing."

        reviewed = client.post(
            "/workspaces/demo/claims/orders%23grain/review",
            json={"decision": "verified", "reviewer": "shivam"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert workspace.facts().by_id("orders#grain").verified_by == "shivam"

        assert client.delete("/workspaces/demo").status_code == 204
        assert client.get("/workspaces").json()["workspaces"] == []
    finally:
        reset_repositories()
        get_settings.cache_clear()
        get_registry.cache_clear()
