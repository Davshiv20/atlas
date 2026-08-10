"""Claims about the schema, with provenance and a review verdict.

Three invariants are enforced in the model rather than by convention, because
each is the kind of rule that quietly erodes once a deadline appears:

1. A claim whose only provenance is LLM inference cannot be marked verified and
   cannot exceed UNGROUNDED_CONFIDENCE_CEILING. Grounding or a human is the only
   route to full trust.
2. Regeneration supersedes, never overwrites. Human verdicts carry forward when
   the claim text is unchanged, and reset to unverified when it is not — so a
   reworded claim is re-reviewed instead of inheriting approval it never got.
3. A subject can hold several claims of an aspect that is naturally plural — a
   table joins to many tables and has many quality findings. Those aspects
   require a discriminator, so that two of them get two ids instead of the
   second silently replacing the first.
"""

from __future__ import annotations

import hashlib
import logging
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, computed_field, model_validator

from atlas.classify import Consequence

logger = logging.getLogger(__name__)

UNGROUNDED_CONFIDENCE_CEILING = 0.7

# Aspects that can legitimately hold more than one claim per subject. Without a
# discriminator these collide on id, and `merge` resolves the collision by
# keeping the last one — which is how four relationships ended up concatenated
# into a single prose claim rather than recorded as four.
PLURAL_ASPECTS = frozenset({"join", "quality", "metric", "lifecycle"})

# Discriminators become part of a URL path segment and of a stable identity, so
# they are constrained rather than free text.
DISCRIMINATOR = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ProvenanceKind(StrEnum):
    GROUNDED_CHECK = "grounded_check"
    LLM_INFERENCE = "llm_inference"
    HUMAN = "human"


class FactStatus(StrEnum):
    UNVERIFIED = "unverified"
    # Accepted without a human reading it: grounded, high-confidence, and about
    # a column whose meaning its shape already determines. Deliberately not
    # VERIFIED — nobody verified it, and an artifact that conflates the two is
    # lying to whoever reads it next.
    AUTO_ACCEPTED = "auto_accepted"
    VERIFIED = "verified"
    REJECTED = "rejected"


class Provenance(BaseModel):
    kind: ProvenanceKind
    detail: str
    result: Literal["pass", "fail", "inconclusive"] | None = None


class Fact(BaseModel):
    subject: str  # "conversations" or "conversations.created_at"
    aspect: str  # "grain" | "semantics" | "join" | "metric" | ...
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: list[Provenance] = Field(min_length=1)
    status: FactStatus = FactStatus.UNVERIFIED
    verified_by: str | None = None
    supersedes: str | None = None

    # What distinguishes this claim from its siblings under the same subject and
    # aspect: the target table for a join, a slug naming the finding for a
    # quality issue. Required for plural aspects, forbidden for singular ones —
    # a table has exactly one grain, and allowing two ids for it would let a
    # contradiction sit in the store unnoticed.
    discriminator: str | None = None

    # Set deterministically from the column's class at creation, never chosen by
    # the model. Drives review order and what "validated" means for a table.
    consequence: Consequence = Consequence.HIGH


    @computed_field  # serialized: clients address a claim by id, not by convention
    @property
    def id(self) -> str:
        if self.discriminator is None:
            return f"{self.subject}#{self.aspect}"
        return f"{self.subject}#{self.aspect}#{self.discriminator}"

    @property
    def claim_hash(self) -> str:
        return hashlib.sha256(self.claim.encode()).hexdigest()[:12]

    @property
    def is_grounded(self) -> bool:
        """Grounded means something that could have falsified the claim did not.
        Profile data is an *input* to inference, not a test of it, so it does
        not count here — only an executed check or a human does."""
        return any(
            p.kind in (ProvenanceKind.GROUNDED_CHECK, ProvenanceKind.HUMAN)
            for p in self.provenance
        )

    @model_validator(mode="after")
    def enforce_discriminator_rules(self) -> Self:
        if self.aspect in PLURAL_ASPECTS and self.discriminator is None:
            raise ValueError(
                f"{self.subject}#{self.aspect}: a {self.aspect} claim needs a discriminator "
                f"(for a join, the table it joins to). Without one, a second {self.aspect} "
                f"claim about {self.subject!r} would replace this one instead of joining it."
            )
        if self.aspect not in PLURAL_ASPECTS and self.discriminator is not None:
            raise ValueError(
                f"{self.subject}#{self.aspect}: {self.aspect} holds one claim per subject, "
                f"so it takes no discriminator. Two would let a contradiction sit unnoticed."
            )
        if self.discriminator is not None and not DISCRIMINATOR.match(self.discriminator):
            raise ValueError(
                f"invalid discriminator {self.discriminator!r}: lowercase alphanumeric with "
                f". _ - only, max 64 characters"
            )
        return self

    @model_validator(mode="after")
    def enforce_grounding_rules(self) -> Self:
        if self.is_grounded:
            return self
        if self.confidence > UNGROUNDED_CONFIDENCE_CEILING:
            raise ValueError(
                f"{self.id}: ungrounded claim cannot exceed confidence "
                f"{UNGROUNDED_CONFIDENCE_CEILING} (got {self.confidence})"
            )
        if self.status in (FactStatus.VERIFIED, FactStatus.AUTO_ACCEPTED):
            raise ValueError(
                f"{self.id}: ungrounded claim cannot be marked {self.status.value}"
            )
        return self


