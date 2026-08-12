"""What kind of column this is, and how much a wrong claim about it costs.

Review does not scale with schema size. On a 200-column schema roughly half the
columns are shape-determined — a primary key, an audit timestamp — and prose
about them tells an agent nothing its type did not. On an 800-column table the
proportion is higher, because wide tables are repetitive.

Two derived properties follow from that, and both are deterministic: no model
decides them, so they cannot drift between runs.

`ColumnClass` groups columns a reviewer would judge identically, which is what
makes one decision cover forty columns.

`Consequence` answers the only question that matters for triage: does an agent
write wrong SQL if this claim is wrong? Grain and join keys always do. An audit
timestamp's description never does.
"""

from __future__ import annotations

import re
from enum import StrEnum

from atlas.snapshot import Column, Table

AUDIT_TIMESTAMP = re.compile(r"(^|_)(created|updated|modified|deleted|inserted)_?(at|on|date|time)$|_at$|_on$", re.IGNORECASE)
BOOLEAN_NAME = re.compile(r"^(is|has|can|should|was|does)_", re.IGNORECASE)
CATEGORICAL_NAME = re.compile(r"(status|state|stage|type|kind|role|mode|category|tier|level)$", re.IGNORECASE)
MEASURE_NAME = re.compile(r"(count|num|qty|quantity|total|amount|price|cost|value|score|rate|size|bytes)$", re.IGNORECASE)
FREE_TEXT_NAME = re.compile(r"(name|title|label|description|desc|notes?|comments?|content|body|summary|message)$", re.IGNORECASE)
VERSION_NAME = re.compile(r"(version|revision|seq|sequence|order|position|index)$", re.IGNORECASE)


class ColumnClass(StrEnum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    AUDIT_TIMESTAMP = "audit_timestamp"
    BOOLEAN_FLAG = "boolean_flag"
    CATEGORICAL = "categorical"
    NUMERIC_MEASURE = "numeric_measure"
    FREE_TEXT = "free_text"
    VERSION = "version"
    OTHER = "other"


class Consequence(StrEnum):
    """What breaks downstream if the claim is wrong."""

    CRITICAL = "critical"  # grain, join keys — wrong SQL, silently
    HIGH = "high"  # filters, units, enums — wrong results
    ROUTINE = "routine"  # shape already says it; prose adds nothing


# Classes whose meaning is fully determined by name and type. A claim about one
# of these is not worth a reviewer's attention, and mostly not worth generating.
ROUTINE_CLASSES = frozenset(
    {
        ColumnClass.PRIMARY_KEY,
        ColumnClass.AUDIT_TIMESTAMP,
        ColumnClass.VERSION,
    }
)

# Aspects that always matter, whatever column they land on.
CRITICAL_ASPECTS = frozenset({"grain", "join"})


def classify_column(table: Table, column: Column) -> ColumnClass:
    name = column.name
    foreign_keys = {c for fk in table.foreign_keys for c in fk.columns}

    if column.is_primary_key:
        return ColumnClass.PRIMARY_KEY
    if name in foreign_keys or re.search(r"_id$|^id$", name, re.IGNORECASE):
        return ColumnClass.FOREIGN_KEY
    if AUDIT_TIMESTAMP.search(name):
        return ColumnClass.AUDIT_TIMESTAMP
    if column.data_type.upper().startswith("BOOL") or BOOLEAN_NAME.search(name):
        return ColumnClass.BOOLEAN_FLAG
    if VERSION_NAME.search(name):
        return ColumnClass.VERSION
    if CATEGORICAL_NAME.search(name) or column.is_enum_candidate:
        return ColumnClass.CATEGORICAL
    if MEASURE_NAME.search(name):
        return ColumnClass.NUMERIC_MEASURE
    if FREE_TEXT_NAME.search(name):
        return ColumnClass.FREE_TEXT
    return ColumnClass.OTHER


def consequence(table: Table, column: Column | None, aspect: str) -> Consequence:
    """How much a wrong claim costs.

    Table-level claims about grain are always critical: an agent with the wrong
    grain writes joins that double-count without erroring, which is the most
    expensive failure this catalogue can cause.
    """
    if aspect in CRITICAL_ASPECTS:
        return Consequence.CRITICAL
    if column is None:
        return Consequence.HIGH  # a table description shapes every query against it

    column_class = classify_column(table, column)
    if column_class is ColumnClass.FOREIGN_KEY:
        return Consequence.CRITICAL  # joins are written from these
    if column_class in ROUTINE_CLASSES:
        return Consequence.ROUTINE
    if column_class is ColumnClass.BOOLEAN_FLAG and column.profile.distinct_count == 1:
        return Consequence.ROUTINE  # a flag with one observed value carries no signal
    return Consequence.HIGH


# Every column is described. There is no `worth_describing` any more.
#
# It used to suppress routine columns before a claim was ever created, on the
# grounds that a description of `id` or `created_at` adds nothing a reader does
# not already have. The saving was real, but the artifact it produced was a
# catalogue with holes in it, and a reader could not tell a column Atlas judged
# self-evident from one the analysis had missed.
#
# Review load is still bounded, by `consequence` rather than by omission: a
# routine claim that its evidence supports is auto-accepted and never reaches a
# reviewer. The column gets its meaning; nobody has to read it.


