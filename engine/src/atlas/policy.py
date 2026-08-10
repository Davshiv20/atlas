"""Evidence-derived trust assessment for semantic claims.

`confidence` is retained as Atlas's compact trust score. It is not a probability
and it is never chosen by the model. The score summarizes five inspectable
properties of the evidence:

* directness — how directly the observation bears on this claim type;
* authority — whether the source is weak, measured, asserted, or enforced;
* coverage — sampled, complete, or authoritative rather than measured;
* consistency — whether supporting observations agree cleanly;
* freshness — how recently the supporting evidence was captured.

The discrete `Trust` state answers *what kind of support established the claim*.
The continuous score answers *how strong is the case within that state*. Review
priority remains separate and is driven by `Consequence` elsewhere.

Contradictions are never averaged into a middling score. A claim with strong
support and a conflicting observation is contradicted and explicitly capped in
the unsupported band until the conflict is resolved.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field, computed_field

from atlas.evidence import (
    Authority,
    ClaimEvidence,
    EvidenceRecord,
    EvidenceType,
    LinkKind,
    Verdict,
)


class Trust(StrEnum):
    """What established the claim, not how important the claim is."""

    UNSUPPORTED = "unsupported"
    SIGNAL = "signal"
    OBSERVED = "observed"
    VERIFIED = "verified"
    ENFORCED = "enforced"
    AUTHORITATIVE = "authoritative"
    CONTRADICTED = "contradicted"


class TrustBand(StrEnum):
    """Human interpretation of a score; intentionally broader than a decimal."""

    UNSUPPORTED = "unsupported"
    WEAK_SIGNALS = "weak_signals"
    PLAUSIBLE = "plausible"
    STRONGLY_SUPPORTED = "strongly_supported"
    HIGHLY_TRUSTED = "highly_trusted"
    AUTHORITATIVE_OR_ENFORCED = "authoritative_or_enforced"
    CONFLICTED = "conflicted"


class TrustFactors(BaseModel):
    """Inspectable inputs to the trust score, each normalized to 0..1."""

    evidence_directness: float = Field(ge=0.0, le=1.0)
    authority: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    consistency: float = Field(ge=0.0, le=1.0)
    freshness: float = Field(ge=0.0, le=1.0)


class TrustAssessment(BaseModel):
    """Why a claim has its confidence score.

    Stored with a claim so API clients do not have to reverse-engineer a number
    from prose provenance. `confidence` remains duplicated on `Fact` for
    backwards compatibility and queue ordering; model validation keeps the two
    equal for newly assessed claims.
    """

    state: Trust
    confidence: float = Field(ge=0.0, le=1.0)
    factors: TrustFactors
    reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def band(self) -> TrustBand:
        if self.state is Trust.CONTRADICTED:
            return TrustBand.CONFLICTED
        return band_for(self.confidence)


# Score composition. These weights are product policy, not statistics; keeping
# them named and adding to one makes changes reviewable rather than magical.
FACTOR_WEIGHTS = TrustFactors(
    evidence_directness=0.30,
    authority=0.25,
    coverage=0.20,
    consistency=0.15,
    freshness=0.10,
)

# State ceilings preserve semantic honesty without collapsing every claim in a
# state to one hardcoded score. Business meaning can be strongly supported by
# data but cannot enter the verified/authoritative band without standing.
STATE_CEILING: dict[Trust, float] = {
    Trust.CONTRADICTED: 0.19,
    Trust.UNSUPPORTED: 0.24,
    Trust.SIGNAL: 0.49,
    Trust.OBSERVED: 0.84,
    Trust.VERIFIED: 0.94,
    Trust.ENFORCED: 0.99,
    Trust.AUTHORITATIVE: 1.00,
}

TRUST_RANK: dict[Trust, int] = {
    Trust.CONTRADICTED: -1,
    Trust.UNSUPPORTED: 0,
    Trust.SIGNAL: 1,
    Trust.OBSERVED: 2,
    Trust.VERIFIED: 3,
    Trust.ENFORCED: 4,
    Trust.AUTHORITATIVE: 5,
}


class ClaimPolicy:
    """What a claim type can establish from data without domain authority."""

    BUSINESS: ClassVar[frozenset[str]] = frozenset(
        {"semantics", "metric", "unit", "lifecycle"}
    )

    @staticmethod
    def requires_complete_scan(aspect: str) -> bool:
        return aspect == "grain"

    @staticmethod
    def ceiling(aspect: str) -> Trust:
        return Trust.OBSERVED if aspect in ClaimPolicy.BUSINESS else Trust.ENFORCED


def band_for(confidence: float) -> TrustBand:
    if confidence < 0.25:
        return TrustBand.UNSUPPORTED
    if confidence < 0.50:
        return TrustBand.WEAK_SIGNALS
    if confidence < 0.70:
        return TrustBand.PLAUSIBLE
    if confidence < 0.85:
        return TrustBand.STRONGLY_SUPPORTED
    if confidence < 0.95:
        return TrustBand.HIGHLY_TRUSTED
    return TrustBand.AUTHORITATIVE_OR_ENFORCED


def _trust_from(record: EvidenceRecord, aspect: str) -> Trust:
    if record.type is EvidenceType.INFERENCE_SIGNAL:
        return Trust.SIGNAL
    if record.authority is Authority.ENFORCED:
        return Trust.ENFORCED
    if record.type in (EvidenceType.HUMAN_DECISION, EvidenceType.AUTHORITATIVE_ARTIFACT):
        return Trust.AUTHORITATIVE
    if record.type is EvidenceType.COLUMN_PROFILE:
        return Trust.OBSERVED
    if record.verdict is Verdict.OBSERVED:
        return Trust.OBSERVED
    if record.type is EvidenceType.DETERMINISTIC_CHECK:
        if not record.scope.is_durable:
            return Trust.OBSERVED
        if ClaimPolicy.requires_complete_scan(aspect) and not record.scope.complete_scan:
            return Trust.OBSERVED
        return Trust.VERIFIED
    return Trust.OBSERVED


def _directness(aspect: str, link: ClaimEvidence, record: EvidenceRecord) -> float:
    if record.type in (EvidenceType.HUMAN_DECISION, EvidenceType.AUTHORITATIVE_ARTIFACT):
        value = 1.0
    elif record.type is EvidenceType.DATABASE_CONSTRAINT:
        value = 1.0 if aspect in {"join", "grain", "quality"} else 0.58
    elif record.type is EvidenceType.DETERMINISTIC_CHECK:
        check_type = str(record.hypothesis.get("check_type", ""))
        expected = {
            "grain": {"grain"},
            "join": {"join"},
            "lifecycle": {"ordering", "distribution", "nullability"},
            "semantics": {"distribution", "nullability", "ordering"},
            "unit": {"distribution"},
            "quality": {"distribution", "nullability", "ordering", "join"},
        }.get(aspect, set())
        value = 1.0 if check_type in expected else 0.78
    elif record.type is EvidenceType.COLUMN_PROFILE:
        value = 0.88 if aspect in ClaimPolicy.BUSINESS else 0.62
    elif record.type is EvidenceType.QUERY_USAGE_PATTERN:
        value = 0.82
    elif record.type is EvidenceType.GOLDEN_QUESTION:
        value = 0.92
    else:
        value = 0.38

    if link.support_scope:
        value += 0.04
    if link.does_not_support:
        value -= min(0.12, len(link.does_not_support) * 0.04)
    return _bounded(value)


def _authority(record: EvidenceRecord) -> float:
    return {
        Authority.ENFORCED: 1.0,
        Authority.ASSERTED: 0.98,
        Authority.MEASURED: 0.82,
        Authority.WEAK: 0.35,
    }[record.authority]


def _coverage(record: EvidenceRecord) -> float:
    if record.authority in (Authority.ENFORCED, Authority.ASSERTED):
        return 1.0
    if record.scope.is_durable:
        return 1.0
    if record.scope.sampled:
        if record.scope.sample_fraction is not None:
            fraction = _bounded(record.scope.sample_fraction)
            return round(0.52 + 0.38 * math.sqrt(fraction), 4)
        if record.scope.rows_examined:
            return 0.64
        return 0.52
    # An observation with unknown extent is useful but cannot imply coverage.
    return 0.45


def _freshness(record: EvidenceRecord, now: datetime) -> float:
    captured = record.freshness.valid_as_of
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    age_days = max(0, (now - captured.astimezone(UTC)).days)
    if age_days <= 30:
        return 1.0
    if age_days <= 90:
        return 0.90
    if age_days <= 365:
        return 0.75
    return 0.55


def _consistency(records: list[EvidenceRecord]) -> float:
    warnings = sum(r.verdict is Verdict.PASSED_WITH_WARNING for r in records)
    if not warnings:
        return 1.0
    return max(0.55, 1.0 - warnings * 0.18)


def _weighted(factors: TrustFactors) -> float:
    return round(
        factors.evidence_directness * FACTOR_WEIGHTS.evidence_directness
        + factors.authority * FACTOR_WEIGHTS.authority
        + factors.coverage * FACTOR_WEIGHTS.coverage
        + factors.consistency * FACTOR_WEIGHTS.consistency
        + factors.freshness * FACTOR_WEIGHTS.freshness,
        2,
    )


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def assess(
    aspect: str,
    pairs: list[tuple[ClaimEvidence, EvidenceRecord]],
    *,
    now: datetime | None = None,
) -> TrustAssessment:
    """Build an inspectable trust assessment from evidence linked to a claim."""

    current = now or datetime.now(UTC)
    contradicting = [record for link, record in pairs if link.relationship is LinkKind.CONTRADICTS]
    supporting_pairs = [
        (link, record)
        for link, record in pairs
        if link.relationship is LinkKind.SUPPORTS and record.bears_on_claim
    ]

    limitations = _unique(
        [
            limitation
            for link, record in supporting_pairs
            for limitation in [*record.limitations, *link.does_not_support]
        ]
    )

    if not supporting_pairs:
        factors = TrustFactors(
            evidence_directness=0.20,
            authority=0.10,
            coverage=0.0,
            consistency=0.50,
            freshness=0.0,
        )
        state = Trust.CONTRADICTED if contradicting else Trust.UNSUPPORTED
        return TrustAssessment(
            state=state,
            confidence=min(_weighted(factors), STATE_CEILING[state]),
            factors=factors,
            reasons=[
                f"{len(contradicting)} contradicting observation(s) and no supporting evidence"
                if contradicting
                else "no supporting evidence"
            ],
            limitations=limitations,
        )

    links = [link for link, _ in supporting_pairs]
    records = [record for _, record in supporting_pairs]
    directness = max(_directness(aspect, link, record) for link, record in supporting_pairs)
    # Independent supporting observations add a small amount of robustness,
    # without allowing a pile of weak signals to imitate authority.
    directness = _bounded(directness + min(0.08, 0.03 * (len(records) - 1)))
    factors = TrustFactors(
        evidence_directness=round(directness, 4),
        authority=max(_authority(record) for record in records),
        coverage=max(_coverage(record) for record in records),
        consistency=0.0 if contradicting else _consistency(records),
        freshness=max(_freshness(record, current) for record in records),
    )

    best = max((_trust_from(record, aspect) for record in records), key=TRUST_RANK.__getitem__)
    authoritative = any(
        record.type in (EvidenceType.HUMAN_DECISION, EvidenceType.AUTHORITATIVE_ARTIFACT)
        for record in records
    )
    ceiling = ClaimPolicy.ceiling(aspect)
    reasons: list[str] = []
    if TRUST_RANK[best] > TRUST_RANK[ceiling] and not authoritative:
        reasons.append(
            f"state capped at {ceiling.value}: {aspect} needs an authoritative source, "
            "not only a data check"
        )
        best = ceiling

    state = Trust.CONTRADICTED if contradicting else best
    score = min(_weighted(factors), STATE_CEILING[state])

    if contradicting:
        reasons.insert(
            0,
            f"{len(contradicting)} contradicting observation(s) — unresolved, not averaged away",
        )
    if any(record.verdict is Verdict.PASSED_WITH_WARNING for record in records):
        reasons.append("a supporting check passed with a warning")

    asserted = [
        record
        for record in records
        if record.type in (EvidenceType.HUMAN_DECISION, EvidenceType.AUTHORITATIVE_ARTIFACT)
    ]
    scanned = [record for record in records if record.scope.is_durable]
    sampled = [record for record in records if record.scope.sampled]
    if asserted:
        reasons.append("asserted by someone with standing, not measured")
    elif scanned:
        rows = max((record.scope.rows_examined or 0) for record in scanned)
        reasons.append(f"complete scan over {rows:,} rows" if rows else "complete scan")
    elif sampled:
        rows = max((record.scope.rows_examined or 0) for record in sampled)
        reasons.append(f"sampled evidence over {rows:,} rows" if rows else "sampled evidence")
    else:
        reasons.append("evidence coverage was not reported")

    reasons.extend(
        [
            f"directness {factors.evidence_directness:.0%}",
            f"authority {factors.authority:.0%}",
            f"coverage {factors.coverage:.0%}",
            f"consistency {factors.consistency:.0%}",
            f"freshness {factors.freshness:.0%}",
        ]
    )

    # `links` is deliberately resolved above: retaining this local makes it
    # obvious that every factor came from a linked observation, not a global
    # pool of evidence that merely mentions the same table.
    assert links
    return TrustAssessment(
        state=state,
        confidence=round(score, 2),
        factors=factors,
        reasons=reasons,
        limitations=limitations,
    )


def evaluate(
    aspect: str, pairs: list[tuple[ClaimEvidence, EvidenceRecord]]
) -> tuple[Trust, float, list[str]]:
    """Compatibility tuple for callers that do not yet persist the assessment."""

    assessment = assess(aspect, pairs)
    return assessment.state, assessment.confidence, assessment.reasons
