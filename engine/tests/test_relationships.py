from __future__ import annotations

from atlas.adapters.base import CheckObservation, DatabaseAdapter
from atlas.evidence import Verdict
from atlas.relationships import (
    MAX_TARGETS_PER_COLUMN,
    Origin,
    as_claims,
    by_table,
    discover,
    propose,
)
from atlas.snapshot import Column, Enforcement, ForeignKey, Snapshot, Table


def column(name: str, data_type: str = "VARCHAR") -> Column:
    return Column(name=name, data_type=data_type, nullable=True)


def table(name: str, columns: list[str], *, fks: list[ForeignKey] | None = None) -> Table:
    return Table(
        schema_name="public",
        name=name,
        columns=[column("id")] + [column(c) for c in columns],
        primary_key=["id"],
        foreign_keys=fks or [],
    )


def fk(columns: list[str], target: str, enforcement=Enforcement.ENFORCED) -> ForeignKey:
    return ForeignKey(
        name=None,
        columns=columns,
        referred_table=target,
        referred_columns=["id"],
        enforcement=enforcement,
    )


def schema(*tables: Table) -> Snapshot:
    return Snapshot(
        database="db", schema_name="public", dialect="postgresql", tables=list(tables)
    )


class Answering(DatabaseAdapter):
    """Returns a fixed join measurement, so proposal and verdict logic are
    tested without a database."""

    def __init__(self, **observations) -> None:
        self.observations = observations
        self.asked: list[tuple[str, str]] = []

    def test_connection(self) -> None: ...
    def probe(self, namespace): ...
    def extract_structure(self, namespace): ...
    def profile(self, snapshot, on_table=None): ...
    def close(self) -> None: ...

    def execute_check(self, check) -> CheckObservation:
        self.asked.append((check.source_relation, check.target_relation))
        return CheckObservation(
            check_type="join",
            observations=dict(self.observations),
            complete_scan=True,
            rows_examined=self.observations.get("source_rows"),
            sql="SELECT …",
        )


# --- an enforced constraint is not re-measured ------------------------------


def test_an_enforced_key_is_taken_as_established_without_a_query() -> None:
    """Re-measuring what PostgreSQL already guarantees is how a run spends
    minutes proving what a constraint proves for free."""
    snapshot = schema(
        table("users", []),
        table("sessions", ["user_id"], fks=[fk(["user_id"], "users")]),
    )
    adapter = Answering(source_rows=10, null_keys=0, matched_rows=10, orphan_rows=0)

    found = discover(adapter, snapshot, database="db")

    assert adapter.asked == []  # no query issued
    assert len(found.verified) == 1
    assert found.evidence.records[0].authority.value == "enforced"


def test_a_declared_but_unenforced_key_is_checked() -> None:
    """Snowflake declares constraints it does not uphold. A declaration is a
    reason to check, not a substitute for checking."""
    snapshot = schema(
        table("users", []),
        table(
            "sessions",
            ["user_id"],
            fks=[fk(["user_id"], "users", Enforcement.DECLARED_NOT_ENFORCED)],
        ),
    )
    adapter = Answering(source_rows=10, null_keys=0, matched_rows=10, orphan_rows=0)

    discover(adapter, snapshot, database="db")

    assert adapter.asked == [("sessions", "users")]


# --- inference -------------------------------------------------------------


def test_a_unanimous_suffix_family_resolves_a_bare_column() -> None:
    """`updated_by` names no table. Every declared `_by` key in this schema
    points at `users`, which says what it means. Reading that is not inference
    about the business."""
    snapshot = schema(
        table("users", []),
        table("clients", ["created_by"], fks=[fk(["created_by"], "users")]),
        table("stages", ["updated_by"]),
    )
    candidates, _ = propose(snapshot)
    inferred = [c for c in candidates if c.origin is Origin.INFERRED]

    assert len(inferred) == 1
    assert (inferred[0].source_relation, inferred[0].target_relation) == ("stages", "users")
    assert "every declared _by column in this schema references users" in inferred[0].rationale


def test_the_exact_column_name_wins_over_its_family() -> None:
    """`author_id -> people` declared beats the `_id` family pointing at
    `documents`: the specific convention is the one the schema states."""
    snapshot = schema(
        table("people", []),
        table("documents", []),
        table("notes", ["author_id"], fks=[fk(["author_id"], "people")]),
        table("comments", ["author_id"]),
    )
    candidates, _ = propose(snapshot)
    inferred = [c for c in candidates if c.origin is Origin.INFERRED]

    assert [(c.source_relation, c.target_relation) for c in inferred] == [
        ("comments", "people")
    ]


def test_a_split_family_settles_nothing() -> None:
    """Two targets for one suffix is not a convention. Falling back to the
    name is right; guessing the more popular one is not."""
    snapshot = schema(
        table("users", []),
        table("teams", []),
        table("a", ["owner_key"], fks=[fk(["owner_key"], "users")]),
        table("b", ["holder_key"], fks=[fk(["holder_key"], "teams")]),
        table("c", ["assignee_key"]),
    )
    candidates, skipped = propose(snapshot)

    assert [c for c in candidates if c.origin is Origin.INFERRED] == []
    assert any("assignee" in reason for reason in skipped)


def test_a_named_entity_resolves_to_its_table_in_either_number() -> None:
    snapshot = schema(table("users", []), table("sessions", ["user_id"]))
    candidates, _ = propose(snapshot)
    assert [(c.source_relation, c.target_relation) for c in candidates] == [
        ("sessions", "users")
    ]


