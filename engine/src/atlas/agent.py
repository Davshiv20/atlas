"""The analysis loop: one agent session per table.

Per-table rather than per-column because the useful inferences are relational —
which of three date columns is the business event, whether a status column
explains a null pattern elsewhere — and a column-at-a-time loop cannot see them.
Per-table also keeps the run cheap on wide schemas, where most columns are
surrogate keys and timestamps that no human will ever review.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from atlas.adapters.base import (
    DatabaseAdapter,
    DistributionCheck,
    GrainCheck,
    JoinCheck,
    OrderingCheck,
)
from atlas.checks import run_check
from atlas.classify import consequence, worth_describing
from atlas.evidence import ClaimEvidence, EvidenceRecord, EvidenceStore, LinkKind, Verdict
from atlas.facts import Consequence, Fact, FactStatus, FactStore, Provenance, ProvenanceKind
from atlas.llm import Tool, build_client, run_tool_loop
from atlas.policy import Trust, evaluate
from atlas.questions import Question, QuestionLog
from atlas.settings import get_settings
from atlas.snapshot import Column, Snapshot, Table

logger = logging.getLogger(__name__)

# Migration bookkeeping — real tables to a schema reader, noise to a catalogue.
INFRASTRUCTURE_TABLES = {"alembic_version", "schema_migrations", "flyway_schema_history"}

ASPECTS = ("grain", "semantics", "join", "lifecycle", "unit", "quality", "metric")

SYSTEM_PROMPT = """\
You are building a business-context catalogue for a database schema nobody on \
the team has documented. Your output is consumed by other agents writing SQL \
against this schema, and reviewed by a human first.

Work bottom-up, in this order:

1. Columns first. Describe what each column means, one claim per column. A \
description states meaning, unit, and role — not its distribution. Skip a \
column only when its shape already says everything (a surrogate key, an \
audit timestamp with nothing unusual about it).
2. Grain next, once you know what the columns are. State exactly what one row \
represents, in the form "one row per X". This is the single most consequential \
claim about a table: an agent that has the grain wrong writes silently \
double-counting joins. Record it with `record_grain`, and be honest in the \
confidence — a grain you inferred from a primary key alone is not the same as \
one you confirmed against the columns that actually vary per row.
3. The table description last, synthesized from the columns and the grain. It \
should be something you could only write having read the columns — if it could \
have been written from the table name alone, it is not worth recording.
4. Anything else worth noting: lifecycle, quality, units, metrics.

Relationships are already settled before you start. Every line marked \
`established:` has been verified against the database, or is enforced by a \
constraint. Do not run join checks for them and do not record claims about \
them — that work is done, and repeating it spends the turns you need for \
meaning. Propose a join only for a relationship that is not listed and that \
you have a specific reason to suspect.

Ground every claim before recording it. You have read-only SQL — use it to test \
what you believe rather than to confirm it. Evidence must be a query that could \
have contradicted the claim: an aggregate, a GROUP BY, a DISTINCT, a HAVING. \
Selecting a few rows shows you what exists and never what is always true, so it \
grounds nothing and will be rejected. Checks that tend to pay off: orphan \
rates on candidate joins, count-distinct against row count for grain, whether an \
enum's value set is closed, whether a soft-delete column is actually exercised, \
ordering between date columns, magnitude checks that reveal units. Follow up on \
whatever a result makes suspicious — a 4% orphan rate is a finding only once you \
know whether the orphans share a date range, a creator, or nothing at all.

A well-constructed question is as valuable as a fact, and often more honest. \
Business meaning is frequently not derivable from data at any effort: if a \
column holds four identifier formats, no query tells you whether that is one \
convention that drifted or four systems feeding one field. Ask, and include the \
evidence that makes the question sharp.

Some sample values are withheld by a privacy policy. The withholding reason is \
itself information — it tells you the column's shape without showing content. \
Do not attempt to work around it by querying the underlying values.

