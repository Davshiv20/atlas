from __future__ import annotations

import threading
from typing import Self

import pytest
from fastapi.testclient import TestClient

from atlas import api
from atlas.api import app
from atlas.catalog import Catalog, WorkspaceConflict
from atlas.evidence import EvidenceStore
from atlas.facts import Fact, FactStore, Provenance, ProvenanceKind
from atlas.jobs import TERMINAL, Job, get_registry
from atlas.metadata import yaml_store
from atlas.metadata.yaml_store import YamlMetadataRepository
from atlas.settings import get_settings
from atlas.snapshot import Column, Snapshot, Table
from atlas.sources import Source, SourceRegistry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    for leaked in ("PG_URL", "SF_URL", "OPENROUTER_API_KEY", "ATLAS_DATABASE_URL"):
        monkeypatch.delenv(leaked, raising=False)
    get_settings.cache_clear()
    get_registry.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_registry.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def declare_sources() -> None:
    SourceRegistry(
        sources=[
            Source(id="pg", adapter="postgresql", url_env="PG_URL", namespace="public"),
            Source(id="sf", adapter="snowflake", url_env="SF_URL", namespace="DB.SCHEMA"),
        ]
    ).write()


def table(name: str, schema: str) -> Table:
    return Table(
        schema_name=schema,
        name=name,
        columns=[Column(name="id", data_type="INTEGER", nullable=False, is_primary_key=True)],
        exact_rows=1,
    )


class FakeAdapter:
    def __init__(self, url: str, *, gate: threading.Event | None = None) -> None:
        self.url = url
        self.gate = gate

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None: ...

    def test_connection(self) -> None: ...

    def extract_structure(self, namespace: str) -> Snapshot:
        if self.gate:
            self.gate.wait(timeout=5)
        if "snowflake" in self.url:
            return Snapshot(
                database="snow",
                schema_name=namespace,
                dialect="snowflake",
                tables=[table("accounts", namespace)],
            )
        return Snapshot(
            database="pgdb",
            schema_name=namespace,
            dialect="postgresql",
            tables=[table("orders", namespace)],
        )

    def profile(self, snapshot: Snapshot, on_table=None) -> Snapshot:
        return snapshot


def seed_legacy(name: str, snapshot: Snapshot) -> Catalog:
    """Write a workspace the way Atlas wrote them before manifests existed.

    Reaches into the YAML store on purpose: this is the only shape of data the
    adoption path exists for, and no port method can produce it.
    """
    root = get_settings().atlas_output_dir / name
    yaml_store._write_snapshot(root / yaml_store.SNAPSHOT, snapshot)
    return Catalog(name)


def seed_reviewed(name: str, source_id: str, table_name: str, claim: str) -> Catalog:
    """A workspace at generation 1 with one claim on it — the state a refresh
    is not allowed to discard silently."""
    workspace = Catalog(name)
    workspace.register(source_id)
    workspace.publish(
        Snapshot(
            database="old",
            schema_name="public",
            dialect="postgresql",
            tables=[table(table_name, "public")],
        )
    )
    workspace.write_facts(
        FactStore(
            facts=[
                Fact(
                    subject=table_name,
                    aspect="grain",
                    claim=claim,
                    confidence=0.5,
                    provenance=[Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="test")],
                )
            ]
        )
    )
    return workspace


def finish(job: dict) -> Job:
    parsed = Job.model_validate(job)
    while (current := get_registry().get(parsed.id)).status not in TERMINAL:
        pass
    assert current.status == "succeeded", current.error
    return current


def test_postgres_and_snowflake_workspaces_keep_independent_snapshots(
    client, monkeypatch
) -> None:
    declare_sources()
    monkeypatch.setenv("PG_URL", "postgresql://pg")
    monkeypatch.setenv("SF_URL", "snowflake://sf")
    get_settings.cache_clear()
    monkeypatch.setattr(api, "create_adapter", lambda url, **_: FakeAdapter(url))

    assert client.post("/workspaces", json={"id": "pg-review", "source_id": "pg"}).status_code == 201
    assert client.post("/workspaces", json={"id": "sf-review", "source_id": "sf"}).status_code == 201

    finish(client.post("/workspaces/pg-review/extract", json={}).json())
    finish(client.post("/workspaces/sf-review/extract", json={}).json())

    pg = Catalog("pg-review").snapshot()
    sf = Catalog("sf-review").snapshot()
    assert (pg.source_id, pg.generation, [t.name for t in pg.tables]) == ("pg", 1, ["orders"])
    assert (sf.source_id, sf.generation, [t.name for t in sf.tables]) == ("sf", 1, ["accounts"])