class FactStore(BaseModel):
    facts: list[Fact] = Field(default_factory=list)

    def by_id(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    def merge(self, incoming: list[Fact]) -> FactStore:
        """Fold a regenerated batch into the store, preserving human verdicts."""
        merged = {fact.id: fact for fact in self.facts}
        for fact in incoming:
            existing = merged.get(fact.id)
            merged[fact.id] = fact if existing is None else _carry_verdict(existing, fact)
        return FactStore(facts=sorted(merged.values(), key=lambda f: f.id))

    def needing_review(self) -> list[Fact]:
        return [f for f in self.facts if f.status is FactStatus.UNVERIFIED]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json", exclude_none=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))

    @classmethod
    def read(cls, path: Path) -> FactStore:
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate({**raw, "facts": [_migrate(f) for f in raw.get("facts", [])]})


def _migrate(raw: dict) -> dict:
    """Give a pre-discriminator claim one, so old catalogues still load.

    Derived from the claim text, which makes it stable across reads — the same
    file parses to the same ids every time. It is deliberately ugly: a
    `legacy-` prefix in an id is a visible marker that this claim predates the
    plural-aspect rule and may be several findings concatenated into one.
    """
    if raw.get("aspect") not in PLURAL_ASPECTS or raw.get("discriminator") is not None:
        return raw
    digest = hashlib.sha256(str(raw.get("claim", "")).encode()).hexdigest()[:8]
    logger.info(
        "migrating %s#%s: assigning discriminator legacy-%s",
        raw.get("subject"),
        raw.get("aspect"),
        digest,
    )
    return {**raw, "discriminator": f"legacy-{digest}"}


def _carry_verdict(existing: Fact, incoming: Fact) -> Fact:
    """Keep the human's decision only if the claim they judged is still the
    claim being made."""
    if existing.claim_hash != incoming.claim_hash:
        return incoming.model_copy(update={"supersedes": f"{existing.id}@{existing.claim_hash}"})

    if existing.status is not FactStatus.VERIFIED:
        return incoming.model_copy(update={"status": existing.status})

    # A past human verdict is itself provenance; recording it is what keeps the
    # carried-over `verified` status legal under the grounding invariant.
    verdict = Provenance(
        kind=ProvenanceKind.HUMAN,
        detail=f"verified by {existing.verified_by or 'unknown reviewer'}",
        result="pass",
    )
    payload = incoming.model_dump()
    payload.update(
        status=FactStatus.VERIFIED,
        verified_by=existing.verified_by,
        confidence=max(existing.confidence, incoming.confidence),
        provenance=[*incoming.provenance, verdict],
    )
    return Fact.model_validate(payload)
