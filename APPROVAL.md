# Approval model

Status: **partially implemented**. Human review is recorded as evidence by `engine/src/atlas/decisions.py`, and `engine/src/atlas/output.py` derives endorsement on read through `engine/src/atlas/endorsement.py`. Legacy reviews are projected on read. Refresh/regeneration does not yet preserve prior review evidence, so automatic stale-endorsement transitions across a new snapshot remain future work. This document records both the implemented model and that remaining integration. Read [`PRODUCT.md`](PRODUCT.md) for the product loop and [`CLAUDE.md`](CLAUDE.md) for the governing invariants.

## What endorsement is

Endorsement answers one question: **where does this claim stand with a human?**

The legacy implementation answered that with a word stored on the claim — `verified`, `unverified`, or `rejected` — written once and never reconsidered. The implemented model derives the answer from what people actually did.

Concretely. On Monday, Atlas proposes *"email is the login identity for a
user"* and a reviewer confirms it.

- The legacy model wrote `status: verified, verified_by: alex`. That was the entire memory of the event.
- The implemented model writes a record: *Alex confirmed this on Monday, having seen these sample values and this uniqueness check.*

The target behavior is that when the uniqueness check later re-runs and fails, the approval derives as **stale** because the evidence it rested on moved. The endorsement derivation supports that comparison, but the current refresh/regeneration path resets semantic state instead of carrying prior review evidence into the new generation. End-to-end stale transitions therefore still require refresh integration.

The implemented improvement is that the system remembers *why* a claim was approved rather than only *that* it was. Preserving that record across generations is the remaining step needed to notice automatically when the reason stops holding.

Everything below is the mechanics of that.

## Why this exists

Trust stopped being a stored scalar and became something derived. `assess()`
recomputes it from linked evidence on every read, reports five inspectable
factors, separates *what established the claim* (`Trust`) from *how strongly*
(`confidence`) from *how to say it* (`TrustBand`), is bounded by
`ClaimPolicy.ceiling`, and treats contradiction as a first-class state rather
than as an absence of support.

Before endorsement was implemented, approval remained `FactStatus` — a flat enum written onto the `Fact`, whose entire record of who decided and why was `verified_by: str | None`.

That made trust and approval inconsistent by construction. `assess_facts` (`engine/src/atlas/output.py`) recomputed every claim's trust on read without revisiting the stored status, so a claim confirmed months earlier could be emitted as:

```yaml
status:      verified        # frozen, from a human, months ago
trust:
  state:     contradicted    # live, from evidence, today
```

Nothing reconciles those. A consumer reading the emitted view has no way to know
which one to believe, and product invariant 4 — ungrounded claims cannot be
presented as verified — is being enforced against one of them and not the other.

## The principle

**A human decision is evidence.**

This is not a new idea in the codebase; it is already implemented once.
`record_answer` (`engine/src/atlas/answers.py`) turns an answered question into
an `EvidenceRecord` with `EvidenceType.HUMAN_DECISION` and
`Authority.ASSERTED`, links it to the claim, and lets the claim be re-scored
like any other. Its module docstring explains why: assertion is the one kind of
evidence that can settle meaning, because no query establishes what a column
means to an organisation.

Approval should be the same act. Confirming a claim records a human attestation;
"approved" is then *derived* from the attestations that exist, exactly as trust
is derived from the observations that exist.

## Three verbs, one mechanism

| Verb | Record | Link | Means |
|---|---|---|---|
| **answer** | `HUMAN_DECISION` · `ASSERTED` · `PASSED` | `SUPPORTS` | a person supplied the meaning |
| **APPROVE** | `HUMAN_DECISION` · `ASSERTED` · `PASSED` | `SUPPORTS` | a person approved the proposed meaning |
| **REJECT** | `HUMAN_DECISION` · `ASSERTED` · `FAILED` | `CONTRADICTS` | a person asserted against the claim |

The vocabulary already exists — `EvidenceType.HUMAN_DECISION`, `Authority.ASSERTED`, and `LinkKind.CONTRADICTS` are defined in `engine/src/atlas/evidence.py`. `record_answer` writes the first form through the question-answer path; `record_decision` writes approvals and rejections through the shared decision-record constructor.

This generalizes the existing evidence model rather than replacing it.

## Endorsement states

Derived by an `endorsement()` function taking the same
`list[tuple[ClaimEvidence, EvidenceRecord]]` that `assess()` already takes.

| State | Derivation |
|---|---|
| `NONE` | no human-decision evidence links to this claim |
| `AUTO` | policy accepted it without a person: grounded, high confidence, routine consequence |
| `APPROVED` | a person confirmed meaning the model proposed |
| `AUTHORED` | a person supplied the meaning themselves (answered, or edited then confirmed) |
| `REJECTED` | a person asserted against it |
| `STALE` | approved or authored, but the evidence it was decided against has since changed |

