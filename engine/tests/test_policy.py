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
from atlas.policy import Trust, evaluate

NOW = datetime(2026, 8, 6, tzinfo=UTC)


def record(**overrides) -> EvidenceRecord:
    payload = {
        "type": EvidenceType.DETERMINISTIC_CHECK,
        "authority": Authority.MEASURED,
        "subjects": ["relation:public.messages"],
        "assertion": Assertion(
            description="Every message belongs to exactly one conversation.",
            conditions={"orphan_count": {"equals": 0}},
        ),
        "observation": {"orphan_count": 0, "message_count": 2798},
        "scope": Scope(complete_scan=True, rows_examined=2798),
        "verdict": Verdict.PASSED,
        "freshness": Freshness(valid_as_of=NOW),
    }
    return EvidenceRecord(**{**payload, **overrides})


def link(evidence: EvidenceRecord, relationship=LinkKind.SUPPORTS) -> ClaimEvidence:
    return ClaimEvidence(
        claim_id="messages#join#conversations",
        evidence_id=evidence.id,
        relationship=relationship,
        rationale="Complete scan found no orphan.",
        support_scope=["join validity", "observed coverage"],
        does_not_support=["whether the relationship is mandatory by design"],
    )


# --- identity and immutability ---------------------------------------------


def test_id_is_content_addressed_not_dated() -> None:
    """A re-run on the same data is the same record; a dated id would collide
    on a second run the same day and break append-only history."""
    assert record().id == record().id
    assert record().id != record(observation={"orphan_count": 3}).id


def test_adding_the_same_observation_twice_is_a_no_op() -> None:
    store = EvidenceStore()
    store.add(record())
    store.add(record())
    assert len(store.records) == 1


def test_a_different_observation_is_a_different_record() -> None:
    store = EvidenceStore()
    store.add(record())
    store.add(record(observation={"orphan_count": 12}, verdict=Verdict.FAILED))
    assert len(store.records) == 2


# --- what can support a claim ----------------------------------------------


def test_failed_checks_cannot_verify() -> None:
    assert not record(verdict=Verdict.FAILED).is_verification


def test_inference_signals_never_verify() -> None:
    """However confident it sounds, a model saying so is not evidence."""
    signal = record(type=EvidenceType.INFERENCE_SIGNAL, authority=Authority.WEAK)
    assert not signal.is_verification


# --- trust computation -----------------------------------------------------


def test_complete_deterministic_check_verifies_a_structural_claim() -> None:
    evidence = record()
    trust, score, _ = evaluate("join", [(link(evidence), evidence)])
    assert trust is Trust.VERIFIED
    assert score > 0.8


def test_sampled_evidence_is_observed_not_verified() -> None:
    """The honest answer for a warehouse table where a complete scan is not
    affordable — a real observation with an expiry, not a verification."""
    evidence = record(scope=Scope(complete_scan=False, sampled=True, sample_fraction=0.01))
    trust, _, reasons = evaluate("join", [(link(evidence), evidence)])
    assert trust is Trust.OBSERVED
    assert any("sampled" in r for r in reasons)


def test_enforced_constraint_outranks_a_measured_check() -> None:
    evidence = record(type=EvidenceType.DATABASE_CONSTRAINT, authority=Authority.ENFORCED)
    trust, score, _ = evaluate("join", [(link(evidence), evidence)])
    assert trust is Trust.ENFORCED
    assert score > 0.9


def test_no_evidence_means_unsupported() -> None:
    trust, score, reasons = evaluate("semantics", [])
    assert trust is Trust.UNSUPPORTED
    assert score < 0.3
    assert reasons == ["no supporting evidence"]


def test_warning_verdict_costs_confidence_without_hiding_the_pass() -> None:
    clean = record()
    warned = record(observation={"orphan_count": 180}, verdict=Verdict.PASSED_WITH_WARNING)
    _, clean_score, _ = evaluate("join", [(link(clean), clean)])
    trust, warned_score, reasons = evaluate("join", [(link(warned), warned)])
    assert trust is Trust.VERIFIED
    assert warned_score < clean_score
    assert any("warning" in r for r in reasons)


# --- contradictions are not averaged ---------------------------------------


def test_a_contradiction_makes_the_claim_unresolved_not_middling() -> None:
    """The rule this exists to enforce: strong support plus one conflicting
    observation is unresolved, not 'somewhat confident'."""
    support = record()
    conflict = record(observation={"orphan_count": 41}, verdict=Verdict.FAILED)
    trust, score, reasons = evaluate(
        "join",
        [(link(support), support), (link(conflict, LinkKind.CONTRADICTS), conflict)],
    )
    assert trust is Trust.CONTRADICTED
    assert score < 0.2
    assert any("not averaged away" in r for r in reasons)


# --- claim type changes what evidence is worth -----------------------------


def test_a_data_check_cannot_establish_business_meaning() -> None:
    """An enforced foreign key is excellent evidence for a relationship and
    near worthless for 'this table represents a customer'."""
    evidence = record(type=EvidenceType.DATABASE_CONSTRAINT, authority=Authority.ENFORCED)
    trust, _, reasons = evaluate("semantics", [(link(evidence), evidence)])
    assert trust is Trust.OBSERVED
    assert any("authoritative source" in r for r in reasons)


def test_a_human_with_standing_lifts_a_business_claim() -> None:
    evidence = record(type=EvidenceType.HUMAN_DECISION, authority=Authority.ASSERTED)
    trust, score, _ = evaluate("semantics", [(link(evidence), evidence)])
    assert trust is Trust.AUTHORITATIVE
    assert score > 0.95


def test_grain_from_a_sample_is_never_verified() -> None:
    """Do not verify grain from five rows — nor from one percent of them."""
    evidence = record(scope=Scope(complete_scan=False, sampled=True, rows_examined=1000))
    trust, _, _ = evaluate("grain", [(link(evidence), evidence)])
    assert trust is Trust.OBSERVED


# --- the link carries the boundary -----------------------------------------


def test_links_record_what_the_evidence_does_not_support() -> None:
    evidence = record()
    store = EvidenceStore()
    store.add(evidence)
    store.link(link(evidence))
    (found_link, _), = store.for_claim("messages#join#conversations")
    assert "mandatory by design" in found_link.does_not_support[0]


def test_contradictions_are_queryable(tmp_path) -> None:
    evidence = record(verdict=Verdict.FAILED)
    store = EvidenceStore()
    store.add(evidence)
    store.link(link(evidence, LinkKind.CONTRADICTS))
    assert len(store.contradictions("messages#join#conversations")) == 1


def test_round_trip_through_disk(tmp_path) -> None:
    store = EvidenceStore()
    store.add(record())
    store.link(link(record()))
    path = tmp_path / "evidence.yaml"
    store.write(path)
    assert EvidenceStore.read(path) == store


@pytest.mark.parametrize("aspect", ["grain", "join", "quality"])
def test_structural_claims_can_reach_enforced(aspect) -> None:
    evidence = record(
        type=EvidenceType.DATABASE_CONSTRAINT,
        authority=Authority.ENFORCED,
        scope=Scope(complete_scan=True),
    )
    trust, _, _ = evaluate(aspect, [(link(evidence), evidence)])
    assert trust is Trust.ENFORCED
