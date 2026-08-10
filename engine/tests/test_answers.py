from __future__ import annotations

from atlas.answers import record_answer
from atlas.evidence import Authority, EvidenceStore, EvidenceType
from atlas.facts import Fact, FactStatus, FactStore, Provenance, ProvenanceKind
from atlas.questions import Question, QuestionLog, QuestionStatus

GUESS = [Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from the column name")]


def asked(subject: str = "deliverables.process_id_ref", aspect: str = "semantics") -> Question:
    return Question(
        subject=subject,
        aspect=aspect,
        question="One identifier space or several?",
        evidence="P1, PR-02, P001, Inc-1",
        table="deliverables",
    )


def test_an_answer_is_the_only_thing_that_passes_the_business_ceiling() -> None:
    """`ClaimPolicy.ceiling` caps semantics at OBSERVED, and the policy's own
    escape hatch was unreachable: nothing in the product built a human
    decision, so every semantics claim sat at 0.65 forever."""
    _, evidence, claim = record_answer(
        asked().answered("Four legacy systems feed this field.", "shivam"),
        FactStore(),
        EvidenceStore(),
    )

    assert claim.confidence > 0.9
    assert claim.status is FactStatus.VERIFIED
    assert claim.verified_by == "shivam"
    assert evidence.records[0].type is EvidenceType.HUMAN_DECISION
    assert evidence.records[0].authority is Authority.ASSERTED


def test_an_answer_lifts_the_claim_the_agent_already_made() -> None:
    """The reviewer is settling an open question, not filing a second opinion
    beside it."""
    existing = Fact(
        subject="deliverables.process_id_ref",
        aspect="semantics",
        claim="An identifier of some external process.",
        confidence=0.65,
        provenance=GUESS,
    )
    facts, _, claim = record_answer(
        asked().answered("Four legacy systems feed this field.", "shivam"),
        FactStore(facts=[existing]),
        EvidenceStore(),
    )

    assert len(facts.facts) == 1  # replaced, not duplicated
    assert claim.claim == "Four legacy systems feed this field."
    assert claim.confidence > existing.confidence
    # The model's reasoning is kept beneath the reviewer's.
    assert claim.provenance[0].kind is ProvenanceKind.LLM_INFERENCE
    assert claim.provenance[-1].kind is ProvenanceKind.HUMAN


def test_the_reason_does_not_describe_a_decision_as_a_sample() -> None:
    """"sampled only — expires as data changes" is wrong about a person's
    answer in both halves."""
    _, _, claim = record_answer(
        asked().answered("Four legacy systems.", "shivam"), FactStore(), EvidenceStore()
    )
    assert "sampled only" not in claim.provenance[-1].detail
    assert "standing" in claim.provenance[-1].detail


def test_answering_cannot_erase_a_contradiction() -> None:
    """An answer joins the case; it does not replace it. A check that refutes
    the claim must keep refuting it."""
    from atlas.adapters.base import CheckObservation, DatabaseAdapter, GrainCheck
    from atlas.checks import run_check
    from atlas.evidence import ClaimEvidence, LinkKind

    class Failing(DatabaseAdapter):
        def test_connection(self) -> None: ...
        def probe(self, namespace): ...
        def extract_structure(self, namespace): ...
        def profile(self, snapshot): ...
        def close(self) -> None: ...

        def execute_check(self, check) -> CheckObservation:
            return CheckObservation(
                check_type="grain",
                observations={"total": 10, "distinct_keys": 4, "null_keys": 0},
                complete_scan=True,
                sql="SELECT …",
            )

    record, _ = run_check(Failing(), GrainCheck(relation="orders", key_fields=["id"]), database="d")
    evidence = EvidenceStore()
    evidence.add(record)
    evidence.link(
        ClaimEvidence(
            claim_id="orders#grain",
            evidence_id=record.id,
            relationship=LinkKind.CONTRADICTS,
            rationale="one row per order",
        )
    )

    _, _, claim = record_answer(
        asked(subject="orders", aspect="grain").answered("One row per order.", "shivam"),
        FactStore(),
        evidence,
    )

    assert claim.confidence < 0.2
    assert "contradicting" in claim.provenance[-1].detail


def test_a_question_keeps_its_identity_across_runs() -> None:
    """Re-analysis re-asks. A reviewer must not be made to answer the same
    thing twice because the evidence string was reworded."""
    first = asked()
    again = first.model_copy(update={"evidence": "P1, PR-02, P001, Inc-1, X-9"})
    assert first.id == again.id


def test_an_answer_survives_re_analysis() -> None:
    settled = asked().answered("Four legacy systems.", "shivam")
    log = QuestionLog(questions=[settled])

    merged = log.merge([asked()])  # the same question, freshly asked

    assert merged.questions[0].status is QuestionStatus.ANSWERED
    assert merged.questions[0].answer == "Four legacy systems."
    assert merged.open == []


def test_a_dismissal_establishes_nothing() -> None:
    """Setting a question aside is not answering it: no claim moves and no
    evidence is recorded."""
    question = asked().dismissed("The column is dead; nothing writes to it.", "shivam")
    assert question.status is QuestionStatus.DISMISSED
    assert question.settled


def test_a_plural_aspect_question_can_be_answered() -> None:
    """A subject can hold several lifecycle claims, so its id needs a
    discriminator. Without one the endpoint raised a 500 and left the question
    open — every plural aspect `ask_human` offers was unanswerable."""
    question = Question(
        subject="orders",
        aspect="lifecycle",
        question="When is an order considered closed?",
        evidence="status, closed_at",
        table="orders",
    ).answered("When closed_at is set; status alone is not authoritative.", "shivam")

    _, _, claim = record_answer(question, FactStore(), EvidenceStore())

    assert claim.discriminator  # required for a plural aspect
    assert claim.id.startswith("orders#lifecycle#")
    assert claim.confidence > 0.9


def test_an_answer_still_lands_on_the_claim_it_settles_when_plural() -> None:
    """The discriminator must match the claim already recorded, not invent a
    second one beside it."""
    existing = Fact(
        subject="orders",
        aspect="lifecycle",
        discriminator="closure",
        claim="Orders close when status flips.",
        confidence=0.65,
        provenance=GUESS,
    )
    facts, _, claim = record_answer(
        Question(
            subject="orders", aspect="lifecycle", question="When closed?",
            evidence="x", table="orders",
        ).answered("When closed_at is set.", "shivam"),
        FactStore(facts=[existing]),
        EvidenceStore(),
    )

    assert len(facts.facts) == 1
    assert claim.discriminator == "closure"


def test_an_answer_survives_a_run_that_does_not_re_ask_it() -> None:
    """The id hashes the question text, so re-analysis rarely reproduces one
    byte for byte. Keying the merge off the incoming batch alone dropped every
    answer for that table on each re-run."""
    settled = asked().answered("Four legacy systems.", "shivam")
    reworded = Question(
        subject="deliverables.process_id_ref",
        question="One identifier space or several? (reworded)",
        evidence="P1, PR-02",
        table="deliverables",
    )

    merged = QuestionLog(questions=[settled]).merge([reworded])

    assert settled.id in {q.id for q in merged.questions}
    assert merged.questions[0].answer == "Four legacy systems."
    assert len(merged.open) == 1  # the reworded one is still outstanding