`AUTHORED` and `APPROVED` are kept apart because they carry different weight. A
person writing the meaning is a stronger claim on their attention than a person
agreeing with a sentence already on screen.

## Factors — reasons, not weights

`TrustAssessment` carries a 0–1 `confidence` because trust is genuinely
continuous: evidence varies in directness, coverage and authority, and a
weighted mean over those means something.

Endorsement is discrete. A named person either endorsed the claim or did not.
Averaging `standing 0.8` with `corroboration 0.3` into `0.55` produces a number
no reviewer can act on and manufactures precisely the false precision invariant 4
warns about. So the factors are recorded and reported, and never averaged.

| Factor | Records |
|---|---|
| `standing` | who decided, and on what authority |
| `specificity` | authored the meaning, or approved a proposal |
| `scope` | what they were shown at the moment they decided |
| `currency` | whether the evidence underneath has moved since |
| `corroboration` | one reviewer, or several independently |

`currency` is the one continuous quantity worth keeping, because it ranks what
most needs re-looking. It does not score the judgment; it scores the distance
between what was reviewed and what is true now.

## Decay

An endorsement records the evidence it was made against — the record ids and their content hashes. The derivation returns `STALE` when those records are replaced by different observations in the evidence set.

Current refresh and regeneration reset semantic state, so production does not yet carry old endorsements forward to exercise that transition automatically. The intended integration is evidence-based rather than clock-based: unchanged evidence leaves an endorsement standing, while changed evidence scopes re-review.

## Rejection

A rejection is human evidence *against* the claim, not a delete. The claim
survives with a `CONTRADICTS` link attached, which is what makes it auditable
and what lets a later reviewer overturn it by recording their own attestation.
This satisfies invariant 2 (claims are attributable, reviewable, and versioned)
in a way a terminal status flag cannot.

## Migration from FactStatus

`FactStatus` stays on the wire as a **derived projection**, the way `confidence`
is currently duplicated onto `Fact` for ordering and backwards compatibility. It
stops being a source of truth.

```
NONE, STALE          → unverified
AUTO                 → auto_accepted
APPROVED, AUTHORED   → verified
REJECTED             → rejected
```

Existing workspaces are not backfilled. When a fact has `verified_by` but no human-decision record, `assess_facts` projects a legacy endorsement on read with unknown scope. This preserves the visible reviewer and status without inventing evidence, but that legacy approval cannot derive staleness. A durable backfill remains optional future migration work.

## API surface

`POST /workspaces/{ws}/claims/{id}/review` kept its request shape. Underneath:

- it writes an evidence record and a link instead of treating a status field as the source of truth;
- **it no longer returns the former grounding 409.** That refusal existed because
  `enforce_grounding_rules` forbids `verified` on an ungrounded claim. Under this
  model, endorsing *is* grounding — `is_verification` is already true for a
  `HUMAN_DECISION` with a `PASSED` verdict — so the condition the 409 protects
  against is no longer reachable;
- subsequent output reads carry the derived `EndorsementAssessment` alongside the existing `TrustAssessment`; the review response itself remains the updated `Fact` for compatibility.

`POST /questions/{id}/answer` continues to record human-decision evidence through its sibling answer path.

## What this costs

The removed 409 did real work: it stopped a confident guess from being promoted to fact by a distracted reviewer. Allowing human assertion therefore required replacement protections rather than simply dropping the refusal:

1. **The states stay distinguishable.** A claim grounded by a passing check
   derives `Trust.VERIFIED`; a claim grounded only by a person derives
   `Trust.AUTHORITATIVE` via `Authority.ASSERTED`. The emitted view can and must
   say which — "a check tested this" and "a named person asserted this" are
   different promises to an agent.
2. **The reviewer must be told before acting**, not after. The interface must show when no database check established the claim at the moment the decision is offered.
3. **`ClaimPolicy.ceiling` still binds.** Human assertion lifts a business claim
   past `OBSERVED`; it must not reach `ENFORCED`, which is a property of the
   database and not of anyone's opinion.

## What this fixes

- The `verified` / `contradicted` divergence, structurally — both derive from
  the same records at the same time.
- The 409 on ungrounded claims, and the dead-end it creates for exactly the
  business claims that most need review.
- Approval that asserts currency it does not have.
- "I approved everything and it still shows unapproved" — approval becomes a set
  of records you can enumerate and display, not a flag that may or may not have
  been written.
- `auto_accepted` stops being a status that looks like a weaker `verified` and
  becomes what it is: a derivation with no human in it.

## What this does not decide

- Whether endorsement is per-claim only, or whether a table or workspace can
  carry its own attestation.
- What `standing` means before Atlas has authentication. Today `reviewer` is a
  self-declared string.
- Whether a bulk review endpoint should exist, or whether a staged console
  review submits N requests.
- Any console design. The UI questions are downstream of this document.
