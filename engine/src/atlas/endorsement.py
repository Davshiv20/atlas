"""Where a claim stands with a human.

The mirror of `policy.assess`. Trust asks what the evidence establishes;
endorsement asks what a person decided, and whether their decision still rests
on something true.

Both are derived from the same input — the evidence linked to a claim — and
neither is stored. A stored verdict is what let `status: verified` and
`trust.state: contradicted` sit on one claim and disagree, because only one of
them was ever recomputed.

Staleness falls out of content addressing rather than needing a clock. An
`EvidenceRecord` id is a hash of what was observed, so a re-run that sees
something different *is* a different record. An endorsement records the ids it
was decided against; when those ids are no longer among the claim's evidence,
the ground the decision stood on has moved.

See `APPROVAL.md` for the model this implements.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from atlas.evidence import (
    Assertion,
    Authority,
    ClaimEvidence,
    EvidenceRecord,
    EvidenceType,
    Freshness,
    LinkKind,
    Scope,
    Verdict,
)

# What the reviewer was looking at, recorded on the decision so a later read can
# tell whether it still holds. Part of the record's content, so two decisions
# taken against different evidence are two different records.
SAW = "saw"
# Whether the person wrote the meaning or agreed with the one proposed.
SPECIFICITY = "specificity"


class Endorsement(StrEnum):
    """Answers: where does this claim stand with a human?"""

    NONE = "none"
    """Nobody has judged it."""

    AUTO = "auto"
    """Policy accepted it. No person was involved, and none is implied."""

    ENDORSED = "endorsed"
    """A person confirmed the meaning the model proposed."""

    AUTHORED = "authored"
    """A person supplied the meaning themselves — answered, or edited and confirmed."""

    DISPUTED = "disputed"
    """A person asserted against it."""

    STALE = "stale"
    """Backed by a person, but the evidence they decided against has changed."""


BACKED = (Endorsement.ENDORSED, Endorsement.AUTHORED)


class EndorsementFactors(BaseModel):
    """Why the state is what it is.

    Deliberately not averaged into a score. Trust is continuous because evidence
    varies in strength; a named person either backed the claim or did not, and a
    weighted mean over "standing" and "corroboration" would be a number nobody
    can act on. These qualify the state; they never replace it.
    """

    standing: list[str] = Field(default_factory=list)
    """Who decided. Self-declared until Atlas has authentication."""

    specificity: str | None = None
    """`authored` or `endorsed` — writing meaning is a stronger act than agreeing."""

    scope: list[str] = Field(default_factory=list)
    """The evidence ids the decision was taken against."""

    currency: float = 1.0
    """Share of that evidence still standing. Below 1.0 the decision is stale."""

    corroboration: int = 0
    """Distinct people who backed it."""


class EndorsementAssessment(BaseModel):
    state: Endorsement
    factors: EndorsementFactors
    reasons: list[str] = Field(default_factory=list)
    decided_at: datetime | None = None


def endorsement(
    pairs: list[tuple[ClaimEvidence, EvidenceRecord]],
    *,
    auto_accepted: bool = False,
) -> EndorsementAssessment:
    """Derive where a claim stands with a human, from its evidence."""
    decisions = [
        (link, record)
        for link, record in pairs
        if record.type is EvidenceType.HUMAN_DECISION
    ]

    if not decisions:
        state = Endorsement.AUTO if auto_accepted else Endorsement.NONE
        return EndorsementAssessment(
            state=state,
            factors=EndorsementFactors(),
            reasons=[
                "accepted by policy without human review"
                if auto_accepted
                else "nobody has judged this claim"
            ],
        )

    # The most recent decision wins. An earlier reviewer is not overruled
    # silently — their record stays linked and readable — but the claim reflects
    # the last person to put their name to it.
    decisions.sort(key=lambda pair: pair[1].captured_at)
    link, record = decisions[-1]

    who = _unique([name for _, r in decisions for name in _deciders(r)])
    backers = _unique(
        [
            name
            for lk, r in decisions
            if lk.relationship is LinkKind.SUPPORTS
            for name in _deciders(r)
        ]
    )

    if link.relationship is LinkKind.CONTRADICTS or record.verdict is Verdict.FAILED:
        return EndorsementAssessment(
            state=Endorsement.DISPUTED,
            factors=EndorsementFactors(
                standing=who,
                specificity=record.observation.get(SPECIFICITY),
                scope=list(record.observation.get(SAW, [])),
                corroboration=len(backers),
            ),
            reasons=[f"disputed by {', '.join(_deciders(record)) or 'a reviewer'}"],
            decided_at=record.captured_at,
        )

    saw = [str(item) for item in record.observation.get(SAW, [])]
    present = {r.id for _, r in pairs}
    still_standing = [item for item in saw if item in present]
    currency = 1.0 if not saw else len(still_standing) / len(saw)

    specificity = record.observation.get(SPECIFICITY)
    state = Endorsement.AUTHORED if specificity == "authored" else Endorsement.ENDORSED
    reasons = [f"{state.value} by {', '.join(_deciders(record)) or 'a reviewer'}"]

    if currency < 1.0:
        moved = len(saw) - len(still_standing)
        state = Endorsement.STALE
        reasons.append(
            f"{moved} of {len(saw)} observation(s) it was decided against have changed"
        )

    return EndorsementAssessment(
        state=state,
        factors=EndorsementFactors(
            standing=who,
            specificity=specificity,
            scope=saw,
            currency=round(currency, 4),
            corroboration=len(backers),
        ),
        reasons=reasons,
        decided_at=record.captured_at,
    )


def as_decision_record(
    *,
    subject: str,
    reviewer: str,
    statement: str,
    supports: bool,
    saw: list[str],
    authored: bool,
    at: datetime | None = None,
) -> EvidenceRecord:
    """A person's decision, shaped like every other observation.

    `saw` is the point of the record. Without it an endorsement asserts currency
    it cannot know it still has — which is the failure this whole model exists
    to remove.
    """
    when = at or datetime.now(UTC)
    verb = "authored" if authored else "endorsed"
    return EvidenceRecord(
        type=EvidenceType.HUMAN_DECISION,
        authority=Authority.ASSERTED,
        subjects=[subject],
        assertion=Assertion(
            description=f"{reviewer} {verb if supports else 'disputed'}: {statement.strip()}"
        ),
        observation={
            "statement": statement,
            SPECIFICITY: "authored" if authored else "endorsed",
            SAW: sorted(saw),
        },
        # A decision is not a measurement. Claiming a scan would misrepresent
        # where the authority comes from.
        scope=Scope(complete_scan=False),
        verdict=Verdict.PASSED if supports else Verdict.FAILED,
        reasons=[f"{verb if supports else 'disputed'} by {reviewer}"],
        limitations=[
            "Rests on the reviewer's standing, not on the data.",
            "A later observation can contradict it, and should be recorded when it does.",
        ],
        freshness=Freshness(
            valid_as_of=when,
            invalidated_by=[f"schema_change:{subject}"],
        ),
        hypothesis={"check_type": "human_decision", "reviewer": reviewer},
    )


def from_legacy_status(status: str, reviewer: str) -> EndorsementAssessment:
    """A decision taken before decisions were recorded as evidence.

    Existing workspaces carry `status` and `verified_by` and nothing else. Read
    strictly, those claims have no human-decision evidence and would derive as
    `none` — silently un-approving every review anyone has already done. They
    are projected instead, with the scope recorded as empty because we genuinely
    do not know what those reviewers were shown. That is not a gap to paper
    over: an approval whose grounds are unknown cannot be told it has gone
    stale, and saying so is more useful than inventing a basis for it.
    """
    state = Endorsement.DISPUTED if status == "rejected" else Endorsement.ENDORSED
    return EndorsementAssessment(
        state=state,
        factors=EndorsementFactors(standing=[reviewer], corroboration=1),
        reasons=[
            (
                f"{state.value} by {reviewer} before decisions were recorded as "
                "evidence; the observations it rested on are unknown, so it "
                "cannot go stale"
            )
        ],
    )


def _deciders(record: EvidenceRecord) -> list[str]:
    who = record.hypothesis.get("reviewer")
    return [str(who)] if who else []


def _unique(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def observation_ids(pairs: list[tuple[ClaimEvidence, EvidenceRecord]]) -> list[str]:
    """What a reviewer is being shown right now, for recording on their decision.

    Human decisions are excluded: endorsing a claim is not agreeing with the
    last person who endorsed it, and counting their record as part of your
    grounds would make every endorsement stale the moment anyone re-endorsed.
    """
    return sorted(
        record.id
        for _, record in pairs
        if record.type is not EvidenceType.HUMAN_DECISION
    )


def projected_status(state: Endorsement) -> str:
    """`FactStatus` as a projection, for clients that still read it."""
    mapping: dict[Endorsement, str] = {
        Endorsement.NONE: "unverified",
        Endorsement.STALE: "unverified",
        Endorsement.AUTO: "auto_accepted",
        Endorsement.ENDORSED: "verified",
        Endorsement.AUTHORED: "verified",
        Endorsement.DISPUTED: "rejected",
    }
    return mapping[state]
