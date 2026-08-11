# Approval model

Status: **proposal**. Nothing here is implemented. Read
[`PRODUCT.md`](PRODUCT.md) for the product loop and [`CLAUDE.md`](CLAUDE.md) for
the invariants this has to satisfy.

## What endorsement is

Endorsement answers one question: **where does this claim stand with a human?**

Today that is answered by a word stored on the claim — `verified`,
`unverified`, `rejected` — written once when a reviewer presses a key, and never
looked at again. This proposal stops storing the word and works the answer out
from what people actually did.

Concretely. On Monday, Atlas proposes *"email is the login identity for a
user"* and a reviewer confirms it.

- Today, that writes `status: verified, verified_by: shivam`. That is the entire
  memory of the event.
- Under this proposal it writes a record: *Shivam confirmed this on Monday,
  having seen these sample values and this uniqueness check.*

On Thursday the uniqueness check re-runs and fails — three hundred duplicate
addresses have appeared.

- Today the claim still reads `verified`. Nothing revisits it, and an agent
  consuming the view is told a person approved something that is now false.
- Under this proposal the record still says the approval rested on a check that
  has since failed, so the claim derives as **stale** on the next read, with no
  one having to notice.

Same keystroke from the reviewer. The difference is that the system remembers
*why* the claim was approved rather than only *that* it was, which is what lets
it notice when the reason stops holding.

Everything below is the mechanics of that.

## Why this exists

Trust stopped being a stored scalar and became something derived. `assess()`
recomputes it from linked evidence on every read, reports five inspectable
factors, separates *what established the claim* (`Trust`) from *how strongly*
(`confidence`) from *how to say it* (`TrustBand`), is bounded by
`ClaimPolicy.ceiling`, and treats contradiction as a first-class state rather
than as an absence of support.

Approval did not move. It is still `FactStatus` — a flat enum written onto the
`Fact`, whose entire record of who decided and why is `verified_by: str | None`.

The two are now inconsistent by construction. `assess_facts`
(`engine/src/atlas/output.py`) recomputes every claim's trust on read and never
touches `status`, so a claim confirmed months ago, against which a check has
since run and failed, is emitted as:

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
| **endorse** | `HUMAN_DECISION` · `ASSERTED` · `PASSED` | `SUPPORTS` | a person confirmed the proposed meaning |
| **dispute** | `HUMAN_DECISION` · `ASSERTED` · `FAILED` | `CONTRADICTS` | a person asserted against the claim |

The vocabulary already exists — `EvidenceType.HUMAN_DECISION`,
`Authority.ASSERTED`, `LinkKind.CONTRADICTS` are all defined in
`engine/src/atlas/evidence.py`. `record_answer` already writes the first row.
The other two are the same function with a different assertion and verdict.

This is a generalization of existing code, not a rewrite.

## Endorsement states

Derived by an `endorsement()` function taking the same
`list[tuple[ClaimEvidence, EvidenceRecord]]` that `assess()` already takes.

| State | Derivation |
|---|---|
| `NONE` | no human-decision evidence links to this claim |
| `AUTO` | policy accepted it without a person: grounded, high confidence, routine consequence |
| `ENDORSED` | a person confirmed meaning the model proposed |
| `AUTHORED` | a person supplied the meaning themselves (answered, or edited then confirmed) |
| `DISPUTED` | a person asserted against it |
| `STALE` | endorsed or authored, but the evidence it was decided against has since changed |

`AUTHORED` and `ENDORSED` are kept apart because they carry different weight. A
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
| `specificity` | authored the meaning, or endorsed a proposal |
| `scope` | what they were shown at the moment they decided |
| `currency` | whether the evidence underneath has moved since |
| `corroboration` | one reviewer, or several independently |

`currency` is the one continuous quantity worth keeping, because it ranks what
most needs re-looking. It does not score the judgment; it scores the distance
between what was reviewed and what is true now.

## Decay

An endorsement records the evidence it was made against — the record ids, and
their content hashes. When those records are superseded by a re-run whose
observations differ, the endorsement derives as `STALE`.

This is the same treatment `freshness` already gets inside `assess()`. A
sampled observation expires because the rows it saw may have changed; a human
endorsement of a claim about those rows expires for the same reason.

Decay is **not** a clock. An endorsement of a claim whose evidence has not moved
stands indefinitely. Time alone does not unseat a person's judgment about
meaning — a changed world does.

## Rejection

A dispute is human evidence *against* the claim, not a delete. The claim
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
ENDORSED, AUTHORED   → verified
DISPUTED             → rejected
```

Existing workspaces need a one-time backfill: for every fact carrying a
`verified_by`, synthesize a `HUMAN_DECISION` record attributed to that reviewer,
with the scope marked unknown and `currency` unresolvable. That is honest — we
genuinely do not know what those reviewers were looking at — and it preserves
the review history rather than discarding it.

## API surface

`POST /workspaces/{ws}/claims/{id}/review` keeps its request shape. What changes
is underneath:

- it writes an evidence record and a link instead of mutating a status field;
- **it stops returning 409.** The refusal exists today because
  `enforce_grounding_rules` forbids `verified` on an ungrounded claim. Under this
  model, endorsing *is* grounding — `is_verification` is already true for a
  `HUMAN_DECISION` with a `PASSED` verdict — so the condition the 409 protects
  against is no longer reachable;
- the response carries the derived `EndorsementAssessment` alongside the
  existing `TrustAssessment`.

`POST /questions/{id}/answer` keeps working and becomes one of three callers of
the shared record-writing function.

## What this costs

The 409 does real work today. Its docstring is explicit: it is *"the rule that
stops a confident guess from being promoted to fact by a distracted reviewer."*
Removing it means a reviewer can ground anything by pressing one key.

That protection has to be replaced, not simply dropped:

1. **The states stay distinguishable.** A claim grounded by a passing check
   derives `Trust.VERIFIED`; a claim grounded only by a person derives
   `Trust.AUTHORITATIVE` via `Authority.ASSERTED`. The emitted view can and must
   say which — "a check tested this" and "a named person asserted this" are
   different promises to an agent.
2. **The reviewer must be told before acting**, not after. The interface has to
   show that nothing tested a claim at the moment the decision is offered. Today
   that arrives as a failed request after the fact, which is worse.
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