def test_workspace_source_binding_cannot_change(client) -> None:
    declare_sources()
    assert client.post(
        "/workspaces", json={"id": "review", "source_id": "pg"}
    ).status_code == 201

    response = client.post("/workspaces", json={"id": "review", "source_id": "sf"})

    assert response.status_code == 409
    assert Catalog("review").manifest().source_id == "pg"


def test_one_source_cannot_be_bound_to_two_workspaces(client) -> None:
    declare_sources()
    assert client.post(
        "/workspaces", json={"id": "first", "source_id": "pg"}
    ).status_code == 201

    response = client.post("/workspaces", json={"id": "second", "source_id": "pg"})

    assert response.status_code == 409
    assert response.json()["detail"]["workspaces"] == ["first"]


def test_stale_extract_cannot_publish_after_delete_and_recreate(client, monkeypatch) -> None:
    declare_sources()
    monkeypatch.setenv("PG_URL", "postgresql://pg")
    monkeypatch.setenv("SF_URL", "snowflake://sf")
    get_settings.cache_clear()
    assert client.post(
        "/workspaces", json={"id": "review", "source_id": "pg"}
    ).status_code == 201
    old_incarnation = Catalog("review").manifest().incarnation_id
    entered = threading.Event()
    release = threading.Event()
    original_submit = api._submit_mutation

    def gated_submit(kind, workspace, manifest, work):
        entered.set()
        release.wait(timeout=5)
        return original_submit(kind, workspace, manifest, work)

    monkeypatch.setattr(api, "_submit_mutation", gated_submit)
    response: dict[str, object] = {}

    def extract() -> None:
        response["extract"] = client.post("/workspaces/review/extract", json={})

    extracting = threading.Thread(target=extract)
    extracting.start()
    assert entered.wait(timeout=5)
    assert client.delete("/workspaces/review").status_code == 204
    assert client.post(
        "/workspaces", json={"id": "review", "source_id": "sf"}
    ).status_code == 201
    new_manifest = Catalog("review").manifest()
    assert new_manifest.incarnation_id != old_incarnation
    release.set()
    extracting.join(timeout=5)

    assert response["extract"].status_code == 409
    assert not Catalog("review").has_snapshot()
    assert Catalog("review").manifest().source_id == "sf"


def test_managed_extract_rejects_source_rebinding_fields(client) -> None:
    declare_sources()
    assert client.post("/workspaces", json={"id": "pg-review", "source_id": "pg"}).status_code == 201
    response = client.post(
        "/workspaces/pg-review/extract",
        json={"source_id": "sf", "database_url": "snowflake://sf", "schema_name": "DB.SCHEMA"},
    )
    assert response.status_code == 422


def test_legacy_workspace_migrates_only_with_unambiguous_snapshot_source(client) -> None:
    declare_sources()
    legacy = seed_legacy(
        "legacy",
        Snapshot(
            database="pgdb",
            schema_name="public",
            dialect="postgresql",
            source_id="pg",
            tables=[table("orders", "public")],
        ),
    )

    body = client.get("/workspaces").json()["workspaces"]
    assert body[0]["id"] == "legacy"
    assert body[0]["source_id"] == "pg"
    assert legacy.manifest().snapshot_generation == 1
    assert legacy.snapshot().generation == 1


def test_ambiguous_legacy_workspace_is_refused(client) -> None:
    seed_legacy(
        "legacy",
        Snapshot(
            database="pgdb",
            schema_name="public",
            dialect="postgresql",
            tables=[table("orders", "public")],
        ),
    )

    response = client.get("/workspaces")
    assert response.status_code == 409
    assert "refusing ambiguous legacy migration" in response.json()["detail"]