def test_a_key_of_the_wrong_type_is_never_proposed() -> None:
    """A VARCHAR column cannot reference an integer key, whatever it is
    called."""
    users = Table(
        schema_name="public",
        name="users",
        columns=[Column(name="id", data_type="INTEGER", nullable=False)],
        primary_key=["id"],
    )
    snapshot = schema(users, table("sessions", ["user_id"]))
    candidates, skipped = propose(snapshot)

    assert candidates == []
    assert any("user" in reason for reason in skipped)


def test_a_column_with_too_many_possible_targets_is_left_alone() -> None:
    """A name that resolves to nothing is a poor reason to issue a query
    against every table in someone's production database."""
    versions = [table(f"thing{n}_versions", []) for n in range(MAX_TARGETS_PER_COLUMN + 1)]
    snapshot = schema(*versions, table("deliverables", ["current_version_id"]))

    candidates, skipped = propose(snapshot)

    assert candidates == []
    assert any("too vague to test" in reason for reason in skipped)


def test_an_unresolvable_name_is_reported_rather_than_guessed() -> None:
    snapshot = schema(table("users", []), table("deliverables", ["location_ref"]))
    candidates, skipped = propose(snapshot)
    assert candidates == []
    assert any("location" in reason for reason in skipped)


def test_a_column_that_already_has_a_constraint_is_not_re_proposed() -> None:
    snapshot = schema(
        table("users", []),
        table("sessions", ["user_id"], fks=[fk(["user_id"], "users")]),
    )
    candidates, _ = propose(snapshot)
    assert [c.origin for c in candidates] == [Origin.DECLARED]


# --- the data decides ------------------------------------------------------


def test_a_proposed_relationship_the_data_refutes_does_not_hold() -> None:
    """The name suggests it; only the check settles it."""
    snapshot = schema(table("users", []), table("sessions", ["user_id"]))
    adapter = Answering(source_rows=100, null_keys=0, matched_rows=0, orphan_rows=100)

    found = discover(adapter, snapshot, database="db")

    assert found.relationships[0].verdict is Verdict.FAILED
    assert found.verified == []
    assert as_claims(found) == ([], [])


def test_two_keys_to_one_table_are_two_claims() -> None:
    """`created_by` and `updated_by` both reference `users`. Keying a claim on
    the target alone collapses them into one reviewable decision."""
    snapshot = schema(
        table("users", []),
        table(
            "clients",
            ["created_by", "updated_by"],
            fks=[fk(["created_by"], "users"), fk(["updated_by"], "users")],
        ),
    )
    facts, _ = as_claims(discover(Answering(), snapshot, database="db"))

    assert len({f.id for f in facts}) == 2


def test_an_enforced_relationship_is_not_queued_for_a_human() -> None:
    """A review queue full of enforced foreign keys is a queue nobody reads:
    approving one is approving PostgreSQL."""
    snapshot = schema(
        table("users", []),
        table("sessions", ["user_id"], fks=[fk(["user_id"], "users")]),
    )
    facts, _ = as_claims(discover(Answering(), snapshot, database="db"))

    assert facts[0].status.value == "auto_accepted"
    assert facts[0].confidence > 0.9


def test_the_map_reaches_both_ends_of_a_relationship() -> None:
    """Reading `users` you need to know what points at it; reading `sessions`
    you need to know where it points."""
    snapshot = schema(
        table("users", []),
        table("sessions", ["user_id"], fks=[fk(["user_id"], "users")]),
    )
    lines = by_table(discover(Answering(), snapshot, database="db"))

    assert "sessions.user_id -> users.id" in lines["sessions"][0]
    assert lines["users"] == lines["sessions"]


def test_the_map_replaces_model_authored_joins_rather_than_joining_them() -> None:
    """A relationship has one owner. Leaving the agent's earlier claim beside
    the derived one gives two answers to a settled question — 62 join claims
    where the schema has 37."""
    from atlas.facts import Fact, FactStore, Provenance, ProvenanceKind
    from atlas.workspace import Workspace

    snapshot = schema(
        table("users", []),
        table("sessions", ["user_id"], fks=[fk(["user_id"], "users")]),
    )
    workspace = Workspace("demo", root=_tmp())
    workspace.create_manifest("test-source")
    workspace.publish_snapshot(snapshot)
    FactStore(
        facts=[
            Fact(
                subject="sessions",
                aspect="join",
                discriminator="users",
                claim="sessions.user_id references users.id.",
                confidence=0.6,
                provenance=[Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="guessed")],
            ),
            Fact(
                subject="sessions",
                aspect="grain",
                claim="One row per session.",
                confidence=0.6,
                provenance=[Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="guessed")],
            ),
        ]
    ).write(workspace.facts_path)

    found = discover(Answering(), snapshot, database="db")
    facts, links = as_claims(found)
    workspace.absorb_relationships(facts, links, found.evidence)

    stored = workspace.read_facts().facts
    joins = [f for f in stored if f.aspect == "join"]
    assert [f.id for f in joins] == ["sessions#join#users.user_id"]
    # Everything that is not a join is untouched.
    assert any(f.aspect == "grain" for f in stored)


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


def test_a_hostile_identifier_cannot_abort_the_run() -> None:
    """Table and column names come from someone else's schema. A mixed-case or
    very long pair produced a discriminator the fact model rejects, and with no
    guard that raised inside the analyze job — killing the whole run before a
    single table was read."""
    long_target = "deliverable_version_approval_records"
    snapshot = schema(
        Table(
            schema_name="public",
            name=long_target,
            columns=[column("id")],
            primary_key=["id"],
        ),
        table(
            "Deliverables",
            ["current_deliverable_version_id"],
            fks=[fk(["current_deliverable_version_id"], long_target)],
        ),
    )

    facts, _ = as_claims(discover(Answering(), snapshot, database="db"))

    assert len(facts) == 1
    assert facts[0].discriminator and len(facts[0].discriminator) <= 64
