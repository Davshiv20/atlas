"""Turning a reviewer's decision into evidence.

The sibling of `answers.record_answer`. That one handles a person supplying a
meaning; this one handles a person endorsing or disputing a meaning already
proposed. Both write the same kind of record, because they are the same act at
different specificity — see `APPROVAL.md`.

Kept apart from `endorsement` so that module stays at `policy`'s layer and can
be a field on `Fact` without a cycle: `facts` imports `policy` and `endorsement`,
never the other way around. The derivation is policy; writing the record is not.
"""

from __future__ import annotations

from atlas.endorsement import (
    as_decision_record,
    endorsement,
    observation_ids,
    projected_status,
)
from atlas.evidence import ClaimEvidence, EvidenceStore, LinkKind
from atlas.facts import Fact, FactStatus, Provenance, ProvenanceKind


def record_decision(
    fact: Fact,
    evidence: EvidenceStore,
    *,
    reviewer: str,
    decision: FactStatus,
    text: str | None = None,
) -> tuple[Fact, EvidenceStore]:
    """Write a reviewer's decision as evidence and re-derive the claim.

    The status that comes back is a projection of what the evidence now says,
    not the `decision` argument echoed: passing `verified` records that a person
    backed the claim, and the state derived from that record may still be
    `disputed` if someone later disagreed, or `stale` if the ground moved.
    """
    supports = decision is not FactStatus.REJECTED
    authored = text is not None and text.strip() != fact.claim
    statement = (text or fact.claim).strip()

    before = evidence.for_claim(fact.id)
    record = as_decision_record(
        subject=fact.subject,
        reviewer=reviewer,
        statement=statement,
        supports=supports,
        # What they were shown. A decision is not part of its own grounds, and
        # neither is anyone else's — otherwise every endorsement would go stale
        # the moment a second person endorsed the same claim.
        saw=observation_ids(before),
        authored=authored,
    )
    evidence.add(record)
    link = ClaimEvidence(
        claim_id=fact.id,
        evidence_id=record.id,
        relationship=LinkKind.SUPPORTS if supports else LinkKind.CONTRADICTS,
        rationale=statement[:200],
    )
    evidence.link(link)

    assessment = endorsement([*before, (link, record)])
    return (
        fact.model_copy(
            update={
                "claim": statement,
                "provenance": [
                    *fact.provenance,
                    Provenance(
                        kind=ProvenanceKind.HUMAN,
                        detail=f"{assessment.state.value} by {reviewer}",
                        result="pass" if supports else "fail",
                    ),
                ],
                "endorsement": assessment,
                "status": FactStatus(projected_status(assessment.state)),
                "verified_by": reviewer,
            }
        ),
        evidence,
    )
