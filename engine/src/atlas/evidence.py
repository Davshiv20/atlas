"""Evidence: reproducible observations with an explicit, falsifiable assertion.

Evidence is not "the SQL that ran". A query string proves a statement was
executed and nothing about what it establishes — which is how a `LIMIT 5` ended
up backing a grain claim at 0.92.

An evidence record states, before it runs, what result would make it fail. Then
it records what was observed, over what scope, and a verdict computed by policy
rather than asserted by a model. Three consequences follow:

* Records are **immutable and content-addressed**. A re-run mints a new id, so
  history is append-only and an id always denotes the same observation.
* Records are **claim-agnostic**. Evidence does not assert what it proves; the
  `ClaimEvidence` link does, and carries `does_not_support` so a reader can see
  the boundary of what was actually established.
* Records are **scoped**. A complete scan and a 1% sample are different facts,
  and collapsing them is how "no orphans found" becomes "referential integrity
  guaranteed".
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, computed_field


class EvidenceType(StrEnum):
    """Where the observation came from.

    Strength depends on the claim it is linked to, not on this alone: an
    enforced foreign key is excellent evidence for a relationship and weak
    evidence that a table means "customer".
    """

    DATABASE_CONSTRAINT = "database_constraint"
    COLUMN_PROFILE = "column_profile"
    DETERMINISTIC_CHECK = "deterministic_check"
    QUERY_USAGE_PATTERN = "query_usage_pattern"
    AUTHORITATIVE_ARTIFACT = "authoritative_artifact"
    HUMAN_DECISION = "human_decision"
    GOLDEN_QUESTION = "golden_question"
    # Recorded when useful, never counted as verification.
    INFERENCE_SIGNAL = "inference_signal"


class Authority(StrEnum):
    ENFORCED = "enforced"  # the database refuses to violate it
    MEASURED = "measured"  # observed over a stated scope
    ASSERTED = "asserted"  # someone with standing said so
    WEAK = "weak"  # a signal, not verification


class Verdict(StrEnum):
    PASSED = "passed"
    # The assertion held but a warning condition fired. An orphan rate of 0.04%
    # is not a clean foreign key and must not be reported as one.
    PASSED_WITH_WARNING = "passed_with_warning"
    FAILED = "failed"
    # There was no assertion to conclude about: a distribution reports what is
    # in the column and tests nothing. Distinct from INCONCLUSIVE, which means
    # an assertion existed and the run could not settle it (an empty table
    # satisfies every grain vacuously). Collapsing the two is what filed every
    # distribution as contradicting the claim it was run to inform.
    OBSERVED = "observed"
    INCONCLUSIVE = "inconclusive"


class Scope(BaseModel):
    """What the observation covers. Never implicit.

    `complete_scan` is the difference between "no counterexample exists" and
    "no counterexample was sampled". The first is durable; the second expires.
    """

    complete_scan: bool
    sampled: bool = False
    rows_examined: int | None = None
    sample_fraction: float | None = None
    filters: list[str] = Field(default_factory=list)

    @property
    def is_durable(self) -> bool:
        return self.complete_scan and not self.sampled


class Assertion(BaseModel):
    """What would make this fail, stated before it runs.

    Conditions are evaluated mechanically against the observation. A model does
    not get to decide whether its own hypothesis held.
    """

    description: str
    conditions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    warning_conditions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Execution(BaseModel):
    """Enough to reproduce the observation exactly."""

    database: str
    dialect: str
    sql: str
    snapshot_id: str | None = None

    @computed_field
    @property
    def query_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.sql.encode()).hexdigest()[:16]


class Freshness(BaseModel):
    valid_as_of: datetime
    # Schema or distribution changes that make this stale. Drift detection then
    # becomes a set intersection rather than a judgement call.
    invalidated_by: list[str] = Field(default_factory=list)


class Privacy(BaseModel):
    contains_raw_values: bool = False
    safe_for_external_model: bool = True


class EvidenceRecord(BaseModel):
    """One immutable observation."""

    type: EvidenceType
    authority: Authority
    subjects: list[str]
    assertion: Assertion
    observation: dict[str, Any] = Field(default_factory=dict)
    scope: Scope
    verdict: Verdict
    execution: Execution | None = None
    limitations: list[str] = Field(default_factory=list)
    # What the verdict turned on, in words: "24 of 26 rows have no match".
    # Derived from the observation, so it stays out of the content hash — but
    # without it a refuted hypothesis can only be reported as a raw dict.
    reasons: list[str] = Field(default_factory=list)
    freshness: Freshness
    privacy: Privacy = Field(default_factory=Privacy)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # The parameters the agent proposed. Stored so a claim linking to this can
    # be checked for agreement: a grain check run on the wrong key columns
    # passes, and without this there is no way to notice.
    hypothesis: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def id(self) -> str:
        """Content-addressed.

        The same check over the same data is the same record; any difference is
        a different record. A dated id would collide on a second run the same
        day and quietly break immutability.
        """
        material = json.dumps(
            {
                "type": self.type.value,
                "subjects": sorted(self.subjects),
                "assertion": self.assertion.model_dump(mode="json"),
                "observation": self.observation,
                "scope": self.scope.model_dump(mode="json"),
                "verdict": self.verdict.value,
                "hypothesis": self.hypothesis,
            },
            sort_keys=True,
            default=str,
        )
        return "evidence:" + hashlib.sha256(material.encode()).hexdigest()[:16]

    @property
    def primary_relation(self) -> str | None:
        """The relation this record is *about*.

        `subjects` lists every relation the check touched; the first is the one
        whose data was under test — for a join that is the source side, because
        "engagements.updated_by does not reference users" is a fact about
        `engagements`.
        """
        for subject in self.subjects:
            if subject.startswith("relation:"):
                return subject.removeprefix("relation:")
        return None

    @property
    def is_verification(self) -> bool:
        """Whether this *verifies* a claim: an assertion was stated and held.

        An inference signal never can, however confident it sounds. Neither can
        a failed or inconclusive check — those contradict or settle nothing,
        which the link records.
        """
        return self.type is not EvidenceType.INFERENCE_SIGNAL and self.verdict in (
            Verdict.PASSED,
            Verdict.PASSED_WITH_WARNING,
        )

    @property
    def is_observation(self) -> bool:
        """Whether this *observes* something a claim can rest on.

        A distribution cannot verify — it asserts nothing, so nothing held. It
        is still the right evidence for what a column contains, and treating it
        as worthless is what capped every column-meaning claim at the floor.
        """
        return (
            self.type is not EvidenceType.INFERENCE_SIGNAL
            and self.verdict is Verdict.OBSERVED
        )

    @property
    def bears_on_claim(self) -> bool:
        """Whether this can support a claim at all, by verifying or observing."""
        return self.is_verification or self.is_observation


class LinkKind(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"
    INCONCLUSIVE = "inconclusive"


class ClaimEvidence(BaseModel):
    """How one piece of evidence bears on one claim.

    Separate from the record because evidence must not assert what it proves.
    The same orphan check supports "invoices join customers" and says nothing
    about "customers are billable entities"; only the link knows which claim is
    in front of it.
    """

    claim_id: str
    evidence_id: str
    relationship: LinkKind
    rationale: str
    support_scope: list[str] = Field(default_factory=list)
    # The boundary. Without it a structural check silently reads as proof of
    # business meaning, which is the likeliest way this catalogue could lie.
    does_not_support: list[str] = Field(default_factory=list)


class EvidenceStore(BaseModel):
    records: list[EvidenceRecord] = Field(default_factory=list)
    links: list[ClaimEvidence] = Field(default_factory=list)

    def by_id(self, evidence_id: str) -> EvidenceRecord | None:
        return next((r for r in self.records if r.id == evidence_id), None)

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        """Append-only. An existing id means the identical observation, so
        re-adding is a no-op rather than a duplicate."""
        if self.by_id(record.id) is None:
            self.records.append(record)
        return record

    def link(self, link: ClaimEvidence) -> ClaimEvidence:
        self.links.append(link)
        return link

    def for_claim(self, claim_id: str) -> list[tuple[ClaimEvidence, EvidenceRecord]]:
        pairs = []
        for link in self.links:
            if link.claim_id != claim_id:
                continue
            record = self.by_id(link.evidence_id)
            if record is not None:
                pairs.append((link, record))
        return pairs

    def contradictions(self, claim_id: str) -> list[tuple[ClaimEvidence, EvidenceRecord]]:
        return [
            pair
            for pair in self.for_claim(claim_id)
            if pair[0].relationship is LinkKind.CONTRADICTS
        ]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json", exclude_none=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100))

    @classmethod
    def read(cls, path: Path) -> EvidenceStore:
        if not path.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})
