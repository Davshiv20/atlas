"""Markdown rendering of the output document, for human review.

`output.yaml` is the artifact; this is a view of it. Rendering from the same
`SchemaOutput` the YAML serializes means the two can never disagree about what
was claimed.
"""

from __future__ import annotations

from atlas.facts import FactStatus
from atlas.output import Claim, ColumnOutput, SchemaOutput, TableOutput

MAX_SAMPLE_VALUES = 8

PREAMBLE = (
    "Claims carry an evidence-derived trust score, not a probability or model opinion. "
    "The score combines evidence directness, authority, coverage, consistency, and "
    "freshness. A checked claim held on the data present at capture time; only joins "
    "marked `[enforced]` are guaranteed by the database itself.\n\n"
    "Sample values are filtered by a privacy policy; a withholding reason describes a "
    "column's shape without exposing its contents."
)


def _tag(claim: Claim) -> str:
    score = round(claim.confidence * 100)
    state = claim.trust.state.value if claim.trust else (
        "checked" if claim.grounded else "unsupported"
    )
    decision = " · validated" if claim.status is FactStatus.VERIFIED else ""
    return f"[trust {score}/100 · {state}{decision}]"


def _samples(column: ColumnOutput) -> str:
    if column.sample_values:
        shown = column.sample_values[:MAX_SAMPLE_VALUES]
        rendered = ", ".join(f"{v.value} ({v.count})" for v in shown)
        more = "" if len(column.sample_values) <= MAX_SAMPLE_VALUES else ", …"
        return f"samples: {rendered}{more}"
    if column.values_withheld_reason:
        return f"samples withheld — {column.values_withheld_reason}"
    return "samples: none collected"


def _shape(column: ColumnOutput) -> str:
    parts = [column.data_type]
    if column.is_primary_key:
        parts.append("pk")
    if not column.nullable:
        parts.append("not null")
    if column.null_fraction:
        parts.append(f"{column.null_fraction:.0%} null")
    if column.distinct_count is not None:
        parts.append(f"{column.distinct_count} distinct")
    if column.min_value is not None:
        parts.append(f"{column.min_value} … {column.max_value}")
    if column.sampled:
        parts.append("SAMPLED — figures are estimates")
    return ", ".join(parts)


def _render_table(table: TableOutput) -> list[str]:
    approx = "" if table.row_count_is_exact else " (estimated)"
    lines = [
        f"## {table.qualified_name}",
        "",
        f"{table.row_count} rows{approx} · primary key: {', '.join(table.primary_key) or 'none'}",
        "",
    ]

    if table.grain:
        lines += [f"**Grain:** {table.grain.text} {_tag(table.grain)}", ""]
    else:
        lines += ["**Grain:** not established — do not assume one row per entity.", ""]

    if table.source_comment:
        lines += [f"Source comment: {table.source_comment}", ""]

    if table.description:
        lines += [f"{table.description.text} {_tag(table.description)}", ""]
    elif not table.analyzed:
        lines += ["_Not analyzed — physical detail only._", ""]

    if table.joins:
        lines.append("**Joins**")
        for join in table.joins:
            if join.enforced:
                lines.append(
                    f"- `{', '.join(join.columns)}` → `{join.referred_table}"
                    f"({', '.join(join.referred_columns)})` [enforced]"
                )
            elif join.description:
                lines.append(f"- {join.description.text} {_tag(join.description)}")
        lines.append("")

    if table.notes:
        lines.append("**Notes**")
        lines += [f"- {note.text} {_tag(note)}" for note in table.notes]
        lines.append("")

    lines.append("**Columns**")
    for column in table.columns:
        lines.append(f"- **{column.name}** — {_shape(column)}")
        lines.append(f"  {_samples(column)}")
        if column.description:
            lines.append(f"  {column.description.text} {_tag(column.description)}")
        lines += [f"  {note.text} {_tag(note)}" for note in column.notes]
    lines.append("")

    if table.open_questions:
        lines.append("**Unresolved — do not assume an answer**")
        lines += [f"- {question}" for question in table.open_questions]
        lines.append("")

    return lines


def render_markdown(output: SchemaOutput, limit: int | None = None) -> str:
    tables = output.tables[:limit] if limit is not None else output.tables
    header = [
        f"# Schema catalogue — {output.database}.{output.schema_name}",
        "",
        (
            f"Captured {output.captured_at:%Y-%m-%d}. {len(tables)} tables · "
            f"{output.claim_count} claims ({output.checked_claim_count} backed by an "
            f"executed check) · {output.question_count} unresolved questions."
        ),
        "",
        PREAMBLE,
        "",
        "---",
        "",
    ]
    body: list[str] = []
    for table in tables:
        body += _render_table(table)
    return "\n".join(header + body).rstrip() + "\n"
