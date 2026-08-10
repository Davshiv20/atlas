"""The physical layer.

Everything here is extracted or measured from the live database. No inference,
no LLM output. This is the ground truth that later layers make claims *about*,
and the baseline that drift detection compares against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Enforcement(StrEnum):
    """Whether the engine upholds a declared constraint.

    `DECLARED_NOT_ENFORCED` is a hint worth acting on, not proof: it says
    someone intended this relationship, which is a reason to run a coverage
    check rather than a reason to skip one.
    """

    ENFORCED = "enforced"
    DECLARED_NOT_ENFORCED = "declared_not_enforced"
    UNKNOWN = "unknown"


class ValueCount(BaseModel):
    value: str
    count: int


class ColumnProfile(BaseModel):
    """Measured distribution of a column. All fields optional: profiling a
    column can fail or be deliberately skipped, and a missing measurement must
    never be confused with a measured zero."""

    null_fraction: float | None = None
    distinct_count: int | None = None
    is_unique: bool | None = None
    min_value: str | None = None
    max_value: str | None = None
    top_values: list[ValueCount] | None = None
    values_withheld_reason: str | None = None
    sampled: bool = False
    error: str | None = None


class Column(BaseModel):
    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    comment: str | None = None
    is_primary_key: bool = False
    profile: ColumnProfile = Field(default_factory=ColumnProfile)

    @property
    def is_enum_candidate(self) -> bool:
        return self.profile.top_values is not None and len(self.profile.top_values) > 1


class ForeignKey(BaseModel):
    name: str | None
    columns: list[str]
    referred_table: str
    referred_columns: list[str]
    # Recorded per constraint, never inferred from the dialect: Snowflake
    # hybrid tables enforce keys while ordinary tables only declare them. A
    # declared-but-unenforced key is a reason to run a coverage check, not a
    # substitute for one.
    enforcement: Enforcement = Enforcement.ENFORCED


class Index(BaseModel):
    name: str
    columns: list[str]
    unique: bool


class Table(BaseModel):
    schema_name: str
    name: str
    comment: str | None = None
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    estimated_rows: int | None = None
    exact_rows: int | None = None  # measured during profiling; absent if sampled

    @property
    def row_count(self) -> int:
        return self.exact_rows if self.exact_rows is not None else (self.estimated_rows or 0)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.name}"


class Snapshot(BaseModel):
    database: str
    schema_name: str
    dialect: str
    source_id: str | None = None
    sample_policy: str = "strict"
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tables: list[Table] = Field(default_factory=list)

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    def inbound_fk_count(self, table_name: str) -> int:
        """How many other tables point at this one. Without query logs this is
        the best available proxy for 'how central is this table'."""
        return sum(
            1
            for t in self.tables
            for fk in t.foreign_keys
            if fk.referred_table == table_name and t.name != table_name
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json", exclude_none=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))

    @classmethod
    def read(cls, path: Path) -> Snapshot:
        return cls.model_validate(yaml.safe_load(path.read_text()))
