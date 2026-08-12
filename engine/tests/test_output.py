from __future__ import annotations

from atlas.adapters.base import CheckObservation, DatabaseAdapter, JoinCheck
from atlas.checks import run_check
from atlas.evidence import ClaimEvidence, EvidenceRecord, EvidenceStore, LinkKind
from atlas.facts import Fact, FactStore, Provenance, ProvenanceKind
from atlas.output import build_output
from atlas.snapshot import Snapshot, Table

GUESS = [Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from column name")]

def test_a_join_claim_does_not_mark_its_target_analyzed() -> None:
    """Reading `users` records `sessions#join#users`, whose subject is
    `sessions`. That table has not been read and must not claim it has."""
    snapshot = Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(schema_name="public", name="users", columns=[], primary_key=["id"]),
            Table(schema_name="public", name="sessions", columns=[], primary_key=["id"]),
        ],
    )
    store = FactStore(
        facts=[
            Fact(
                subject="users",
                aspect="grain",
                claim="One row per user.",
                confidence=0.6,
                provenance=GUESS,
            ),
            Fact(
                subject="sessions",
                aspect="join",
                discriminator="users",
                claim="sessions.user_id references users.id.",
                confidence=0.6,
                provenance=GUESS,
            ),
        ]
    )
    output = build_output(snapshot, store, [])
    by_name = {t.name: t for t in output.tables}
    assert by_name["users"].analyzed is True
    assert by_name["sessions"].analyzed is False


def _snapshot() -> Snapshot:
    return Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(schema_name="public", name="engagements", columns=[], primary_key=["id"]),
            Table(schema_name="public", name="users", columns=[], primary_key=["id"]),
        ],
    )


def _failed_join() -> EvidenceRecord:
    check = JoinCheck(
        source_relation="engagements",
        source_fields=["updated_by"],
        target_relation="users",
        target_fields=["id"],
    )
    record, _ = run_check(
        _StubAdapter(
            CheckObservation(
                check_type="join",
                observations={"source_rows": 26, "matched_rows": 2, "orphan_rows": 24},
                complete_scan=True,
                rows_examined=26,
                sql="SELECT …",
            )
        ),
        check,
        database="db",
    )
    return record


class _StubAdapter(DatabaseAdapter):
    def __init__(self, observation: CheckObservation) -> None:
        self.observation = observation

    def test_connection(self) -> None: ...
    def probe(self, namespace): ...
    def extract_structure(self, namespace): ...
    def profile(self, snapshot, on_table=None): ...
    def close(self) -> None: ...

    def execute_check(self, check) -> CheckObservation:
        return self.observation


def test_a_refuted_hypothesis_is_reported_on_the_table_it_was_about() -> None:
    """No claim means "nobody looked" or "someone looked and the answer was
    no". Four refuted `updated_by` joins were invisible in one run."""
    evidence = EvidenceStore()
    evidence.add(_failed_join())

    output = build_output(_snapshot(), FactStore(), [], evidence)
    by_name = {t.name: t for t in output.tables}

    assert by_name["users"].ruled_out == []  # the fact is about engagements
    ruled = by_name["engagements"].ruled_out
    assert len(ruled) == 1
    assert "updated_by" in ruled[0].hypothesis
    assert "24 of 26" in ruled[0].finding
    assert ruled[0].scope == "complete scan over 26 rows"


def test_legacy_confidence_is_recomputed_from_linked_evidence() -> None:
    check = JoinCheck(
        source_relation="engagements",
        source_fields=["updated_by"],
        target_relation="users",
        target_fields=["id"],
    )
    record, _ = run_check(
        _StubAdapter(
            CheckObservation(
                check_type="join",
                observations={"source_rows": 26, "matched_rows": 26, "orphan_rows": 0},
                complete_scan=True,
                rows_examined=26,
                sql="SELECT …",
            )
        ),
        check,
        database="db",
    )
    evidence = EvidenceStore(records=[record])
    claim_id = "engagements#join#users.updated_by"
    evidence.link(
        ClaimEvidence(
            claim_id=claim_id,
            evidence_id=record.id,
            relationship=LinkKind.SUPPORTS,
            rationale="all references matched",
        )
    )
    legacy = Fact(
        subject="engagements",
        aspect="join",
        discriminator="users.updated_by",
        claim="engagements.updated_by references users.id.",
        confidence=0.65,
        provenance=[
            Provenance(
                kind=ProvenanceKind.GROUNDED_CHECK,
                detail="legacy fixed score",
                result="pass",
            )
        ],
    )

    output = build_output(_snapshot(), FactStore(facts=[legacy]), [], evidence)
    emitted = next(t for t in output.tables if t.name == "engagements").joins[0].description

    assert emitted is not None
    assert emitted.confidence > 0.85
    assert emitted.trust is not None
    assert emitted.trust.factors.coverage == 1.0