def test_refresh_requires_semantic_reset_and_advances_generation(client, monkeypatch) -> None:
    declare_sources()
    monkeypatch.setenv("PG_URL", "postgresql://pg")
    get_settings.cache_clear()
    monkeypatch.setattr(api, "create_adapter", lambda url, **_: FakeAdapter(url))
    workspace = seed_reviewed("pg-review", "pg", "old_orders", "Old semantic state.")
    workspace.write_evidence(EvidenceStore())

    conflict = client.post("/workspaces/pg-review/extract", json={})
    assert conflict.status_code == 409
    assert "reset_semantics=true" in conflict.json()["detail"]

    finish(client.post("/workspaces/pg-review/extract", json={"reset_semantics": True}).json())
    assert not workspace.has_semantics()
    assert workspace.manifest().snapshot_generation == 2
    assert workspace.snapshot().generation == 2


def test_failed_refresh_preserves_the_previous_snapshot_and_semantics(client, monkeypatch) -> None:
    class FailingAdapter(FakeAdapter):
        def profile(self, snapshot: Snapshot, on_table=None) -> Snapshot:
            raise RuntimeError("profile failed")

    declare_sources()
    monkeypatch.setenv("PG_URL", "postgresql://pg")
    get_settings.cache_clear()
    monkeypatch.setattr(api, "create_adapter", lambda url, **_: FailingAdapter(url))
    workspace = seed_reviewed("pg-review", "pg", "old_orders", "Old semantic state.")

    submitted = Job.model_validate(
        client.post(
            "/workspaces/pg-review/extract", json={"reset_semantics": True}
        ).json()
    )
    while (current := get_registry().get(submitted.id)).status not in TERMINAL:
        pass

    assert current.status == "failed"
    assert workspace.snapshot().tables[0].name == "old_orders"
    assert workspace.facts().facts[0].claim == "Old semantic state."


def test_refresh_commits_the_generation_pointer_last(monkeypatch) -> None:
    workspace = seed_reviewed("pg-review", "pg", "old_orders", "Reviewed old state.")
    replacement = Snapshot(
        database="new",
        schema_name="public",
        dialect="postgresql",
        tables=[table("new_orders", "public")],
    )
    original_write = yaml_store._write_manifest

    def fail_new_manifest(path, manifest) -> None:
        if manifest.snapshot_generation == 2:
            raise OSError("manifest commit failed")
        original_write(path, manifest)

    monkeypatch.setattr(yaml_store, "_write_manifest", fail_new_manifest)
    with pytest.raises(OSError, match="manifest commit failed"):
        workspace.publish(replacement, reset_semantics=True)

    assert workspace.manifest().snapshot_generation == 1
    assert workspace.snapshot().tables[0].name == "old_orders"
    assert workspace.facts().facts[0].claim == "Reviewed old state."

    monkeypatch.setattr(yaml_store, "_write_manifest", original_write)
    workspace.publish(replacement, reset_semantics=True)
    assert workspace.manifest().snapshot_generation == 2
    assert workspace.snapshot().tables[0].name == "new_orders"
    assert not workspace.has_semantics()


def test_adoption_keeps_the_original_state_until_the_pointer_commits(
    monkeypatch, isolated
) -> None:
    workspace = seed_legacy(
        "legacy",
        Snapshot(
            database="old",
            schema_name="public",
            dialect="postgresql",
            source_id="pg",
            tables=[table("orders", "public")],
        ),
    )
    at_root = isolated / "legacy" / yaml_store.SNAPSHOT
    original_write = yaml_store._write_manifest

    def fail_manifest(path, manifest) -> None:
        raise OSError("manifest commit failed")

    monkeypatch.setattr(yaml_store, "_write_manifest", fail_manifest)
    with pytest.raises(OSError, match="manifest commit failed"):
        workspace.adopt("pg")

    assert not workspace.is_registered()
    assert at_root.exists()

    monkeypatch.setattr(yaml_store, "_write_manifest", original_write)
    workspace.adopt("pg")
    assert workspace.snapshot().source_id == "pg"
    assert not at_root.exists()


def test_source_delete_is_blocked_while_workspace_references_it(client) -> None:
    declare_sources()
    Catalog("pg-review").register("pg")

    response = client.delete("/sources/pg")
    assert response.status_code == 409
    assert response.json()["detail"]["workspaces"] == ["pg-review"]

    assert client.delete("/workspaces/pg-review").status_code == 204
    assert client.delete("/sources/pg").status_code == 204
    assert SourceRegistry.read().get("sf").id == "sf"


