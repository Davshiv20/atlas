from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atlas.decisions import record_decision
from atlas.endorsement import (
    Endorsement,
    as_decision_record,
    endorsement,
    observation_ids,
)
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
from atlas.facts import Fact, FactStatus, Provenance, ProvenanceKind

GUESS = [Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from column name")]


def check(*, orphans: int = 0) -> EvidenceRecord:
    """An executed check. Its id is a hash of what it saw, so changing the
    observation makes it a different record — which is what staleness reads."""
    return EvidenceRecord(
        type=EvidenceType.DETERMINISTIC_CHECK,
        authority=Authority.MEASURED,
        subjects=["orders.status"],
        assertion=Assertion(description="every keyed row matches"),
        observation={"source_rows": 100, "orphan_rows": orphans},
        scope=Scope(complete_scan=True, rows_examined=100),
        verdict=Verdict.PASSED if orphans == 0 else Verdict.FAILED,
        freshness=Freshness(valid_as_of=datetime.now(UTC)),
    )


def claim() -> Fact:
    return Fact(
        subject="orders.status",
        aspect="semantics",
        claim="Lifecycle state of the order.",
        confidence=0.6,
        provenance=GUESS,
    )


def test_a_claim_nobody_has_judged_is_none() -> None:
    assert endorsement([]).state is Endorsement.NONE


def test_policy_acceptance_is_not_a_human() -> None:
    assessment = endorsement([], auto_accepted=True)
    assert assessment.state is Endorsement.AUTO
    assert assessment.factors.corroboration == 0


def test_endorsing_records_who_and_what_they_saw() -> None:
    store = EvidenceStore()
    observed = check()
    store.add(observed)
    store.link(
        ClaimEvidence(
            claim_id=claim().id,
            evidence_id=observed.id,
            relationship=LinkKind.SUPPORTS,
            rationale="no orphans",
        )
    )

    fact, store = record_decision(
        claim(), store, reviewer="shivam", decision=FactStatus.VERIFIED
    )
    assessment = endorsement(store.for_claim(fact.id))

    assert assessment.state is Endorsement.ENDORSED
    assert assessment.factors.standing == ["shivam"]
    assert assessment.factors.scope == [observed.id]
    assert assessment.factors.currency == 1.0


def test_supplying_the_meaning_is_authored_not_endorsed() -> None:
    store = EvidenceStore()
    fact, store = record_decision(
        claim(),
        store,
        reviewer="shivam",
        decision=FactStatus.VERIFIED,
        text="Whether the order is awaiting payment or stock.",
    )
    assessment = endorsement(store.for_claim(fact.id))

    assert assessment.state is Endorsement.AUTHORED
    assert fact.claim == "Whether the order is awaiting payment or stock."


def test_endorsing_grounds_an_ungrounded_claim() -> None:
    """The reason the 409 stopped being reachable."""
    unbacked = claim()
    assert not unbacked.is_grounded

    fact, _ = record_decision(
        unbacked, EvidenceStore(), reviewer="shivam", decision=FactStatus.VERIFIED
    )
    assert fact.is_grounded


def test_a_dispute_is_evidence_against_not_a_deletion() -> None:
    store = EvidenceStore()
    fact, store = record_decision(
        claim(), store, reviewer="shivam", decision=FactStatus.REJECTED
    )
    pairs = store.for_claim(fact.id)

    assert endorsement(pairs).state is Endorsement.DISPUTED
    assert any(link.relationship is LinkKind.CONTRADICTS for link, _ in pairs)
    assert fact.claim, "the claim survives so a later reviewer can overturn it"


def test_an_endorsement_goes_stale_when_what_it_rested_on_changes() -> None:
    """The failure this whole model exists to remove.

    A reviewer confirms a claim while a check is passing. The check is re-run
    and now fails. Nothing about the reviewer's record changed — but what they
    agreed to did, and the claim must stop asserting their approval as current.
    """
    store = EvidenceStore()
    passing = check(orphans=0)
    store.add(passing)
    store.link(
        ClaimEvidence(
            claim_id=claim().id,
            evidence_id=passing.id,
            relationship=LinkKind.SUPPORTS,
            rationale="no orphans",
        )
    )

    fact, store = record_decision(
        claim(), store, reviewer="shivam", decision=FactStatus.VERIFIED
    )
    assert endorsement(store.for_claim(fact.id)).state is Endorsement.ENDORSED

    # The world moves: the same check now sees orphans, so it is a different
    # record, and the one the reviewer agreed to is no longer among the claim's
    # evidence.
    refreshed = EvidenceStore()
    failing = check(orphans=17)
    refreshed.add(failing)
    refreshed.link(
        ClaimEvidence(
            claim_id=fact.id,
            evidence_id=failing.id,
            relationship=LinkKind.CONTRADICTS,
            rationale="17 orphans",
        )
    )
    decision = next(
        record
        for record in store.records
        if record.type is EvidenceType.HUMAN_DECISION
    )
    refreshed.add(decision)
    refreshed.link(
        next(
            link
            for link in store.links
            if link.evidence_id == decision.id
        )
    )

    after = endorsement(refreshed.for_claim(fact.id))
    assert after.state is Endorsement.STALE
    assert after.factors.currency == 0.0
    assert "changed" in " ".join(after.reasons)


def test_the_latest_decision_wins_but_earlier_ones_are_kept() -> None:
    store = EvidenceStore()
    fact, store = record_decision(
        claim(), store, reviewer="ada", decision=FactStatus.VERIFIED
    )
    fact, store = record_decision(
        fact, store, reviewer="shivam", decision=FactStatus.REJECTED
    )

    pairs = store.for_claim(fact.id)
    assessment = endorsement(pairs)
    assert assessment.state is Endorsement.DISPUTED
    assert set(assessment.factors.standing) == {"ada", "shivam"}
    assert len([r for _, r in pairs if r.type is EvidenceType.HUMAN_DECISION]) == 2


def test_a_decision_is_not_part_of_its_own_grounds() -> None:
    """Otherwise every endorsement would go stale the moment anyone re-endorsed."""
    store = EvidenceStore()
    observed = check()
    store.add(observed)
    store.link(
        ClaimEvidence(
            claim_id=claim().id,
            evidence_id=observed.id,
            relationship=LinkKind.SUPPORTS,
            rationale="no orphans",
        )
    )
    fact, store = record_decision(
        claim(), store, reviewer="ada", decision=FactStatus.VERIFIED
    )
    assert observation_ids(store.for_claim(fact.id)) == [observed.id]


def test_a_review_taken_before_evidence_was_recorded_is_not_lost() -> None:
    """Existing workspaces carry `verified_by` and no decision record.

    Read strictly they endorse nothing, so a naive derivation silently
    un-approves every review anyone has already done. They project instead, and
    say plainly that their grounds are unknown.
    """
    from atlas.output import assess_facts

    reviewed = claim().model_copy(
        update={"status": FactStatus.VERIFIED, "verified_by": "shivam"}
    )
    reviewed = reviewed.model_copy(
        update={
            "provenance": [
                *reviewed.provenance,
                Provenance(kind=ProvenanceKind.HUMAN, detail="verified by shivam"),
            ]
        }
    )
    from atlas.facts import FactStore

    out = assess_facts(FactStore(facts=[reviewed]), EvidenceStore())
    settled = out.facts[0]

    assert settled.status is FactStatus.VERIFIED
    assert settled.endorsement is not None
    assert settled.endorsement.state is Endorsement.ENDORSED
    assert settled.endorsement.factors.standing == ["shivam"]
    assert "unknown" in " ".join(settled.endorsement.reasons)


def test_every_claim_gets_an_endorsement_even_with_no_evidence() -> None:
    """Skipping unlinked claims left a verified one carrying no endorsement —
    the same divergence this model exists to close, in miniature."""
    from atlas.facts import FactStore
    from atlas.output import assess_facts

    out = assess_facts(FactStore(facts=[claim()]), EvidenceStore())
    assert out.facts[0].endorsement is not None
    assert out.facts[0].endorsement.state is Endorsement.NONE


def test_a_decision_record_states_what_it_does_not_establish() -> None:
    record = as_decision_record(
        subject="orders.status",
        reviewer="shivam",
        statement="Lifecycle state.",
        supports=True,
        saw=[],
        authored=False,
        at=datetime.now(UTC) - timedelta(days=1),
    )
    assert record.authority is Authority.ASSERTED
    assert any("standing" in limitation for limitation in record.limitations)
