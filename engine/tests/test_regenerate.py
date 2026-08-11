from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
from atlas.facts import Fact, FactStore, Provenance, ProvenanceKind
from atlas.questions import Question, QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Column, Snapshot, Table
from atlas.workspace import Workspace, WorkspaceManifest

NOW = datetime(2026, 8, 7, tzinfo=UTC)
CHECK = Provenance(kind=ProvenanceKind.GROUNDED_CHECK, detail="executed", result="pass")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def evidence_for(relation: str, marker: int) -> EvidenceRecord:
    return EvidenceRecord(
        type=EvidenceType.DETERMINISTIC_CHECK,
        authority=Authority.MEASURED,
        subjects=[f"relation:public.{relation}"],
        assertion=Assertion(description=f"about {relation}"),
        observation={"total": marker},
        scope=Scope(complete_scan=True, rows_examined=marker),
        verdict=Verdict.PASSED,
        freshness=Freshness(valid_as_of=NOW),
    )


def seed() -> Workspace:
    workspace = Workspace("demo")
    workspace.write_manifest(
        WorkspaceManifest(id="demo", source_id="source", snapshot_generation=1)
    )
    Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        source_id="source",
        generation=1,
        tables=[
            Table(schema_name="public", name="orders", columns=[Column(name="id", data_type="int", nullable=False)]),
            Table(schema_name="public", name="customers", columns=[Column(name="id", data_type="int", nullable=False)]),
        ],
    ).write(workspace.snapshot_path)
    orders = evidence_for("orders", 1)
    customers = evidence_for("customers", 2)

    FactStore(
        facts=[
            Fact(subject="orders", aspect="grain", claim="One row per order.",
                 confidence=0.88, provenance=[CHECK]),
            Fact(subject="customers", aspect="grain", claim="One row per customer.",
                 confidence=0.88, provenance=[CHECK]),
        ]
    ).write(workspace.facts_path)

    store = EvidenceStore()
    store.add(orders)
    store.add(customers)
    store.link(ClaimEvidence(claim_id="orders#grain", evidence_id=orders.id,
                             relationship=LinkKind.SUPPORTS, rationale="x"))
    store.link(ClaimEvidence(claim_id="customers#grain", evidence_id=customers.id,
                             relationship=LinkKind.SUPPORTS, rationale="x"))
    store.write(workspace.evidence_path)

    QuestionLog(
        questions=[
            Question(subject="orders.status", question="q", evidence="e", table="orders"),
            Question(subject="customers.tier", question="q", evidence="e", table="customers"),
        ]
    ).write(workspace.questions_path)
    return workspace


def test_regenerating_one_table_leaves_the_others_alone() -> None:
    """Scoped so regenerating one table cannot discard review done on another."""
    workspace = seed()
    removed = workspace.drop({"orders"})

    assert removed == {"claims": 1, "evidence": 1, "questions": 1}
    assert [f.subject for f in workspace.read_facts().facts] == ["customers"]
    assert [q.table for q in workspace.read_questions()] == ["customers"]


def test_evidence_is_replaced_not_accumulated() -> None:
    """Records carry valid_as_of; keeping several runs' worth leaves nothing
    marking which observation is current."""
    workspace = seed()
    workspace.drop({"orders"})
    subjects = [s for r in workspace.read_evidence().records for s in r.subjects]
    assert subjects == ["relation:public.customers"]


def test_links_to_dropped_evidence_do_not_dangle() -> None:
    workspace = seed()
    workspace.drop({"orders"})
    store = workspace.read_evidence()
    assert all(store.by_id(link.evidence_id) is not None for link in store.links)
    assert store.for_claim("orders#grain") == []


def test_dropping_nothing_changes_nothing() -> None:
    workspace = seed()
    assert workspace.drop(set()) == {"claims": 0, "evidence": 0, "questions": 0}
    assert len(workspace.read_facts().facts) == 2


def test_column_scoped_claims_go_with_their_table() -> None:
    workspace = seed()
    facts = workspace.read_facts()
    facts.facts.append(
        Fact(subject="orders.status", aspect="semantics", claim="State.",
             confidence=0.88, provenance=[CHECK])
    )
    facts.write(workspace.facts_path)

    workspace.drop({"orders"})
    assert [f.subject for f in workspace.read_facts().facts] == ["customers"]


def test_evidence_survives_a_workspace_round_trip() -> None:
    workspace = seed()
    assert len(workspace.read_evidence().records) == 2
    assert len(workspace.read_evidence().links) == 2
