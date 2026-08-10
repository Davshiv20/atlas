"""Turning a reviewer's answer into evidence.

`ClaimPolicy.ceiling` caps business meaning at OBSERVED: no query establishes
what a column means to the organisation, whatever its scope. The policy already
lifts a claim past that for `HUMAN_DECISION` or `AUTHORITATIVE_ARTIFACT`
evidence — but nothing in the product ever built such a record, so the branch
was unreachable and every semantics claim in a run sat at the 0.65 ceiling.

This is that missing half. An answered question becomes an evidence record with
`Authority.ASSERTED`, which is the one kind of evidence that can settle meaning,
and the claim on that subject is re-scored against it like any other.

The record is deliberately shaped like the others: it states what it
establishes, what it does not, and who said so. An answer is authoritative
because a person with standing gave it, not because it is unfalsifiable — the
reviewer's name is on it, and a later reviewer can contradict it.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from atlas.facts import (
    PLURAL_ASPECTS,
    Consequence,
    Fact,
    FactStatus,
    FactStore,
    Provenance,
    ProvenanceKind,
)
from atlas.policy import evaluate
from atlas.questions import Question


def record_answer(
    question: Question,
    facts: FactStore,
    evidence: EvidenceStore,
) -> tuple[FactStore, EvidenceStore, Fact]:
    """Fold an answered question into the claims and the evidence.

    Returns the updated stores and the claim the answer established, which is
    either an existing claim on that subject re-scored, or a new one written
    from the answer itself.
    """
    if question.answer is None:
        raise ValueError(f"{question.id} has no answer to record")

    record = _as_evidence(question)
    evidence.add(record)

    existing = _claim_for(question, facts)
    claim_id = existing.id if existing else _claim_id(question)
    discriminator = existing.discriminator if existing else _discriminator_for(question)

    link = ClaimEvidence(
        claim_id=claim_id,
        evidence_id=record.id,
        relationship=LinkKind.SUPPORTS,
        rationale=question.answer[:200],
    )
    # Everything already backing this claim still counts. An answer is added to
    # the case, not substituted for it — a check that contradicts the answer
    # must keep contradicting it.
    pairs = evidence.for_claim(claim_id)
    evidence.link(link)
    trust, score, reasons = evaluate(question.aspect, [*pairs, (link, record)])

    settled = Fact(
        subject=question.subject,
        aspect=question.aspect,
        discriminator=discriminator,
        claim=question.answer,
        confidence=score,
        provenance=[
            *(existing.provenance if existing else []),
            Provenance(
                kind=ProvenanceKind.HUMAN,
                detail=f"{trust.value}: answered by {question.answered_by}; "
                f"{'; '.join(reasons)}",
                result="pass",
            ),
        ],
        consequence=existing.consequence if existing else Consequence.HIGH,
        # A person with standing said so. Queueing it for review would be
        # queueing the reviewer's own answer back to them.
        status=FactStatus.VERIFIED,
        verified_by=question.answered_by,
    )

    kept = [f for f in facts.facts if f.id != claim_id]
    return FactStore(facts=[*kept, settled]), evidence, settled


def _claim_for(question: Question, facts: FactStore) -> Fact | None:
    """The claim this answer settles, if the agent already made one.

    Matched on subject and aspect rather than on id: the agent may have
    attached a discriminator the question does not carry, and re-deriving the
    id from the question would miss it.
    """
    candidates = [
        f for f in facts.facts if f.subject == question.subject and f.aspect == question.aspect
    ]
    return candidates[0] if len(candidates) == 1 else None


def _claim_id(question: Question) -> str:
    discriminator = _discriminator_for(question)
    return f"{question.subject}#{question.aspect}" + (f"#{discriminator}" if discriminator else "")


def _discriminator_for(question: Question) -> str | None:
    """What distinguishes this claim from its siblings, for plural aspects.

    A subject can hold several `lifecycle` or `quality` claims, so those ids
    require one — and answering such a question with no matching claim already
    recorded used to raise out of the endpoint as a 500, leaving the question
    unsettled. The question's own id is stable and unique, which is exactly
    what the discriminator has to be.
    """
    if question.aspect not in PLURAL_ASPECTS:
        return None
    return question.id.removeprefix("question:")


def _as_evidence(question: Question) -> EvidenceRecord:
    return EvidenceRecord(
        type=EvidenceType.HUMAN_DECISION,
        authority=Authority.ASSERTED,
        subjects=_subjects(question.subject),
        assertion=Assertion(
            description=(
                f"{question.answered_by} answered: {question.question.strip()}"
            ),
        ),
        observation={"answer": question.answer, "asked": question.question},
        # A decision is not a measurement. Claiming a scan of the table would
        # misrepresent where the authority comes from.
        scope=Scope(complete_scan=False),
        verdict=Verdict.PASSED,
        reasons=[f"answered by {question.answered_by}"],
        limitations=[
            "Rests on the reviewer's standing, not on the data.",
            "A later observation can contradict it, and should be recorded when it does.",
        ],
        freshness=Freshness(
            valid_as_of=question.answered_at or datetime.now(UTC),
            invalidated_by=[f"schema_change:{subject}" for subject in _subjects(question.subject)],
        ),
        hypothesis={"question_id": question.id, "check_type": "human_decision"},
    )


def _subjects(subject: str) -> list[str]:
    """`orders.status` is a field on a relation; `orders` is the relation."""
    if "." in subject:
        table = subject.split(".")[0]
        return [f"relation:{table}", f"field:{subject}"]
    return [f"relation:{subject}"]
