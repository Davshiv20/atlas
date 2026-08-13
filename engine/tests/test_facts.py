from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.facts import (
    Fact,
    FactStatus,
    FactStore,
    Provenance,
    ProvenanceKind,
)
from atlas.metadata.yaml_store import YamlMetadataRepository
from atlas.policy import Trust, TrustAssessment, TrustFactors

LLM = Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="inferred from column name")
CHECK = Provenance(kind=ProvenanceKind.GROUNDED_CHECK, detail="orphan rate 0.0%", result="pass")


def assessment(confidence: float = 0.84) -> TrustAssessment:
    return TrustAssessment(
        state=Trust.OBSERVED,
        confidence=confidence,
        factors=TrustFactors(
            evidence_directness=0.9,
            authority=0.82,
            coverage=1.0,
            consistency=1.0,
            freshness=1.0,
        ),
        reasons=["complete scan"],
    )


def make_fact(**overrides) -> Fact:
    payload = {
        "subject": "deliverables.stage",
        "aspect": "semantics",
        "claim": "Lean Six Sigma phase the deliverable belongs to.",
        "confidence": 0.5,
        "provenance": [LLM],
    }
    return Fact(**{**payload, **overrides})


def test_ungrounded_fact_cannot_exceed_confidence_ceiling() -> None:
    with pytest.raises(ValidationError, match="cannot exceed confidence"):
        make_fact(confidence=0.85)


def test_ungrounded_fact_cannot_be_verified() -> None:
    with pytest.raises(ValidationError, match="cannot be marked verified"):
        make_fact(status=FactStatus.VERIFIED)


def test_grounded_fact_may_exceed_ceiling() -> None:
    fact = make_fact(confidence=0.95, provenance=[LLM, CHECK])
    assert fact.confidence == 0.95


def test_confidence_must_match_the_stored_trust_assessment() -> None:
    with pytest.raises(ValidationError, match="must equal the evidence-derived trust score"):
        make_fact(confidence=0.80, trust=assessment(0.84), provenance=[CHECK])


def test_trust_assessment_round_trips_with_the_fact(tmp_path) -> None:
    fact = make_fact(confidence=0.84, trust=assessment(), provenance=[CHECK])
    store = YamlMetadataRepository(tmp_path)
    store.write_facts("demo", FactStore(facts=[fact]))
    loaded = store.read_facts("demo").facts[0]
    assert loaded.trust == fact.trust
    assert loaded.trust is not None
    assert loaded.trust.band.value == "strongly_supported"


def test_merge_carries_verdict_when_claim_unchanged() -> None:
    verified = make_fact(
        confidence=0.9,
        provenance=[LLM, CHECK],
        status=FactStatus.VERIFIED,
        verified_by="shivam",
    )
    store = FactStore(facts=[verified]).merge([make_fact()])

    merged = store.by_id("deliverables.stage#semantics")
    assert merged is not None
    assert merged.status is FactStatus.VERIFIED
    assert merged.verified_by == "shivam"
    assert merged.confidence == 0.9
    # The carried verdict must appear as provenance, or the fact would be a
    # verified claim with no evidence behind it.
    assert any(p.kind is ProvenanceKind.HUMAN for p in merged.provenance)


def test_merge_resets_verdict_when_claim_changed() -> None:
    verified = make_fact(
        confidence=0.9, provenance=[LLM, CHECK], status=FactStatus.VERIFIED, verified_by="shivam"
    )
    store = FactStore(facts=[verified]).merge([make_fact(claim="Something materially different.")])

    merged = store.by_id("deliverables.stage#semantics")
    assert merged is not None
    assert merged.status is FactStatus.UNVERIFIED
    assert merged.supersedes == f"deliverables.stage#semantics@{verified.claim_hash}"


def test_round_trip_through_the_store(tmp_path) -> None:
    store = YamlMetadataRepository(tmp_path)
    store.write_facts("demo", FactStore(facts=[make_fact()]))
    assert store.read_facts("demo").facts == [make_fact()]


def test_reading_a_workspace_with_no_claims_is_empty(tmp_path) -> None:
    assert YamlMetadataRepository(tmp_path).read_facts("demo").facts == []


# --- plural aspects need a discriminator -----------------------------------


def test_two_joins_on_one_subject_both_survive_merge() -> None:
    """The bug this rule exists for: before discriminators, a second join claim
    replaced the first and `supersedes` made the loss look deliberate."""
    messages = make_fact(
        subject="conversations", aspect="join", discriminator="messages",
        claim="conversations.id = messages.conversation_id, 1:N.",
    )
    whiteboards = make_fact(
        subject="conversations", aspect="join", discriminator="whiteboards",
        claim="conversations.id = whiteboards.conversation_id, 1:1.",
    )
    store = FactStore().merge([messages, whiteboards])

    assert [f.id for f in store.facts] == [
        "conversations#join#messages",
        "conversations#join#whiteboards",
    ]
    assert all(f.supersedes is None for f in store.facts)


def test_plural_aspect_without_a_discriminator_is_refused() -> None:
    with pytest.raises(ValidationError, match="needs a discriminator"):
        make_fact(subject="conversations", aspect="join")


def test_singular_aspect_rejects_a_discriminator() -> None:
    """A table has one grain. Two ids for it would let a contradiction sit in
    the store with nothing to flag it."""
    with pytest.raises(ValidationError, match="holds one claim per subject"):
        make_fact(aspect="grain", discriminator="whatever")


@pytest.mark.parametrize("bad", ["Has Caps", "with space", "has#hash", "", "x" * 65])
def test_discriminators_are_url_safe_slugs(bad) -> None:
    with pytest.raises(ValidationError):
        make_fact(aspect="join", discriminator=bad)


def test_id_is_two_parts_without_a_discriminator() -> None:
    assert make_fact(aspect="semantics").id == "deliverables.stage#semantics"


def test_legacy_claims_still_load_and_are_marked(tmp_path, caplog) -> None:
    """Pre-discriminator catalogues must stay readable — but the assigned id
    carries `legacy-`, because such a claim may be several findings in one."""
    stored = _stored_claims(
        tmp_path,
        "- subject: conversations\n"
        "  aspect: join\n"
        "  claim: 'Join paths: a = b, c = d, e = f.'\n"
        "  confidence: 0.5\n"
        "  provenance:\n"
        "  - kind: llm_inference\n"
        "    detail: inferred\n",
    )
    assert len(stored.facts) == 1
    assert stored.facts[0].id.startswith("conversations#join#legacy-")


def test_legacy_migration_is_stable_across_reads(tmp_path) -> None:
    raw = (
        "- subject: orders\n  aspect: quality\n  claim: Something.\n  confidence: 0.5\n"
        "  provenance:\n  - kind: llm_inference\n    detail: inferred\n"
    )
    first = _stored_claims(tmp_path, raw).facts[0].id
    assert first == _stored_claims(tmp_path, raw).facts[0].id


def _stored_claims(root, entries: str) -> FactStore:
    """Read claims the store finds already on disk.

    Written as text rather than through `write_facts` on purpose: the point is
    what happens to a file some older version of Atlas left behind, and the
    current writer cannot produce one.
    """
    path = root / "demo" / "facts.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("facts:\n" + entries)
    return YamlMetadataRepository(root).read_facts("demo")
