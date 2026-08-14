"""What leaves the building, and what is held back from it.

The export is the one artifact that outlives the process that made it. A served
view is current by definition and a reader can always ask again; a file on
someone's disk is read months later by somebody who was not here, so the things
under test are mostly about what it says about itself — which capture it came
from, what is missing, and whether anyone stood behind the meaning in it.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from atlas.api import app
from atlas.catalog import Catalog
from atlas.facts import Fact, FactStatus, FactStore, Provenance, ProvenanceKind
from atlas.jobs import get_registry
from atlas.questions import Question, QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Column, Snapshot, Table
from atlas.sources import Source, get_source_repository

CHECK = Provenance(kind=ProvenanceKind.GROUNDED_CHECK, detail="executed: SELECT 1", result="pass")
GUESS = Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from the name")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for leaked in ("ATLAS_DATABASE_URL", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_registry.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_registry.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def table(name: str) -> Table:
    return Table(
        schema_name="public",
        name=name,
        columns=[
            Column(name="id", data_type="INTEGER", nullable=False, is_primary_key=True),
            Column(name="status", data_type="VARCHAR", nullable=False),
        ],
        exact_rows=10,
    )


def claim(subject: str, aspect: str, text: str, *, verified: bool) -> Fact:
    """A claim either settled or not. Grain drives `emittable`, so which one a
    table gets is what decides whether it may be exported."""
    return Fact(
        subject=subject,
        aspect=aspect,
        claim=text,
        confidence=0.9,
        provenance=[GUESS, CHECK],
        status=FactStatus.VERIFIED if verified else FactStatus.UNVERIFIED,
        verified_by="shivam" if verified else None,
    )


def seed(name: str = "demo") -> Catalog:
    """One table that passed review and one that did not."""
    get_source_repository().add(
        Source(id="shop", adapter="postgresql", url_env="SHOP_URL")
    )
    workspace = Catalog(name)
    workspace.register("shop")
    workspace.publish(
        Snapshot(
            database="shop",
            schema_name="public",
            dialect="postgresql",
            tables=[table("orders"), table("drafts")],
        )
    )
    workspace.write_facts(
        FactStore(
            facts=[
                claim("orders", "grain", "One row per order.", verified=True),
                claim("orders.status", "semantics", "Order lifecycle state.", verified=True),
                claim("drafts", "grain", "One row per draft.", verified=False),
            ]
        )
    )
    workspace.write_questions(
        QuestionLog(
            questions=[
                Question(
                    subject="orders.status",
                    question="Does 'void' mean cancelled, or never issued?",
                    evidence="two values dominate",
                    table="orders",
                ),
                Question(
                    subject="orders.id",
                    question="Is this stable across replays?",
                    evidence="looks sequential",
                    table="orders",
                ).answered("Yes, it is stable.", "shivam"),
            ]
        )
    )
    return workspace


def body(client: TestClient, query: str = "") -> str:
    response = client.get(f"/workspaces/demo/export{query}")
    assert response.status_code == 200, response.text
    return response.text


def exported(client: TestClient, query: str = "") -> list[str]:
    """The table names in an export, parsed rather than grepped.

    Counting `- name:` also counts the entries under `excluded:`, which is how
    a one-table export looked like three.
    """
    document = yaml.safe_load(body(client, query))
    return [t["name"] for t in (document.get("tables") or [])]


# --- what is in it ---------------------------------------------------------


def test_only_reviewed_tables_are_exported_by_default(client) -> None:
    seed()
    assert exported(client) == ["orders"]


def test_including_the_unvalidated_takes_saying_so(client) -> None:
    seed()
    assert set(exported(client, "?include=all")) == {"orders", "drafts"}


def test_the_header_names_what_was_left_out(client) -> None:
    """A count tells a reader they have an incomplete file. The names tell them
    whether it matters."""
    seed()
    header = body(client).split("tables:")[0]
    assert "1 of 2 tables passed review" in header
    assert "drafts" in header
    assert "snapshot generation 1" in header


def test_including_the_unvalidated_says_so_before_the_first_claim(client) -> None:
    """Not after. A reader who meets the caveat forty lines down has already
    formed a view of the meaning above it."""
    seed()
    text = body(client, "?include=all")
    header = text.split("tables:")[0]
    assert "nobody has reviewed" in header
    assert "drafts" in header
    assert text.index("nobody has reviewed") < text.index("- name:")


def test_a_fully_reviewed_workspace_says_that_plainly(client) -> None:
    seed()
    # Settle the outstanding grain, and the caveat should disappear rather than
    # become an empty list.
    workspace = Catalog("demo")
    facts = workspace.facts()
    workspace.write_facts(
        FactStore(
            facts=[
                f if f.subject != "drafts" else claim("drafts", "grain", f.claim, verified=True)
                for f in facts.facts
            ]
        )
    )
    header = body(client, "?include=all").split("tables:")[0]
    assert "All 2 tables passed review." in header
    assert "excluded" not in header


def test_open_questions_travel_with_their_table(client) -> None:
    """Invariant 7 in the artifact: unknown meaning stays explicit. An agent
    that knows a column is unresolved can hedge; one handed a view that omits
    the uncertainty cannot."""
    seed()
    orders = yaml.safe_load(body(client))["tables"][0]
    assert orders["open_questions"] == ["Does 'void' mean cancelled, or never issued?"]


def test_an_answered_question_is_not_an_open_one(client) -> None:
    seed()
    orders = yaml.safe_load(body(client))["tables"][0]
    assert not any("stable across replays" in q for q in orders["open_questions"])


def test_a_question_with_a_colon_in_it_still_parses(client) -> None:
    """The file is the product. A hand-written scalar containing a colon broke
    the whole document once, and questions are the value most likely to."""
    seed()
    workspace = Catalog("demo")
    workspace.write_questions(
        QuestionLog(
            questions=[
                Question(
                    subject="orders.status",
                    question="Which is it: cancelled, or never issued? See ticket #42.",
                    evidence="ambiguous",
                    table="orders",
                )
            ]
        )
    )
    document = yaml.safe_load(body(client))
    assert document["tables"][0]["open_questions"] == [
        "Which is it: cancelled, or never issued? See ticket #42."
    ]


# --- scoping ---------------------------------------------------------------


def test_one_table_can_be_exported_on_its_own(client) -> None:
    seed()
    assert exported(client, "?table=orders") == ["orders"]


def test_asking_for_an_unreviewed_table_honours_the_gate(client) -> None:
    """Naming a table is not consent to export unvalidated meaning. Saying
    `include=all` is."""
    seed()
    assert exported(client, "?table=drafts") == []
    assert exported(client, "?table=drafts&include=all") == ["drafts"]


def test_a_table_that_is_not_there_is_a_404(client) -> None:
    seed()
    assert client.get("/workspaces/demo/export?table=nope").status_code == 404


# --- the URL as a resource -------------------------------------------------


def test_the_same_view_tags_the_same(client) -> None:
    seed()
    first = client.get("/workspaces/demo/export")
    second = client.get("/workspaces/demo/export")
    assert first.headers["etag"] == second.headers["etag"]


def test_an_unchanged_view_costs_a_304(client) -> None:
    """What makes this a resource an agent can poll rather than a download in
    disguise."""
    seed()
    tag = client.get("/workspaces/demo/export").headers["etag"]
    unchanged = client.get("/workspaces/demo/export", headers={"If-None-Match": tag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""


def test_the_tag_does_not_move_on_its_own(client) -> None:
    """The rendered text carries the moment it was taken. Hashing that would
    mint a new tag every request and the 304 would never fire — so the tag is
    over the view, not over the bytes."""
    seed()
    first = client.get("/workspaces/demo/export")
    assert "Exported" in first.text
    assert client.get("/workspaces/demo/export").headers["etag"] == first.headers["etag"]


def test_reviewing_a_claim_moves_the_tag(client) -> None:
    """The promise the URL makes: if the meaning changed, the caller finds out."""
    seed()
    before = client.get("/workspaces/demo/export").headers["etag"]

    approved = client.post(
        "/workspaces/demo/claims/drafts%23grain/review",
        json={"decision": "verified", "reviewer": "shivam"},
    )
    assert approved.status_code == 200, approved.text

    after = client.get("/workspaces/demo/export")
    assert after.headers["etag"] != before
    # And the newly settled table is now in the default export.
    assert {t["name"] for t in yaml.safe_load(after.text)["tables"]} == {"orders", "drafts"}


def test_a_different_scope_is_a_different_resource(client) -> None:
    seed()
    ready = client.get("/workspaces/demo/export").headers["etag"]
    everything = client.get("/workspaces/demo/export?include=all").headers["etag"]
    assert ready != everything


# --- download --------------------------------------------------------------


def test_downloading_names_the_generation_it_came_from(client) -> None:
    """A file nobody can date is a file nobody can tell is stale."""
    seed()
    response = client.get("/workspaces/demo/export?download=1")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="demo-gen1-semantic-view.yaml"'
    )
    assert response.headers["content-type"].startswith("application/yaml")


def test_the_served_view_is_not_a_download(client) -> None:
    seed()
    assert "content-disposition" not in client.get("/workspaces/demo/export").headers


def test_json_carries_what_the_comments_carry(client) -> None:
    """JSON has no comments, so a consumer that cannot see what is missing will
    assume nothing is."""
    seed()
    document = json.loads(body(client, "?format=json"))
    assert document["tables_exported"] == 1
    assert document["tables_total"] == 2
    assert document["tables_excluded"] == ["drafts"]
    assert document["snapshot_generation"] == 1
    assert [t["name"] for t in document["view"]["tables"]] == ["orders"]


def test_json_admits_when_it_holds_unreviewed_meaning(client) -> None:
    seed()
    document = json.loads(body(client, "?format=json&include=all"))
    assert document["unreviewed_included"] == ["drafts"]


# --- reaching the engine ---------------------------------------------------


def test_the_console_prefix_routes_to_the_same_place(client) -> None:
    """The console addresses the engine at `/api`, and in the built image there
    is no proxy to strip it — so every console fetch 404'd in the one artifact
    we ship, while working in development."""
    seed()
    direct = client.get("/workspaces/demo/export")
    prefixed = client.get("/api/workspaces/demo/export")
    assert prefixed.status_code == 200
    assert prefixed.text == direct.text
    assert prefixed.headers["etag"] == direct.headers["etag"]


def test_the_prefix_does_not_swallow_a_real_path(client) -> None:
    """`/apiary` is not `/api`. Stripping on a prefix match rather than a path
    segment would route it to `ry`."""
    assert client.get("/apiary").status_code == 404