def test_source_delete_and_workspace_create_are_serialized(client, monkeypatch) -> None:
    declare_sources()
    entered = threading.Event()
    release = threading.Event()
    original = api.referencing_source

    def gated_references(source_id: str, repository=None):
        if source_id == "pg":
            entered.set()
            release.wait(timeout=5)
        return original(source_id, repository)

    monkeypatch.setattr(api, "referencing_source", gated_references)
    responses: dict[str, object] = {}

    def delete() -> None:
        responses["delete"] = client.delete("/sources/pg")

    def create() -> None:
        responses["create"] = client.post(
            "/workspaces", json={"id": "racing", "source_id": "pg"}
        )

    deleting = threading.Thread(target=delete)
    creating = threading.Thread(target=create)
    deleting.start()
    assert entered.wait(timeout=5)
    creating.start()
    release.set()
    deleting.join(timeout=5)
    creating.join(timeout=5)

    assert responses["delete"].status_code == 204
    assert responses["create"].status_code == 404
    assert not Catalog("racing").is_registered()


def test_cached_workspace_remains_readable_without_source_credentials(client) -> None:
    declare_sources()
    workspace = Catalog("pg-review")
    workspace.register("pg")
    workspace.publish(
        Snapshot(
            database="pgdb",
            schema_name="public",
            dialect="postgresql",
            tables=[table("orders", "public")],
        )
    )

    listing = client.get("/workspaces").json()["workspaces"][0]
    assert listing["snapshot_available"] is True
    assert listing["source_configured"] is False
    assert client.get("/workspaces/pg-review/output").status_code == 200
    assert client.post("/workspaces/pg-review/analyze", json={}).status_code == 400


def test_second_workspace_mutation_is_rejected_while_first_is_active(client, monkeypatch) -> None:
    declare_sources()
    monkeypatch.setenv("PG_URL", "postgresql://pg")
    get_settings.cache_clear()
    gate = threading.Event()
    monkeypatch.setattr(api, "create_adapter", lambda url, **_: FakeAdapter(url, gate=gate))
    assert client.post("/workspaces", json={"id": "pg-review", "source_id": "pg"}).status_code == 201

    first = client.post("/workspaces/pg-review/extract", json={})
    assert first.status_code == 202
    second = client.post("/workspaces/pg-review/extract", json={})
    assert second.status_code == 409
    assert second.json()["detail"]["job_id"] == first.json()["id"]
    review = client.post(
        "/workspaces/pg-review/claims/orders%23grain/review",
        json={"decision": "verified", "reviewer": "shivam"},
    )
    assert review.status_code == 409
    delete_response = client.delete("/workspaces/pg-review")
    assert delete_response.status_code == 409

    from typer.testing import CliRunner

    from atlas.cli import app as cli_app

    cli_response = CliRunner().invoke(cli_app, ["extract", "pg-review"])
    assert cli_response.exit_code != 0
    assert "active job" in cli_response.output

    gate.set()
    finish(first.json())


def test_publishing_out_of_generation_zero_will_not_abandon_semantic_state(tmp_path) -> None:
    """The guard must not be conditioned on the current generation.

    Publishing out of generation zero is the case where semantic state is not
    carried into the new generation but abandoned: the files stay at the
    workspace root while every accessor moves to `generations/1/`. Skipping the
    check there protected the smaller loss and waved through the total one.
    """
    workspace = Catalog("gen-zero", YamlMetadataRepository(tmp_path))
    workspace.register("pg")
    workspace.write_facts(
        FactStore(
            facts=[
                Fact(
                    subject="orders",
                    aspect="grain",
                    claim="One row per order.",
                    confidence=0.5,
                    provenance=[
                        Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from the name")
                    ],
                )
            ]
        )
    )
    assert workspace.has_semantics()

    snapshot = Snapshot(
        database="shop",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(
                schema_name="public",
                name="orders",
                columns=[Column(name="id", data_type="INT", nullable=False)],
            )
        ],
    )

    with pytest.raises(WorkspaceConflict, match="reset_semantics"):
        workspace.publish(snapshot)

    # Refused, so nothing moved and the claims are still reachable.
    assert workspace.manifest().snapshot_generation == 0
    assert workspace.facts().facts[0].claim == "One row per order."

    published = workspace.publish(snapshot, reset_semantics=True)
    assert published.snapshot_generation == 1
