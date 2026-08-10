"""The single output document.

One file so nothing downstream has to join a snapshot to a fact store to make
sense of a column. Physical shape and claimed meaning sit on the same object,
and every claim carries the confidence and evidence that produced it — an
agent reading this can tell an enforced constraint from a guess without
consulting anything else.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from atlas.classify import Consequence, classify_column, consequence
from atlas.evidence import EvidenceStore, Scope, Verdict
from atlas.facts import Fact, FactStatus, FactStore, ProvenanceKind
from atlas.policy import TrustAssessment, assess
from atlas.questions import Question, QuestionStatus
from atlas.snapshot import Column, Snapshot, Table

DESCRIPTION_ASPECTS = ("semantics", "unit")
NOTE_ASPECTS = ("lifecycle", "quality", "metric")


class Claim(BaseModel):
    text: str
    # Evidence-derived trust score. This is not a probability.
    confidence: float
    trust: TrustAssessment | None = None
    status: FactStatus
    grounded: bool
    evidence: str | None = None
    consequence: Consequence = Consequence.HIGH
    # Who stood behind it. Carried into the emitted view so a reader can tell a
    # reviewed line from the model's own, and by whom.
    reviewer: str | None = None
    @classmethod
    def from_fact(cls, fact: Fact) -> Claim:
        checks = [p for p in fact.provenance if p.kind is ProvenanceKind.GROUNDED_CHECK]
        return cls(
            text=fact.claim,
            confidence=fact.confidence,
            trust=fact.trust,
            status=fact.status,
            grounded=bool(checks),
            evidence=checks[0].detail.removeprefix("executed: ") if checks else None,
            consequence=Consequence(fact.consequence.value),
            reviewer=fact.verified_by,
        )


class SampleValue(BaseModel):
    value: str
    count: int


class ColumnOutput(BaseModel):
    name: str
    column_class: str
    consequence: Consequence
    data_type: str
    nullable: bool
    is_primary_key: bool = False
    null_fraction: float | None = None
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    sampled: bool = False
    sample_values: list[SampleValue] | None = None
    values_withheld_reason: str | None = None
    description: Claim | None = None
    notes: list[Claim] = Field(default_factory=list)


class JoinOutput(BaseModel):
    columns: list[str] = Field(default_factory=list)
    referred_table: str | None = None
    referred_columns: list[str] = Field(default_factory=list)
    enforced: bool
    description: Claim | None = None


class ValidationSummary(BaseModel):
    """What "validated" means for this table.

    Counted over consequential claims only. A table is validated when every
    claim that could make an agent write wrong SQL has been judged — not when
    all 800 of its columns have. Routine claims are reported separately so the
    number is auditable rather than a smaller total with no explanation.
    """

    critical_total: int = 0
    critical_settled: int = 0
    high_total: int = 0
    high_settled: int = 0
    routine_total: int = 0
    routine_auto_accepted: int = 0

    @property
    def validated(self) -> bool:
        return (
            self.critical_total + self.high_total > 0
            and self.critical_settled == self.critical_total
            and self.high_settled == self.high_total
        )


class RuledOut(BaseModel):
    """A hypothesis that was tested and did not hold.

    Absence of a claim is ambiguous: it can mean nobody looked, or that someone
    looked and the answer was no. Four join hypotheses about `updated_by`
    columns were refuted in one run and the reviewer had no way to see any of
    them, so the next reader is free to assume the same relationship again.
    """

    hypothesis: str
    finding: str
    scope: str
    evidence_id: str


class TableOutput(BaseModel):
    name: str
    qualified_name: str
    row_count: int
    row_count_is_exact: bool
    primary_key: list[str] = Field(default_factory=list)
    source_comment: str | None = None
    grain: Claim | None = None
    description: Claim | None = None
    joins: list[JoinOutput] = Field(default_factory=list)
    notes: list[Claim] = Field(default_factory=list)
    columns: list[ColumnOutput] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    ruled_out: list[RuledOut] = Field(default_factory=list)
    analyzed: bool = False
    validation: ValidationSummary = Field(default_factory=ValidationSummary)


class SchemaOutput(BaseModel):
    database: str
    schema_name: str
    captured_at: datetime
    table_count: int
    claim_count: int
    checked_claim_count: int
    question_count: int
    tables: list[TableOutput] = Field(default_factory=list)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json", exclude_none=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100))

    @classmethod
    def read(cls, path: Path) -> SchemaOutput:
        return cls.model_validate(yaml.safe_load(path.read_text()))


def _all(facts: list[Fact], aspects: tuple[str, ...]) -> list[Claim]:
    """Every matching claim, strongest first."""
    matching = sorted((f for f in facts if f.aspect in aspects), key=lambda f: -f.confidence)
    return [Claim.from_fact(f) for f in matching]


def _pick(facts: list[Fact], aspects: tuple[str, ...]) -> tuple[Claim | None, list[Claim]]:
    """Highest-confidence matching claim becomes the description; the rest
    become notes rather than being discarded — a second opinion on the same
    subject is signal, not noise."""
    claims = _all(facts, aspects)
    return (claims[0], claims[1:]) if claims else (None, [])


def _build_column(
    table: Table, column: Column, facts: list[Fact]
) -> ColumnOutput:
    profile = column.profile
    description, extra = _pick(facts, DESCRIPTION_ASPECTS)
    notes = _all(facts, NOTE_ASPECTS)
    samples = (
        [SampleValue(value=v.value, count=v.count) for v in profile.top_values]
        if profile.top_values
        else None
    )
    return ColumnOutput(
        name=column.name,
        column_class=classify_column(table, column).value,
        consequence=consequence(table, column, "semantics"),
        data_type=column.data_type,
        nullable=column.nullable,
        is_primary_key=column.is_primary_key,
        null_fraction=profile.null_fraction,
        distinct_count=profile.distinct_count,
        min_value=profile.min_value,
        max_value=profile.max_value,
        sampled=profile.sampled,
        sample_values=samples,
        values_withheld_reason=profile.values_withheld_reason,
        description=description,
        notes=extra + notes,
    )


def _build_joins(table: Table, facts: list[Fact]) -> list[JoinOutput]:
    """One entry per relationship.

    Every join now carries a claim — the map is derived from the constraints
    and the checks together — so emitting the declared keys and the claims as
    separate lists showed each enforced relationship twice, once with a target
    and once without.
    """
    claims = {
        f.discriminator: f
        for f in sorted((f for f in facts if f.aspect == "join"), key=lambda f: -f.confidence)
        if f.discriminator
    }

    joins: list[JoinOutput] = []
    for fk in table.foreign_keys:
        key = f"{fk.referred_table}.{'_'.join(fk.columns)}"
        claim = claims.pop(key, None)
        joins.append(
            JoinOutput(
                columns=list(fk.columns),
                referred_table=fk.referred_table,
                referred_columns=list(fk.referred_columns),
                enforced=True,
                description=Claim.from_fact(claim) if claim else None,
            )
        )

    # What is left was established by measurement rather than declared. The
    # discriminator carries the target and the column, so the shape survives
    # without re-deriving it from prose.
    for key, claim in claims.items():
        target, _, columns = key.partition(".")
        joins.append(
            JoinOutput(
                columns=[columns] if columns else [],
                referred_table=target or None,
                enforced=False,
                description=Claim.from_fact(claim),
            )
        )
    return joins


def _ruled_out(evidence: EvidenceStore | None) -> dict[str, list[RuledOut]]:
    """Refuted hypotheses, grouped by the table they were about.

    Only failures no claim cites. A failed check that a claim does cite is an
    unresolved contradiction on that claim and is already shown there; repeating
    it here would read as a settled negative when it is not.
    """
    if evidence is None:
        return {}

    cited = {link.evidence_id for link in evidence.links}
    grouped: dict[str, list[RuledOut]] = {}
    for record in evidence.records:
        if record.verdict is not Verdict.FAILED or record.id in cited:
            continue
        relation = record.primary_relation
        if relation is None:
            continue
        grouped.setdefault(relation, []).append(
            RuledOut(
                hypothesis=record.assertion.description,
                finding="; ".join(record.reasons) if record.reasons else "the assertion failed",
                scope=_scope_phrase(record.scope),
                evidence_id=record.id,
            )
        )
    return grouped


def _scope_phrase(scope: Scope) -> str:
    rows = f"{scope.rows_examined:,} rows" if scope.rows_examined is not None else "the table"
    return f"complete scan over {rows}" if scope.is_durable else f"sampled from {rows}"


def assess_facts(store: FactStore, evidence: EvidenceStore | None) -> FactStore:
    """Re-score linked claims when output is read.

    Existing workspaces predate factorized trust and otherwise keep showing the
    old fixed 0.65/0.88 values forever. Re-assessment is derived and non-
    destructive: the fact store remains the review history, while the output
    reflects current policy and freshness. Claims with no linked evidence keep
    their legacy scalar until they are regenerated or grounded.
    """
    if evidence is None:
        return store

    current: list[Fact] = []
    for fact in store.facts:
        pairs = evidence.for_claim(fact.id)
        if not pairs:
            current.append(fact)
            continue
        assessment = assess(fact.aspect, pairs)
        current.append(
            fact.model_copy(
                update={"confidence": assessment.confidence, "trust": assessment}
            )
        )
    return FactStore(facts=current)


def build_output(
    snapshot: Snapshot,
    store: FactStore,
    questions: list[Question],
    evidence: EvidenceStore | None = None,
) -> SchemaOutput:
    ruled_out = _ruled_out(evidence)
    facts = assess_facts(store, evidence).facts
    by_subject: dict[str, list[Fact]] = {}
    for fact in facts:
        by_subject.setdefault(fact.subject, []).append(fact)


    tables: list[TableOutput] = []
    for table in snapshot.tables:
        table_facts = by_subject.get(table.name, [])
        grain, _ = _pick(table_facts, ("grain",))
        description, extra = _pick(table_facts, ("semantics",))
        notes = _all(table_facts, NOTE_ASPECTS)

        columns = [
            _build_column(
                table,
                column,
                by_subject.get(f"{table.name}.{column.name}", []),
            )
            for column in table.columns
        ]

        tables.append(
            TableOutput(
                name=table.name,
                qualified_name=table.qualified_name,
                row_count=table.row_count,
                row_count_is_exact=table.exact_rows is not None,
                primary_key=list(table.primary_key),
                source_comment=table.comment,
                grain=grain,
                description=description,
                joins=_build_joins(table, table_facts),
                notes=extra + notes,
                columns=columns,
                # Only the unsettled ones. An answered question is no longer a
                # gap in the catalogue — it is a claim, and counting it here
                # would keep telling the reviewer to go and answer it.
                open_questions=[
                    q.question
                    for q in questions
                    if q.table == table.name and q.status is QuestionStatus.OPEN
                ],
                ruled_out=ruled_out.get(table.name, []),
                # Not `bool(table_facts)`: reading `users` records join claims
                # whose subject is `sessions`, which would then advertise
                # itself as analyzed while holding no grain, description, or
                # column claims of its own.
                analyzed=bool(grain or description or any(c.description for c in columns)),
                validation=_summarize(grain, description, columns),
            )
        )

    # Analyzed tables first, then by structural centrality — the order a human
    # or an agent should read them in.
    tables.sort(
        key=lambda t: (t.analyzed, snapshot.inbound_fk_count(t.name), t.row_count), reverse=True
    )

    return SchemaOutput(
        database=snapshot.database,
        schema_name=snapshot.schema_name,
        captured_at=snapshot.extracted_at,
        table_count=len(tables),
        claim_count=len(facts),
        checked_claim_count=sum(
            1
            for f in facts
            if any(p.kind is ProvenanceKind.GROUNDED_CHECK for p in f.provenance)
        ),
        question_count=sum(1 for q in questions if q.status is QuestionStatus.OPEN),
        tables=tables,
    )


SETTLED = {FactStatus.VERIFIED, FactStatus.AUTO_ACCEPTED, FactStatus.REJECTED}


def _summarize(
    grain: Claim | None, description: Claim | None, columns: list[ColumnOutput]
) -> ValidationSummary:
    """Count what validation actually requires.

    A missing grain counts against the table: absence is not neutral, it is the
    single most consequential thing that can be unknown about a table.
    """
    summary = ValidationSummary(critical_total=1)  # grain is always required
    if grain is not None and grain.status in SETTLED:
        summary.critical_settled += 1

    for claim in [description, *(c.description for c in columns)]:
        if claim is None:
            continue
        if claim.consequence is Consequence.CRITICAL:
            summary.critical_total += 1
            summary.critical_settled += claim.status in SETTLED
        elif claim.consequence is Consequence.HIGH:
            summary.high_total += 1
            summary.high_settled += claim.status in SETTLED
        else:
            summary.routine_total += 1
            summary.routine_auto_accepted += claim.status is FactStatus.AUTO_ACCEPTED
    return summary
