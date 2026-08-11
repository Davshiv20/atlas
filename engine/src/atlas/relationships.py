"""Relationship discovery, without a model.

Joins are the one thing in this system a model should never decide, and until
now they were the thing it spent most of its budget guessing at: 14 of 35
evidence records in one run were join checks, 12 of them targeting a single
table, all rediscovered from scratch while analysing it. Every one of those
hypotheses is derivable from the schema.

Three sources, in descending order of authority:

1. **Declared and enforced.** The database already guarantees it. No query is
   needed and none is run — this is the strongest evidence obtainable, and
   re-measuring it is how a run spends four minutes proving what a constraint
   already proves.
2. **Declared but not enforced.** Snowflake's ordinary tables, and any schema
   where constraints are documentation. An intention worth checking.
3. **Inferred.** A reference-shaped column with no constraint behind it. The
   *candidate* is proposed mechanically; whether it holds is decided by a
   `JoinCheck`, never by the naming that suggested it.

The inference is deliberately conservative. Its main lever is the schema's own
conventions: if `created_by` is a declared foreign key to `users` in six
tables, then an undeclared `updated_by` is a question about `users` and not
about the other twenty-two tables. That reads intent already present in the
schema instead of inventing it.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from atlas.adapters.base import DatabaseAdapter, JoinCheck
from atlas.checks import run_check
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
from atlas.facts import (
    Consequence,
    Fact,
    FactStatus,
    Provenance,
    ProvenanceKind,
    join_discriminator,
)
from atlas.policy import Trust, assess
from atlas.snapshot import Enforcement, Snapshot, Table

logger = logging.getLogger(__name__)

# How many targets one undeclared column may be tested against. A column whose
# name resolves to nothing is a poor reason to issue twenty-two queries against
# someone's production database.
MAX_TARGETS_PER_COLUMN = 3

# Columns shaped like a reference. Everything else is not proposed at all.
REFERENCE_SUFFIXES = ("_id", "_by", "_uuid", "_key", "_fk", "_ref")


class Origin(StrEnum):
    DECLARED = "declared"
    INFERRED = "inferred"


@dataclass(frozen=True)
class Candidate:
    """One relationship worth establishing, and why it was proposed."""

    source_relation: str
    source_fields: list[str]
    target_relation: str
    target_fields: list[str]
    origin: Origin
    rationale: str
    # Set only for declared constraints. `ENFORCED` means the database upholds
    # it and no check is warranted.
    enforcement: Enforcement | None = None

    @property
    def needs_check(self) -> bool:
        return self.enforcement is not Enforcement.ENFORCED

    def to_check(self) -> JoinCheck:
        return JoinCheck(
            source_relation=self.source_relation,
            source_fields=list(self.source_fields),
            target_relation=self.target_relation,
            target_fields=list(self.target_fields),
        )


@dataclass(frozen=True)
class Relationship:
    """A candidate after the database has had its say."""

    candidate: Candidate
    verdict: Verdict
    evidence_id: str
    finding: str

    @property
    def holds(self) -> bool:
        return self.verdict in (Verdict.PASSED, Verdict.PASSED_WITH_WARNING)


@dataclass(frozen=True)
class Discovery:
    relationships: list[Relationship]
    evidence: EvidenceStore
    skipped: list[str]

    @property
    def verified(self) -> list[Relationship]:
        return [r for r in self.relationships if r.holds]


# --- proposing -------------------------------------------------------------


def propose(snapshot: Snapshot) -> tuple[list[Candidate], list[str]]:
    """Every relationship worth establishing, plus what was deliberately left
    alone and why."""
    declared = _declared(snapshot)
    conventions, families = _conventions(snapshot)
    constrained = {
        (candidate.source_relation, tuple(candidate.source_fields)) for candidate in declared
    }

    inferred: list[Candidate] = []
    skipped: list[str] = []
    for table in snapshot.tables:
        found, passed_over = _infer_for_table(
            table, snapshot, conventions, families, constrained
        )
        inferred.extend(found)
        skipped.extend(passed_over)

    return declared + inferred, skipped


def _declared(snapshot: Snapshot) -> list[Candidate]:
    return [
        Candidate(
            source_relation=table.name,
            source_fields=list(fk.columns),
            target_relation=fk.referred_table,
            target_fields=list(fk.referred_columns),
            origin=Origin.DECLARED,
            rationale=(
                "declared and enforced by the database"
                if fk.enforcement is Enforcement.ENFORCED
                else "declared but not enforced by this engine"
            ),
            enforcement=fk.enforcement,
        )
        for table in snapshot.tables
        for fk in table.foreign_keys
    ]


def _conventions(snapshot: Snapshot) -> tuple[dict[str, str], dict[str, str]]:
    """What column names mean here, learned from the constraints that exist.

    Two tiers. By exact name first: `updated_by` declared against `users` five
    times settles an undeclared `updated_by` elsewhere. Then by suffix family,
    and only when the family is unanimous — if every declared `*_by` column in
    the schema points at `users`, an undeclared `created_by` is a question
    about `users` and not about the other twenty-two tables.

    Both tiers read intent already present in the schema. Neither decides
    whether the relationship holds; a `JoinCheck` does that.
    """
    by_name: dict[str, Counter[str]] = defaultdict(Counter)
    by_suffix: dict[str, Counter[str]] = defaultdict(Counter)
    for table in snapshot.tables:
        for fk in table.foreign_keys:
            if len(fk.columns) != 1:
                continue
            column = fk.columns[0]
            by_name[column][fk.referred_table] += 1
            suffix = _suffix_of(column)
            if suffix:
                by_suffix[suffix][fk.referred_table] += 1

    names = {column: targets.most_common(1)[0][0] for column, targets in by_name.items()}
    # Unanimity only. A suffix used for two different targets says nothing.
    families = {
        suffix: next(iter(targets))
        for suffix, targets in by_suffix.items()
        if len(targets) == 1
    }
    return names, families


def _suffix_of(column: str) -> str | None:
    for suffix in REFERENCE_SUFFIXES:
        if column.lower().endswith(suffix):
            return suffix
    return None


def _infer_for_table(
    table: Table,
    snapshot: Snapshot,
    conventions: dict[str, str],
    families: dict[str, str],
    constrained: set[tuple[str, tuple[str, ...]]],
) -> tuple[list[Candidate], list[str]]:
    primary = set(table.primary_key)
    found: list[Candidate] = []
    passed_over: list[str] = []

    for column in table.columns:
        name = column.name
        if name in primary or (table.name, (name,)) in constrained:
            continue
        if not name.lower().endswith(REFERENCE_SUFFIXES):
            continue

        targets, why = _targets_for(
            name, column.data_type, table.name, snapshot, conventions, families
        )
        if not targets:
            passed_over.append(f"{table.name}.{name}: no candidate target ({why})")
            continue
        if len(targets) > MAX_TARGETS_PER_COLUMN:
            passed_over.append(
                f"{table.name}.{name}: {len(targets)} possible targets, too vague to test"
            )
            continue
        found.extend(
            Candidate(
                source_relation=table.name,
                source_fields=[name],
                target_relation=target.name,
                target_fields=list(target.primary_key),
                origin=Origin.INFERRED,
                rationale=why,
            )
            for target in targets
        )
    return found, passed_over


def _targets_for(
    column: str,
    data_type: str,
    owner: str,
    snapshot: Snapshot,
    conventions: dict[str, str],
    families: dict[str, str],
) -> tuple[list[Table], str]:
    """Which tables this column might reference, and the reason."""
    keyed = {
        table.name: table
        for table in snapshot.tables
        if len(table.primary_key) == 1 and table.name != owner
    }

    def typed(table: Table) -> bool:
        """The key types must agree. A VARCHAR column cannot reference an
        integer key, whatever it is called."""
        pk = next((c for c in table.columns if c.name == table.primary_key[0]), None)
        return pk is not None and _comparable(pk.data_type, data_type)

    convention = conventions.get(column)
    if convention in keyed and typed(keyed[convention]):
        return [keyed[convention]], f"{column} is a declared foreign key to {convention} elsewhere"

    suffix = _suffix_of(column)
    family = families.get(suffix) if suffix else None
    if family in keyed and typed(keyed[family]):
        return (
            [keyed[family]],
            f"every declared {suffix} column in this schema references {family}",
        )

    stem = re.sub(r"_(id|uuid|key|fk|ref)$", "", column.lower())
    if stem != column.lower():
        exact = [t for name, t in keyed.items() if _same_entity(name, stem) and typed(t)]
        if exact:
            return exact, f"{column} names the {stem} entity"
        # `current_version_id` against `deliverable_versions`: the stem is a
        # qualified form of the table's own name.
        partial = [
            t
            for name, t in keyed.items()
            if _same_entity(name.split("_")[-1], stem.split("_")[-1]) and typed(t)
        ]
        if partial:
            return partial, f"{column} ends in the same entity as these tables"
        return [], f"nothing named {stem}"

    return [], "the name carries no target and no convention matches it"


def _same_entity(a: str, b: str) -> bool:
    """Singular and plural of one name. Deliberately crude: it only has to
    decide whether to *test* something the database will then settle."""
    return _singular(a) == _singular(b)


def _singular(word: str) -> str:
    for suffix, replacement in (("ies", "y"), ("ses", "s"), ("s", "")):
        if word.endswith(suffix) and len(word) > len(suffix):
            return word[: -len(suffix)] + replacement
    return word


def _comparable(left: str, right: str) -> bool:
    """Whether two declared types could hold the same key.

    Length and precision are dropped: `VARCHAR(32)` and `VARCHAR` are the same
    kind of thing, and a key stored as one is routinely referenced as the
    other.
    """
    return _base_type(left) == _base_type(right)


def _base_type(declared: str) -> str:
    return re.sub(r"\(.*\)", "", declared).strip().upper()


# --- establishing ----------------------------------------------------------


def discover(adapter: DatabaseAdapter, snapshot: Snapshot, database: str) -> Discovery:
    """Settle every proposed relationship. No model is involved at any point."""
    candidates, skipped = propose(snapshot)
    store = EvidenceStore()
    settled: list[Relationship] = []

    for candidate in candidates:
        if not candidate.needs_check:
            record = _constraint_evidence(candidate, database)
            store.add(record)
            settled.append(
                Relationship(candidate, Verdict.PASSED, record.id, candidate.rationale)
            )
            continue

        record, message = run_check(adapter, candidate.to_check(), database=database)
        if record is None:
            skipped.append(f"{candidate.source_relation}.{candidate.source_fields[0]}: {message}")
            continue
        store.add(record)
        settled.append(Relationship(candidate, record.verdict, record.id, message))

    logger.info(
        "relationships: %d proposed, %d hold, %d skipped",
        len(candidates),
        sum(1 for r in settled if r.holds),
        len(skipped),
    )
    return Discovery(relationships=settled, evidence=store, skipped=skipped)


def _constraint_evidence(candidate: Candidate, database: str) -> EvidenceRecord:
    """Evidence for a relationship the database itself upholds.

    No query, and no scope claim: a constraint is not an observation over rows,
    it is a rule the engine refuses to let rows violate.
    """
    fields = ", ".join(candidate.source_fields)
    return EvidenceRecord(
        type=EvidenceType.DATABASE_CONSTRAINT,
        authority=Authority.ENFORCED,
        subjects=[
            f"relation:{candidate.source_relation}",
            f"relation:{candidate.target_relation}",
            f"field:{candidate.source_relation}.{candidate.source_fields[0]}",
        ],
        assertion=_constraint_assertion(candidate),
        observation={"enforcement": Enforcement.ENFORCED.value},
        scope=Scope(complete_scan=True),
        verdict=Verdict.PASSED,
        reasons=[f"the database enforces {candidate.source_relation}.{fields}"],
        limitations=[
            "Establishes that the reference is valid, not what the relationship means.",
            "A nullable key may still be unused; enforcement says nothing about coverage.",
        ],
        freshness=Freshness(
            valid_as_of=datetime.now(UTC),
            invalidated_by=[f"schema_change:relation:{candidate.source_relation}"],
        ),
        hypothesis={
            "source_relation": candidate.source_relation,
            "source_fields": list(candidate.source_fields),
            "target_relation": candidate.target_relation,
            "target_fields": list(candidate.target_fields),
            "check_type": "constraint",
            "database": database,
        },
    )


def _constraint_assertion(candidate: Candidate) -> Assertion:
    fields = ", ".join(candidate.source_fields)
    return Assertion(
        description=(
            f"{candidate.source_relation}.{fields} references "
            f"{candidate.target_relation}, enforced by the database."
        ),
        conditions={"enforcement": {"equals": Enforcement.ENFORCED.value}},
    )


# --- publishing ------------------------------------------------------------


#: What `facts.DISCRIMINATOR` accepts. Mirrored rather than imported as a regex
#: because the constraint is the model's; this only has to produce something it
#: will take.


def as_claims(discovery: Discovery) -> tuple[list[Fact], list[ClaimEvidence]]:
    """Verified relationships as reviewable claims.

    No model wrote these and none could improve them: the sentence is a
    restatement of what the constraint or the check established, and the
    confidence comes from `policy.assess` over that same evidence. They exist
    as claims so a relationship is reviewable, supersedable, and readable
    alongside everything else rather than living in a parallel structure.
    """
    facts: list[Fact] = []
    links: list[ClaimEvidence] = []

    for relationship in discovery.verified:
        candidate = relationship.candidate
        fields = ", ".join(candidate.source_fields)
        targets = ", ".join(candidate.target_fields)
        # The target alone is not a relationship: `clients.created_by` and
        # `clients.updated_by` both reference `users`, and keying on the target
        # collapses them into one claim that can only be reviewed once.
        discriminator = join_discriminator(
            candidate.target_relation, candidate.source_fields
        )
        claim_id = f"{candidate.source_relation}#join#{discriminator}"
        record = discovery.evidence.by_id(relationship.evidence_id)
        if record is None:  # every settled relationship minted its own record
            continue

        link = ClaimEvidence(
            claim_id=claim_id,
            evidence_id=relationship.evidence_id,
            relationship=LinkKind.SUPPORTS,
            rationale=relationship.finding[:200],
        )
        assessment = assess("join", [(link, record)])
        trust, score, reasons = (
            assessment.state,
            assessment.confidence,
            assessment.reasons,
        )

        facts.append(
            Fact(
                subject=candidate.source_relation,
                aspect="join",
                discriminator=discriminator,
                claim=(
                    f"{candidate.source_relation}.{fields} references "
                    f"{candidate.target_relation}.{targets}. {relationship.finding}"
                ),
                confidence=score,
                trust=assessment,
                provenance=[
                    Provenance(
                        kind=ProvenanceKind.GROUNDED_CHECK,
                        detail=f"{trust.value}: {'; '.join(reasons)}",
                        result="pass",
                    )
                ],
                consequence=Consequence.HIGH,
                # Structural and settled by the database, not by a model. A
                # human queue full of enforced foreign keys is a queue nobody
                # reads, and reviewing one is reviewing PostgreSQL.
                status=FactStatus.AUTO_ACCEPTED
                if trust is Trust.ENFORCED
                else FactStatus.UNVERIFIED,
            )
        )
        links.append(link)

    return facts, links


def by_table(discovery: Discovery) -> dict[str, list[str]]:
    """Settled relationships, phrased for the agent and filed under both ends.

    Both ends deliberately: reading `users` you need to know what points at it,
    and reading `sessions` you need to know where it points.
    """
    lines: dict[str, list[str]] = defaultdict(list)
    for relationship in discovery.verified:
        candidate = relationship.candidate
        fields = ", ".join(candidate.source_fields)
        targets = ", ".join(candidate.target_fields)
        sentence = (
            f"{candidate.source_relation}.{fields} -> "
            f"{candidate.target_relation}.{targets} ({relationship.finding})"
        )
        lines[candidate.source_relation].append(sentence)
        if candidate.target_relation != candidate.source_relation:
            lines[candidate.target_relation].append(sentence)
    return dict(lines)
