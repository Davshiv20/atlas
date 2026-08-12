"""The emitted semantic view — what an agent is actually given.

Everything before this module is about establishing *whether* something is
true. This is the first that decides what to *say*, and the two are not the
same document: the catalogue keeps every claim, its evidence, its confidence
and its review state, while the view keeps only what survived review, phrased
for something writing SQL against the schema.

Three rules shape it:

- **A column with no established meaning is not a dimension.** It is listed
  under `excluded` with the reason, because silently omitting it reads as
  "this column does not exist" and an agent will then invent one.
- **Review state travels with the claim.** A pending description is emitted as
  a comment, not as fact, so a reader can see which lines a human has stood
  behind and which are still the model's.
- **Relationships come from the map, never from prose.** They are already
  settled mechanically by `relationships.py`; restating them here would be a
  second answer to a question that has one.
"""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field

from atlas.facts import FactStatus
from atlas.output import ColumnOutput, SampleValue, SchemaOutput, TableOutput

# Below this share of non-null values a column carries nothing an agent can
# use. Named because it is a judgement, not a fact about databases.
EMPTY_COLUMN_THRESHOLD = 0.999
MAX_SAMPLE_VALUES = 5


class Dimension(BaseModel):
    name: str
    expr: str
    data_type: str
    description: str | None = None
    unique: bool = False
    nullable: bool = True
    sample_values: list[SampleValue] = Field(default_factory=list)
    samples_withheld: str | None = None
    #: False while the description is still the model's own.
    reviewed: bool = False
    reviewed_by: str | None = None


class Relationship(BaseModel):
    to: str
    left: str
    right: str
    enforced: bool


class Excluded(BaseModel):
    """A column deliberately left out, and why.

    Omitting it silently reads as "this column does not exist", and an agent
    told a table has ten columns when it has fifteen will invent the rest.
    """

    name: str
    reason: str
    sample_values: list[SampleValue] = Field(default_factory=list)
    samples_withheld: str | None = None


class TableView(BaseModel):
    name: str
    base_table: str
    row_count: int
    grain: str | None = None
    description: str | None = None
    reviewed_by: str | None = None
    dimensions: list[Dimension] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    excluded: list[Excluded] = Field(default_factory=list)
    #: Claims a human has not settled that would otherwise be emitted as fact.
    pending: int = 0

    @property
    def emittable(self) -> bool:
        """Whether this table can be published without a caveat.

        A table whose grain is unreviewed is the dangerous case: an agent that
        has the grain wrong writes silently double-counting joins.
        """
        return self.pending == 0 and self.grain is not None


class SemanticView(BaseModel):
    database: str
    schema_name: str
    tables: list[TableView] = Field(default_factory=list)

    @property
    def ready(self) -> list[TableView]:
        return [t for t in self.tables if t.emittable]


def build_semantic_view(output: SchemaOutput) -> SemanticView:
    """Every captured table, analysed or not.

    Omitting the unanalysed ones hid their existence: an agent reading the view
    could not tell the difference between a table Atlas has nothing to say about
    and a table that is not in the database. The first is a gap it should route
    around; the second is a fact about the schema. Emitting the shape with the
    meaning explicitly unestablished says which — `ready` stays false because
    grain is still missing, so nothing here can be mistaken for settled.
    """
    return SemanticView(
        database=output.database,
        schema_name=output.schema_name,
        tables=[_table(table) for table in output.tables],
    )


def _table(table: TableOutput) -> TableView:
    dimensions: list[Dimension] = []
    excluded: list[Excluded] = []

    for column in table.columns:
        reason = _why_excluded(table, column)
        if reason:
            excluded.append(
                Excluded(
                    name=column.name,
                    reason=reason,
                    sample_values=list(column.sample_values or [])[:MAX_SAMPLE_VALUES],
                    samples_withheld=column.values_withheld_reason,
                )
            )
            continue
        dimensions.append(_dimension(table, column))

    return TableView(
        name=table.name,
        base_table=table.qualified_name,
        row_count=table.row_count,
        grain=table.grain.text if table.grain else None,
        description=table.description.text if table.description else None,
        reviewed_by=_reviewer(table),
        dimensions=dimensions,
        relationships=[
            Relationship(
                to=join.referred_table,
                left=join.columns[0],
                right=join.referred_columns[0] if join.referred_columns else "id",
                enforced=join.enforced,
            )
            for join in table.joins
            if join.referred_table and join.columns
        ],
        excluded=excluded,
        pending=_pending(table),
    )


def _dimension(table: TableOutput, column: ColumnOutput) -> Dimension:
    claim = column.description
    return Dimension(
        name=column.name,
        # `expr` rather than a bare name because a view is eventually allowed
        # to rename or compute; today it is always the column itself.
        expr=column.name,
        data_type=column.data_type,
        description=claim.text if claim else None,
        unique=bool(
            column.is_primary_key
            or (column.distinct_count is not None and column.distinct_count == table.row_count)
        ),
        nullable=column.nullable,
        sample_values=list(column.sample_values or [])[:MAX_SAMPLE_VALUES],
        samples_withheld=column.values_withheld_reason,
        reviewed=bool(claim and claim.status is FactStatus.VERIFIED),
        reviewed_by=None,
    )


