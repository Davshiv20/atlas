"""Trust and confidence, computed from evidence rather than asserted.

    Trust = claim-type policy
          + evidence types
          + scope
          + deterministic verdicts
          + contradictions
          + freshness

Two numbers come out, and they answer different questions:

`trust` is the discrete authority ladder — what kind of thing established this.
`confidence` is a score for ordering a review queue and for showing a reader how
far along that ladder a claim sits.

Contradictions are deliberately **not** averaged into the score. A claim with
strong support and one contradicting observation is not "somewhat confident";
it is unresolved, and the score is reported beside that fact rather than
absorbing it. Averaging is what lets a real conflict disappear into a 0.6.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from atlas.evidence import (
    Authority,
    ClaimEvidence,
    EvidenceRecord,
    EvidenceType,
    LinkKind,
    Verdict,
)


class Trust(StrEnum):
    """What established the claim. Ordered weakest to strongest."""

    UNSUPPORTED = "unsupported"  # nothing but inference
    SIGNAL = "signal"  # naming, shape, model interpretation
    OBSERVED = "observed"  # a profile or a sampled check
    VERIFIED = "verified"  # a complete deterministic check
    ENFORCED = "enforced"  # the database guarantees it
    AUTHORITATIVE = "authoritative"  # a domain owner with standing said so
    CONTRADICTED = "contradicted"  # conflicting evidence, unresolved


TRUST_SCORE: dict[Trust, float] = {
    Trust.CONTRADICTED: 0.10,
    Trust.UNSUPPORTED: 0.20,
    Trust.SIGNAL: 0.40,
    Trust.OBSERVED: 0.65,
    Trust.VERIFIED: 0.88,
    Trust.ENFORCED: 0.97,
    Trust.AUTHORITATIVE: 0.99,
}

# A warning verdict is a real result, not a clean one: an orphan rate of 0.04%
# means the join works and is not a guaranteed foreign key.
WARNING_PENALTY = 0.10


class ClaimPolicy:
    """What a claim of a given type requires before it can be trusted.

    Structural claims and business claims have different ladders. An enforced
    foreign key is the strongest possible evidence for a relationship and near
    worthless for "this table represents a customer" — so one universal
    hierarchy would be wrong for half of all claims.
    """

    BUSINESS: ClassVar[frozenset[str]] = frozenset({"semantics", "metric", "unit", "lifecycle"})

    @staticmethod
    def requires_complete_scan(aspect: str) -> bool:
        # A grain established from a sample is not established. Everything else
        # can degrade to OBSERVED and say so.
        return aspect == "grain"

    @staticmethod
    def ceiling(aspect: str) -> Trust:
        """The most a claim of this type can reach from evidence alone.

        Business meaning cannot be verified by querying data at any scope. A
        distribution can rule interpretations out; it cannot establish what a
        column means to the organisation. Only an authoritative artifact or a
        human with standing lifts a business claim past OBSERVED.
        """
        return Trust.OBSERVED if aspect in ClaimPolicy.BUSINESS else Trust.ENFORCED


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
        # It observed; it did not test. A complete scan of a column's values
        # still establishes only what is there, never what it means.
        return Trust.OBSERVED

    if record.type is EvidenceType.DETERMINISTIC_CHECK:
        if not record.scope.is_durable:
            # Sampled evidence is a real observation with an expiry date, not a
            # verification. This is the honest answer for warehouse tables where
            # a complete scan is not affordable.
            return Trust.OBSERVED
        if ClaimPolicy.requires_complete_scan(aspect) and not record.scope.complete_scan:
            return Trust.OBSERVED
        return Trust.VERIFIED

    return Trust.OBSERVED


def evaluate(
    aspect: str, pairs: list[tuple[ClaimEvidence, EvidenceRecord]]
) -> tuple[Trust, float, list[str]]:
    """Return (trust, confidence, reasons) for a claim.

    `reasons` is what a reviewer reads instead of a bare number — the score is
    only defensible if the reader can see what produced it.
    """
    reasons: list[str] = []

    contradicting = [r for link, r in pairs if link.relationship is LinkKind.CONTRADICTS]
    if contradicting:
        reasons.append(
            f"{len(contradicting)} contradicting observation(s) — unresolved, not averaged away"
        )
        return Trust.CONTRADICTED, TRUST_SCORE[Trust.CONTRADICTED], reasons

    # `bears_on_claim`, not `is_verification`: a distribution asserts nothing,
    # so nothing held, but it is still the right evidence for what a column
    # contains. Requiring verification here made `_trust_from`'s COLUMN_PROFILE
    # branch unreachable and floored every claim resting on an observation.
    supporting = [
        record
        for link, record in pairs
        if link.relationship is LinkKind.SUPPORTS and record.bears_on_claim
    ]
    if not supporting:
        reasons.append("no supporting evidence")
        return Trust.UNSUPPORTED, TRUST_SCORE[Trust.UNSUPPORTED], reasons

    best = max((_trust_from(r, aspect) for r in supporting), key=lambda t: TRUST_SCORE[t])

    ceiling = ClaimPolicy.ceiling(aspect)
    if TRUST_SCORE[best] > TRUST_SCORE[ceiling] and not any(
        r.type in (EvidenceType.HUMAN_DECISION, EvidenceType.AUTHORITATIVE_ARTIFACT)
        for r in supporting
    ):
        reasons.append(
            f"capped at {ceiling.value}: a {aspect} claim needs an authoritative source, "
            f"not a data check"
        )
        best = ceiling

    score = TRUST_SCORE[best]
    if any(r.verdict is Verdict.PASSED_WITH_WARNING for r in supporting):
        score = round(score - WARNING_PENALTY, 2)
        reasons.append("a supporting check passed with a warning")

    asserted = [
        r
        for r in supporting
        if r.type in (EvidenceType.HUMAN_DECISION, EvidenceType.AUTHORITATIVE_ARTIFACT)
    ]
    scanned = [r for r in supporting if r.scope.is_durable]
    if asserted:
        # Scope language does not apply to a decision. "Sampled only — expires
        # as data changes" is wrong about a person's answer in both halves.
        reasons.append("asserted by someone with standing, not measured")
    elif scanned:
        rows = max((r.scope.rows_examined or 0) for r in scanned)
        reasons.append(f"complete scan over {rows:,} rows" if rows else "complete scan")
    else:
        reasons.append("sampled only — expires as data changes")

    return best, score, reasons