def test_a_failure_a_claim_cites_is_not_reported_as_settled() -> None:
    """A cited failure is an unresolved contradiction shown on the claim.
    Repeating it here would read as a settled negative."""
    record = _failed_join()
    evidence = EvidenceStore()
    evidence.add(record)
    evidence.link(
        ClaimEvidence(
            claim_id="engagements#join#users",
            evidence_id=record.id,
            relationship=LinkKind.CONTRADICTS,
            rationale="engagements.updated_by references users.id",
        )
    )

    output = build_output(_snapshot(), FactStore(), [], evidence)
    assert {t.name: t.ruled_out for t in output.tables}["engagements"] == []


def test_no_evidence_means_no_ruled_out_section() -> None:
    output = build_output(_snapshot(), FactStore(), [])
    assert all(t.ruled_out == [] for t in output.tables)


def test_a_declared_key_and_its_claim_are_one_join_not_two() -> None:
    """Every relationship carries a claim now. Emitting the constraint and the
    claim as separate entries showed each enforced join twice, the second time
    with no target table."""
    from atlas.snapshot import ForeignKey

    snapshot = Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(schema_name="public", name="users", columns=[], primary_key=["id"]),
            Table(
                schema_name="public",
                name="sessions",
                columns=[],
                primary_key=["id"],
                foreign_keys=[
                    ForeignKey(
                        name=None,
                        columns=["user_id"],
                        referred_table="users",
                        referred_columns=["id"],
                    )
                ],
            ),
        ],
    )
    store = FactStore(
        facts=[
            Fact(
                subject="sessions",
                aspect="join",
                discriminator="users.user_id",
                claim="sessions.user_id references users.id.",
                confidence=0.6,
                provenance=GUESS,
            ),
            Fact(
                subject="sessions",
                aspect="join",
                discriminator="teams.owner_id",
                claim="sessions.owner_id references teams.id.",
                confidence=0.6,
                provenance=GUESS,
            ),
        ]
    )

    # The measured join carries its shape in the evidence that established it,
    # not in its discriminator — that value is an identity and is hashed
    # whenever a name is long or irregular, so it cannot be split back apart.
    record, _ = run_check(
        _StubAdapter(
            CheckObservation(
                check_type="join",
                observations={"source_rows": 10, "matched_rows": 10, "orphan_rows": 0},
                complete_scan=True,
                rows_examined=10,
                sql="SELECT …",
            )
        ),
        JoinCheck(
            source_relation="sessions",
            source_fields=["owner_id"],
            target_relation="teams",
            target_fields=["id"],
        ),
        database="db",
    )
    evidence = EvidenceStore()
    evidence.add(record)
    evidence.link(
        ClaimEvidence(
            claim_id="sessions#join#teams.owner_id",
            evidence_id=record.id,
            relationship=LinkKind.SUPPORTS,
            rationale="every keyed row matched",
        )
    )

    joins = {
        t.name: t.joins for t in build_output(snapshot, store, [], evidence).tables
    }["sessions"]

    assert len(joins) == 2
    declared = next(j for j in joins if j.enforced)
    assert declared.referred_table == "users"
    assert declared.description is not None  # the claim rides with the constraint

    measured = next(j for j in joins if not j.enforced)
    assert (measured.referred_table, measured.columns) == ("teams", ["owner_id"])


def test_an_answered_question_stops_counting_as_a_gap() -> None:
    """Once answered it is a claim, not an outstanding decision. Counting it
    keeps telling the reviewer to go and answer what they just answered."""
    from atlas.questions import Question

    snapshot = Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        tables=[Table(schema_name="public", name="orders", columns=[], primary_key=["id"])],
    )
    questions = [
        Question(subject="orders.status", question="Which states are terminal?",
                 evidence="open, closed", table="orders"),
        Question(subject="orders.total", question="Gross or net?", evidence="12.00",
                 table="orders").answered("Net of tax.", "shivam"),
    ]

    output = build_output(snapshot, FactStore(), questions)

    assert output.question_count == 1
    assert output.tables[0].open_questions == ["Which states are terminal?"]