#: Reasons that are properties of the column, not gaps in the catalogue. Saying
#: "no established meaning" about `created_at` implies work is outstanding when
#: the shape already says everything and no claim was ever going to be made.
BY_SHAPE = {
    "primary_key": "surrogate key",
    "audit_timestamp": "audit timestamp",
    "foreign_key": "join key — see relationships",
}


def _why_excluded(table: TableOutput, column: ColumnOutput) -> str | None:
    """Why this column is not offered to an agent.

    The reason matters as much as the exclusion: a reader has to be able to
    tell "we decided this adds nothing" from "nobody has worked this out yet",
    and only the second is a reason to go and review something.
    """
    if column.null_fraction is not None and column.null_fraction >= EMPTY_COLUMN_THRESHOLD:
        return f"{column.null_fraction:.0%} null"
    if column.description and column.description.status is FactStatus.REJECTED:
        return "description rejected in review"
    by_shape = BY_SHAPE.get(column.column_class)
    if by_shape and column.description is None:
        return by_shape
    if column.description is None:
        return "no established meaning yet"
    return None


def _pending(table: TableOutput) -> int:
    """Consequential claims a human has not settled."""
    claims = [table.grain, table.description, *(c.description for c in table.columns)]
    return sum(
        1
        for claim in claims
        if claim is not None
        and claim.status is FactStatus.UNVERIFIED
        and claim.consequence.value in ("critical", "high")
    )


def _reviewer(table: TableOutput) -> str | None:
    """Who stood behind this table, if anyone is named.

    Keeps looking past a verified claim that carries no name — stopping at the
    first one left the attribution blank whenever the grain happened to be
    approved by an unnamed reviewer.
    """
    for claim in (table.grain, table.description):
        if claim and claim.status is FactStatus.VERIFIED and claim.reviewer:
            return claim.reviewer
    return None


# --- rendering -------------------------------------------------------------


def render_yaml(view: SemanticView, only: str | None = None) -> str:
    """The view as YAML, with review state carried in comments.

    Hand-rendered rather than dumped: the comments are the point. A reader has
    to be able to see which lines a person stood behind and which are still the
    model's, and no serializer will put that beside the value it qualifies.
    """
    tables = [t for t in view.tables if only is None or t.name == only]
    lines = ["tables:"]
    for table in tables:
        lines.extend(_render_table(table))
    return "\n".join(lines) + "\n"


def _render_table(table: TableView) -> list[str]:
    lines = [
        f"  - name: {table.name}",
        f"    base_table: {table.base_table}",
    ]
    if table.grain:
        lines.extend(_field("    ", "grain", table.grain.rstrip(".")))
    if table.reviewed_by:
        lines.append(f"    # approved by {table.reviewed_by}")
    if table.description:
        lines.extend(_field("    ", "description", table.description))

    if table.dimensions:
        lines.append("    dimensions:")
        for dimension in table.dimensions:
            lines.append(f"      - name: {dimension.name}")
            lines.append(f"        expr: {dimension.expr}")
            lines.append(f"        data_type: {dimension.data_type}")
            if dimension.unique:
                lines.append("        unique: true")
            if not dimension.nullable:
                lines.append("        nullable: false")
            # The description is the reason this file exists. Emitting a column
            # list without it hands an agent the schema it already had.
            if dimension.description:
                lines.extend(_field("        ", "description", dimension.description))
            lines.extend(_render_samples("        ", dimension.sample_values, dimension.samples_withheld))
            if not dimension.reviewed:
                lines.append("        # pending review")

    if table.relationships:
        lines.append("    relationships:")
        for relationship in table.relationships:
            lines.append(f"      - to: {relationship.to}")
            lines.append(f"        left: {relationship.left}")
            lines.append(f"        right: {relationship.right}")
            if not relationship.enforced:
                lines.append("        # verified by check, not enforced")

    if table.excluded:
        lines.append("    excluded:")
        for entry in table.excluded:
            lines.append(f"      - name: {entry.name}")
            lines.extend(_field("        ", "reason", entry.reason))
            lines.extend(_render_samples("        ", entry.sample_values, entry.samples_withheld))

    return lines


def _render_samples(
    indent: str, samples: list[SampleValue], withheld: str | None
) -> list[str]:
    """Emit sample state for every column without bypassing privacy policy."""
    if withheld:
        return _field(indent, "samples_withheld", withheld)
    if not samples:
        return [f"{indent}sample_values: []"]

    lines = [f"{indent}sample_values:"]
    for sample in samples:
        dumped = yaml.safe_dump(
            {"value": sample.value, "count": sample.count},
            width=68,
            allow_unicode=True,
            default_flow_style=True,
            sort_keys=False,
        ).rstrip("\n").splitlines()
        lines.append(f"{indent}  - {dumped[0]}")
        lines.extend(f"{indent}    {line}" for line in dumped[1:])
    return lines


def _field(indent: str, key: str, value: str) -> list[str]:
    """One field, with the value serialised rather than written by hand.

    Hand-written scalars produced a document that did not parse: a grain
    reading "the composite key enforces this grain: 538 rows" put a colon in a
    plain scalar and broke the entire file — and the file is the product. Every
    free-text value goes through the serialiser, which quotes and folds it
    correctly; only the structure and the comments are ours.
    """
    dumped = yaml.safe_dump(
        {key: value},
        width=68,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return [indent + line for line in dumped.rstrip("\n").split("\n")]