Record findings through the tools. Prose in your final message is not collected.\
"""


class AnalysisSink(BaseModel):
    facts: list[Fact] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    # Evidence is minted here, never by the agent. A claim can only cite an id
    # that a check actually produced, which is what makes an unbacked claim
    # unrepresentable rather than merely discouraged.
    evidence: EvidenceStore = Field(default_factory=EvidenceStore)
    # True when the model was cut off by the turn ceiling rather than finishing.
    # Its claims are then a partial reading of the table, not a complete one.
    truncated: bool = False


def render_table(
    snapshot: Snapshot, table: Table, relationships: list[str] | None = None
) -> str:
    """The physical view the agent reasons over, sample values included where
    the redaction policy allowed them.

    `relationships` are the ones already settled mechanically. Passing them in
    is what stops the agent spending its turn budget rediscovering joins the
    schema already answers.
    """
    lines = [
        (
            f"TABLE {table.qualified_name} — {table.row_count} rows"
            f"{f' — {table.comment}' if table.comment else ''}"
        ),
        f"primary key: {', '.join(table.primary_key) or 'none'}",
    ]

    for settled in relationships or []:
        lines.append(f"established: {settled}")

    for fk in table.foreign_keys:
        lines.append(
            f"declared fk: {', '.join(fk.columns)} -> "
            f"{fk.referred_table}({', '.join(fk.referred_columns)})"
        )
    for index in table.indexes:
        lines.append(f"index{' unique' if index.unique else ''}: {', '.join(index.columns)}")

    lines.append(f"referenced by {snapshot.inbound_fk_count(table.name)} other table(s)")
    describable = [c for c in table.columns if worth_describing(table, c)]
    routine = [c for c in table.columns if not worth_describing(table, c)]

    lines.append("")
    lines.append("COLUMNS TO DESCRIBE")
    for column in describable:
        lines.append(f"  {column.name} ({column.data_type}){_column_detail(column)}")

    if routine:
        lines.append("")
        lines.append(
            "SHAPE-DETERMINED COLUMNS — do not record claims for these. Their meaning "
            "follows from name and type; a description would add nothing a reader does "
            "not already have. Use them for grain and joins."
        )
        for column in routine:
            lines.append(f"  {column.name} ({column.data_type})")

    lines.append("")
    lines.append("OTHER TABLES IN SCHEMA")
    lines.append(", ".join(t.name for t in snapshot.tables if t.name != table.name))
    return "\n".join(lines)


def _column_detail(column: Column) -> str:
    profile = column.profile
    parts: list[str] = []
    if column.is_primary_key:
        parts.append("pk")
    if not column.nullable:
        parts.append("not null")
    if profile.null_fraction is not None:
        parts.append(f"null={profile.null_fraction:.0%}")
    if profile.distinct_count is not None:
        parts.append(f"distinct={profile.distinct_count}")
    if profile.min_value is not None:
        parts.append(f"range={profile.min_value}..{profile.max_value}")
    if profile.sampled:
        parts.append("SAMPLED (estimates, not a census)")

    detail = "  " + ", ".join(parts) if parts else ""
    if profile.top_values:
        values = ", ".join(f"{v.value}({v.count})" for v in profile.top_values[:10])
        return f"{detail}\n      values: {values}"
    if profile.values_withheld_reason:
        return f"{detail}\n      values held: {profile.values_withheld_reason}"
    return detail


def _irrelevant(subject: str, record: EvidenceRecord) -> str | None:
    """Why this evidence cannot bear on this subject, or None if it can.

    Existence was the only check before, so a claim about `users.password_hash`
    could cite the distribution of `users.role` and be scored on it. Records
    carry `field:<table>.<column>` subjects precisely so this is answerable.
    """
    fields = {s.removeprefix("field:") for s in record.subjects if s.startswith("field:")}
    if not fields:
        # Relation-scoped evidence — a grain or a join. It bears on the table
        # and on any column in it.
        return None
    if subject in fields:
        return None
    return f"it observed {', '.join(sorted(fields))}, not {subject}"


def _bearing(record: EvidenceRecord) -> LinkKind:
    """How one evidence record bears on the claim that cited it.

    Three outcomes, not two. The linker used to ask only "did this verify?" and
    file everything else as CONTRADICTS, so a distribution showing exactly the
    values a claim described was recorded as refuting it — a third of one run's
    claims were scored as contradicted with no failed check behind any of them.
    """
    if record.is_verification:
        return LinkKind.SUPPORTS
    if record.is_observation:
        # Observes without asserting. Real support, capped at OBSERVED by the
        # policy rather than by pretending it verified something.
        return LinkKind.SUPPORTS
    if record.verdict is Verdict.FAILED:
        return LinkKind.CONTRADICTS
    # An assertion that could not be settled — an empty table satisfies every
    # grain vacuously. It neither supports nor refutes.
    return LinkKind.INCONCLUSIVE


def build_tools(
    adapter: DatabaseAdapter, snapshot: Snapshot, sink: AnalysisSink
) -> list[Tool]:
    """The agent's whole surface.

    There is no generic SQL tool. The agent proposes a hypothesis as typed
    parameters; Atlas composes the SQL, runs it, and decides whether the
    assertion held. Three things follow that prompting could never guarantee:
    the agent cannot write a query that leaks column values, cannot cite
    evidence that does not exist, and cannot rule on its own hypothesis.
    """
    database = snapshot.database

    def _record(check) -> str:
        record, message = run_check(adapter, check, database=database)
        if record is None:
            return message
        sink.evidence.add(record)
        return f"{record.id} — {message}"

    def run_grain_check(relation: str, key_fields: list[str]) -> str:
        return _record(GrainCheck(relation=relation, key_fields=key_fields))

    def run_join_check(
        source_relation: str,
        source_fields: list[str],
        target_relation: str,
        target_fields: list[str],
    ) -> str:
        return _record(
            JoinCheck(
                source_relation=source_relation,
                source_fields=source_fields,
                target_relation=target_relation,
                target_fields=target_fields,
            )
        )

    def run_distribution_check(relation: str, field: str) -> str:
        return _record(DistributionCheck(relation=relation, field=field))

    def run_ordering_check(relation: str, earlier_field: str, later_field: str) -> str:
        return _record(
            OrderingCheck(relation=relation, earlier_field=earlier_field, later_field=later_field)
        )

    def describe_table(name: str) -> str:
        table = snapshot.table(name)
        if table is None:
            available = ", ".join(t.name for t in snapshot.tables)
            return f"No table named {name!r}. Available: {available}"
        return render_table(snapshot, table)

    def record_claim(
        subject: str,
        aspect: str,
        claim: str,
        evidence_ids: list[str],
        discriminator: str = "",
    ) -> str:
        # Resolved once. Looking each id up again on every use meant four
        # separate places had to trust that the guard above had run, and none
        # of them could be checked — a miss would have reached `evaluate` as a
        # None and failed there instead of here.
        cited = {e: sink.evidence.by_id(e) for e in evidence_ids}
        unknown = [e for e, record in cited.items() if record is None]
        if unknown:
            return (
                f"REJECTED: no such evidence {unknown}. Cite ids returned by a check you ran "
                f"in this session."
            )
        records = {e: record for e, record in cited.items() if record is not None}

        mismatched = [
            f"{e} ({reason})"
            for e, record in records.items()
            if (reason := _irrelevant(subject, record)) is not None
        ]
        if mismatched:
            return (
                f"REJECTED: evidence that is not about {subject}: {'; '.join(mismatched)}. "
                f"Run a check on {subject} itself, or claim what the evidence you have "
                f"actually observed."
            )

        weight = _consequence_of(snapshot, subject, aspect)
        fact_id = f"{subject}#{aspect}" + (f"#{discriminator}" if discriminator else "")

        links = [
            ClaimEvidence(
                claim_id=fact_id,
                evidence_id=e,
                relationship=_bearing(records[e]),
                rationale=claim[:200],
            )
            for e in evidence_ids
        ]
        pairs = [(link, records[link.evidence_id]) for link in links]
        trust, score, reasons = evaluate(aspect, pairs)

        if trust is Trust.UNSUPPORTED and weight is not Consequence.ROUTINE:
            return (
                f"REJECTED: a {weight.value}-consequence {aspect} claim cannot be recorded "
                f"without supporting evidence. Run a check that could have contradicted it, "
                f"or ask a question instead."
            )

        try:
            fact = Fact(
                subject=subject,
                aspect=aspect,
                claim=claim,
                confidence=score,
                provenance=[
                    Provenance(
                        kind=ProvenanceKind.GROUNDED_CHECK
                        if trust is not Trust.UNSUPPORTED
                        else ProvenanceKind.LLM_INFERENCE,
                        detail=f"{trust.value}: {'; '.join(reasons)}",
                        # Three outcomes here too: a claim resting on nothing
                        # is not a passing check, and was being recorded as one.
                        result="fail"
                        if trust is Trust.CONTRADICTED
                        else "inconclusive"
                        if trust is Trust.UNSUPPORTED
                        else "pass",
                    )
                ],
                discriminator=discriminator or None,
                consequence=weight,
                status=FactStatus.AUTO_ACCEPTED
                if weight is Consequence.ROUTINE and trust is not Trust.UNSUPPORTED
                else FactStatus.UNVERIFIED,
            )
        except ValidationError as exc:
            return f"REJECTED: {exc.errors()[0]['msg']}"

        sink.facts.append(fact)
        for link in links:
            sink.evidence.link(link)
        return f"Recorded {fact.id} — {trust.value}, confidence {score:.2f} ({'; '.join(reasons)})."

    def ask_human(subject: str, question: str, evidence: str, aspect: str = "semantics") -> str:
        # `aspect` is what the answer would establish. Without it an answer has
        # no claim to attach to, and answering is the only thing that lifts a
        # business claim past the OBSERVED ceiling.
        sink.questions.append(
            Question(subject=subject, question=question, evidence=evidence, aspect=aspect)
        )
        return "Queued for review."

    def _tool(name, description, properties, required, run) -> Tool:
        return Tool(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            run=run,
        )

    string = {"type": "string"}
    string_list = {"type": "array", "items": {"type": "string"}}

    return [
        _tool(
            "run_grain_check",
            "Test what one row of a table represents. Compares row count with distinct key "
            "count and counts null keys. The single most consequential check: an agent with "
            "the grain wrong writes joins that silently double-count.",
            {"relation": string, "key_fields": string_list},
            ["relation", "key_fields"],
            run_grain_check,
        ),
        _tool(
            "run_join_check",
            "Test whether every source row finds a match in the target, and report the "
            "orphan rate. Run this even when a foreign key is declared — on some engines a "
            "declared key is never enforced.",
            {
                "source_relation": string,
                "source_fields": string_list,
                "target_relation": string,
                "target_fields": string_list,
            },
            ["source_relation", "source_fields", "target_relation", "target_fields"],
            run_join_check,
        ),
        _tool(
            "run_distribution_check",
            "Observe which values occur in a column and how often. An observation, not a "
            "test: it can rule interpretations out, never confirm one.",
            {"relation": string, "field": string},
            ["relation", "field"],
            run_distribution_check,
        ),
        _tool(
            "run_ordering_check",
            "Test whether one timestamp always follows another — useful for lifecycle "
            "claims and for spotting columns written out of order.",
            {"relation": string, "earlier_field": string, "later_field": string},
            ["relation", "earlier_field", "later_field"],
            run_ordering_check,
        ),
        _tool(
            "describe_table",
            "Physical detail for any table: columns, types, constraints, profiles, and "
            "sample values where the privacy policy allows them.",
            {"name": string},
            ["name"],
            describe_table,
        ),
        _tool(
            "record_claim",
            "Record a claim, citing the evidence ids returned by checks you ran. A claim "
            "of any consequence needs at least one; without evidence, ask a question "
            "instead. Atlas computes the confidence — you do not choose it.",
            {
                "subject": string,
                "aspect": {"type": "string", "enum": list(ASPECTS)},
                "claim": string,
                "evidence_ids": string_list,
                "discriminator": {
                    "type": "string",
                    "description": (
                        "Required for join, quality, metric and lifecycle claims, which a "
                        "subject can have several of: the target table for a join, a slug "
                        "naming the finding otherwise."
                    ),
                },
            },
            ["subject", "aspect", "claim", "evidence_ids"],
            record_claim,
        ),
        _tool(
            "ask_human",
            "Queue a question for the reviewer. Use it when no check can settle the point — "
            "business meaning, intent, or which of several conventions is authoritative.",
            {
                "subject": string,
                "question": string,
                "evidence": string,
                "aspect": {
                    "type": "string",
                    "enum": list(ASPECTS),
                    "description": "What a reviewer's answer would establish about the subject.",
                },
            },
            ["subject", "question", "evidence"],
            ask_human,
        ),
    ]


def analyze_table(
    client: OpenAI,
    adapter: DatabaseAdapter,
    snapshot: Snapshot,
    table: Table,
    relationships: list[str] | None = None,
) -> AnalysisSink:
    sink = AnalysisSink()
    tools = build_tools(adapter, snapshot, sink)

    sink.truncated = run_tool_loop(
        client,
        system=SYSTEM_PROMPT,
        user=render_table(snapshot, table, relationships),
        tools=tools,
        on_text=lambda text: logger.info("[%s] %s", table.name, text.strip()[:300]),
    )
    if sink.truncated:
        logger.warning(
            "%s hit the turn ceiling after %d claim(s); its reading is partial",
            table.name,
            len(sink.facts),
        )

    for question in sink.questions:
        question.table = table.name
    return sink


def select_tables(
    snapshot: Snapshot,
    limit: int | None = None,
    tables: list[str] | None = None,
    already_analyzed: set[str] | None = None,
) -> list[Table]:
    """Which tables this run will cover.

    Named tables win over ranking — an explicit request is not a suggestion.
    Otherwise the most structurally central come first, and anything already
    analyzed is skipped: re-deriving a table costs money and, worse, produces
    reworded claims that reset human verdicts through `_carry_verdict`.
    """
    analyzed = already_analyzed or set()
    candidates = [t for t in snapshot.tables if t.name not in INFRASTRUCTURE_TABLES]

    if tables:
        wanted = {t.lower() for t in tables}
        return [t for t in candidates if t.name.lower() in wanted]

    pending = [t for t in candidates if t.name not in analyzed]
    ranked = sorted(
        pending,
        key=lambda t: (snapshot.inbound_fk_count(t.name), t.row_count),
        reverse=True,
    )
    return ranked if limit is None else ranked[:limit]


def analyze_schema(
    adapter: DatabaseAdapter,
    snapshot: Snapshot,
    limit: int | None = None,
    client: OpenAI | None = None,
    tables: list[str] | None = None,
    already_analyzed: set[str] | None = None,
    on_table_start: Callable[[str], None] | None = None,
    on_table_done: Callable[[str, AnalysisSink], None] | None = None,
    relationships: dict[str, list[str]] | None = None,
    workers: int | None = None,
) -> tuple[FactStore, QuestionLog, EvidenceStore]:
    """Analyze the selected tables. `limit=None` means every remaining one.

    Tables are read concurrently. Each one is an independent conversation over
    its own sink — nothing is shared between them but the adapter, whose pooled
    connections are already per-call — so the only ordering that matters is
    where results land, and that is imposed below rather than left to whichever
    worker finishes first.

    Returns the evidence alongside the claims: a claim without the observation
    behind it cannot be re-examined, which defeats the point of grounding it.

    The callbacks exist because a run is minutes per table: without them the
    caller cannot say which table is under way, and the console shows a spinner
    with nothing behind it. `on_table_done` also reports whether that table hit
    the turn ceiling, which is the difference between a finished reading and a
    partial one. Both are invoked under a lock: the concurrency belongs to this
    function, so callers write to the workspace as if they were sequential.
    """
    client = client or build_client()
    ranked = select_tables(snapshot, limit, tables, already_analyzed)
    if not ranked:
        return FactStore(), QuestionLog(), EvidenceStore()

    pool_size = min(workers or get_settings().atlas_max_workers, len(ranked))
    reporting = threading.Lock()
    finished: dict[str, AnalysisSink] = {}

    def read(table: Table) -> None:
        logger.info("analyzing %s (%s rows)", table.qualified_name, table.row_count)
        with reporting:
            if on_table_start:
                on_table_start(table.name)
        sink = analyze_table(
            client, adapter, snapshot, table, (relationships or {}).get(table.name)
        )
        # Persisting is serialised, not the reading. The workspace rewrites
        # whole files and merges claim by claim; two workers landing at once
        # would drop one of them.
        with reporting:
            finished[table.name] = sink
            logger.info(
                "  %s: %d facts, %d questions", table.name, len(sink.facts), len(sink.questions)
            )
            if on_table_done:
                on_table_done(table.name, sink)

    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="atlas-table") as pool:
        for outcome in as_completed([pool.submit(read, table) for table in ranked]):
            # Re-raised here rather than swallowed: one table failing is a run
            # failing, and a silently short catalogue is worse than an error.
            outcome.result()

    # Folded in the order the tables were selected, so a run's output does not
    # depend on which worker happened to finish first.
    store, questions, evidence = FactStore(), QuestionLog(), EvidenceStore()
    for table in ranked:
        sink = finished[table.name]
        store = store.merge(sink.facts)
        questions.questions.extend(sink.questions)
        for record in sink.evidence.records:
            evidence.add(record)
        evidence.links.extend(sink.evidence.links)

    return store, questions, evidence


def _consequence_of(snapshot: Snapshot, subject: str, aspect: str) -> Consequence:
    """Triage weight for a claim, derived from the schema rather than chosen.

    A model asked to rate its own claim's importance rates everything important.
    The column's class is a fact about the schema, so it is read, not asked for.
    """
    table_name, _, column_name = subject.partition(".")
    table = snapshot.table(table_name)
    if table is None:
        return Consequence.HIGH

    column = next((c for c in table.columns if c.name == column_name), None) if column_name else None
    return Consequence(consequence(table, column, aspect).value)
