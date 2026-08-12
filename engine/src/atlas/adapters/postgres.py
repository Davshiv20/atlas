"""PostgreSQL adapter.

Holds every line of Postgres-specific SQL that used to sit in `extract.py`,
`profile.py`, and `query.py`: `pg_stat_user_tables`, `TABLESAMPLE SYSTEM`,
`::text`, `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Connection, Inspector
from sqlalchemy.exc import SQLAlchemyError

from atlas.adapters.base import (
    Check,
    CheckObservation,
    ConnectionInfo,
    DatabaseAdapter,
    DatabaseCapabilities,
    DistributionCheck,
    GrainCheck,
    JoinCheck,
    NullabilityCheck,
    OrderingCheck,
)
from atlas.redact import (
    ENUM_MAX_DISTINCT,
    all_values_are_opaque_ids,
    value_withholding_reason,
)
from atlas.settings import get_settings
from atlas.snapshot import (
    Column,
    ColumnProfile,
    Enforcement,
    ForeignKey,
    Index,
    Snapshot,
    Table,
    ValueCount,
)

logger = logging.getLogger(__name__)

TOP_VALUE_LIMIT = 15
SAMPLE_ABOVE_ROWS = 500_000
SAMPLE_TARGET_ROWS = 100_000

ROW_ESTIMATE_SQL = text(
    """
    -- reltuples is -1 or 0 until the table is analysed, which is the common
    -- case on a dev database, so fall back to the live-tuple counter.
    SELECT c.relname AS table_name,
           GREATEST(c.reltuples, COALESCE(s.n_live_tup, 0), 0)::bigint AS estimated_rows
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
    WHERE n.nspname = :schema AND c.relkind = 'r'
    """
)

DISTINCT_UNSUPPORTED = {"json", "xml", "tsvector", "point", "line"}
ORDERED_PREFIXES = (
    "int", "bigint", "smallint", "numeric", "decimal", "real",
    "double", "float", "money", "date", "timestamp", "time",
)


class PostgresAdapter(DatabaseAdapter):
    dialect = "postgresql"

    capabilities = DatabaseCapabilities(
        foreign_keys_enforced=True,
        supports_read_only_transaction=True,
        supports_statement_timeout=True,
    )

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # --- lifecycle ---------------------------------------------------------

    def test_connection(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def probe(self, namespace: str) -> ConnectionInfo:
        with self.engine.connect() as connection:
            version = connection.execute(text("SELECT version()")).scalar_one()
            tables = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
                ),
                {"schema": namespace},
            ).scalar_one()
        # "PostgreSQL 16.1 on aarch64…" — the first two words are the useful part.
        banner = " ".join(str(version).split()[:2])
        return ConnectionInfo(
            server_version=banner, namespace=namespace, table_count=int(tables)
        )

    def close(self) -> None:
        self.engine.dispose()

    # --- identifiers -------------------------------------------------------

    def _quote(self, identifier: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(identifier)

    def _qualify(self, relation: str) -> str:
        """`schema.table` or a bare table, quoted correctly either way."""
        parts = relation.split(".")
        return ".".join(self._quote(p) for p in parts)

    # --- structure ---------------------------------------------------------

    def extract_structure(self, namespace: str) -> Snapshot:
        inspector = inspect(self.engine)
        estimates = self._row_estimates(namespace)
        tables = [
            self._extract_table(inspector, namespace, name, estimates.get(name))
            for name in sorted(inspector.get_table_names(schema=namespace))
        ]
        return Snapshot(
            database=self.engine.url.database or "unknown",
            schema_name=namespace,
            dialect=self.engine.dialect.name,
            sample_policy=get_settings().atlas_sample_policy,
            tables=tables,
        )

    def _row_estimates(self, schema: str) -> dict[str, int]:
        with self.engine.connect() as connection:
            rows = connection.execute(ROW_ESTIMATE_SQL, {"schema": schema}).all()
        return {row.table_name: int(row.estimated_rows) for row in rows}

    def _extract_table(
        self, inspector: Inspector, schema: str, name: str, estimated_rows: int | None
    ) -> Table:
        primary_key = (
            inspector.get_pk_constraint(name, schema=schema).get("constrained_columns") or []
        )
        columns = [
            Column(
                name=col["name"],
                data_type=str(col["type"]),
                nullable=bool(col["nullable"]),
                default=None if col.get("default") is None else str(col["default"]),
                comment=col.get("comment"),
                is_primary_key=col["name"] in primary_key,
            )
            for col in inspector.get_columns(name, schema=schema)
        ]
        return Table(
            schema_name=schema,
            name=name,
            comment=inspector.get_table_comment(name, schema=schema).get("text"),
            columns=columns,
            primary_key=list(primary_key),
            foreign_keys=self._foreign_keys(inspector, schema, name),
            indexes=self._indexes(inspector, schema, name),
            estimated_rows=estimated_rows,
        )

    def _foreign_keys(self, inspector: Inspector, schema: str, name: str) -> list[ForeignKey]:
        return [
            ForeignKey(
                name=fk.get("name"),
                columns=list(fk["constrained_columns"]),
                referred_table=fk["referred_table"],
                referred_columns=list(fk["referred_columns"]),
                # Postgres upholds these, so a declaration is a guarantee.
                enforcement=self._fk_enforcement(),
            )
            for fk in inspector.get_foreign_keys(name, schema=schema)
        ]

    def _fk_enforcement(self) -> Enforcement:
        return (
            Enforcement.ENFORCED
            if self.capabilities.foreign_keys_enforced
            else Enforcement.DECLARED_NOT_ENFORCED
        )

    def _indexes(self, inspector: Inspector, schema: str, name: str) -> list[Index]:
        indexes = []
        for idx in inspector.get_indexes(name, schema=schema):
            columns = [c for c in idx.get("column_names") or [] if c is not None]
            if not columns:
                continue  # expression index; no column list to reason about
            indexes.append(
                Index(name=idx["name"] or "unnamed", columns=columns, unique=bool(idx["unique"]))
            )
        return indexes

    # --- profiling ---------------------------------------------------------

    def profile(
        self, snapshot: Snapshot, on_table: Callable[[str], None] | None = None
    ) -> Snapshot:
        def profiled(table: Table) -> Table:
            if on_table:
                on_table(table.name)
            return self._profile_table(table)

        return snapshot.model_copy(
            update={"tables": [profiled(t) for t in snapshot.tables]}
        )

    def _profile_table(self, table: Table) -> Table:
        source, sampled = self._table_source(table)
        aggregates = self._aggregate_sweep(table, source)
        if aggregates is None:
            failed = ColumnProfile(error="aggregate sweep failed")
            return table.model_copy(
                update={"columns": [c.model_copy(update={"profile": failed}) for c in table.columns]}
            )

        row_count = int(aggregates["_row_count"])
        columns = [
            column.model_copy(
                update={
                    "profile": self._build_profile(
                        table, column, i, aggregates, row_count, sampled, source
                    )
                }
            )
            for i, column in enumerate(table.columns)
        ]
        return table.model_copy(
            update={"columns": columns, "exact_rows": None if sampled else row_count}
        )

    def _table_source(self, table: Table) -> tuple[str, bool]:
        """Full table, or TABLESAMPLE when a per-column scan would be wasteful.
        Sampling is recorded so a reviewer never mistakes an estimate for a
        census."""
        qualified = f"{self._quote(table.schema_name)}.{self._quote(table.name)}"
        estimated = table.estimated_rows or 0
        if estimated <= SAMPLE_ABOVE_ROWS:
            return qualified, False
        percent = max(0.01, min(100.0, SAMPLE_TARGET_ROWS / estimated * 100))
        return f"{qualified} TABLESAMPLE SYSTEM ({percent:.4f})", True

    def _aggregate_sweep(self, table: Table, source: str) -> dict | None:
        selects = ["count(*) AS _row_count"]
        for index, column in enumerate(table.columns):
            quoted = self._quote(column.name)
            selects.append(f"count({quoted}) AS c{index}_nonnull")
            if self._supports_distinct(column.data_type):
                selects.append(f"count(DISTINCT {quoted}) AS c{index}_distinct")
            if self._supports_min_max(column.data_type):
                selects.append(f"min({quoted})::text AS c{index}_min")
                selects.append(f"max({quoted})::text AS c{index}_max")

        sql = f"SELECT {', '.join(selects)} FROM {source}"
        try:
            with self._read_only() as connection:
                return dict(connection.execute(text(sql)).mappings().one())
        except SQLAlchemyError as exc:
            logger.warning("aggregate sweep failed for %s: %s", table.qualified_name, exc)
            return None

    @staticmethod
    def _supports_distinct(data_type: str) -> bool:
        return data_type.lower().split("(")[0].strip() not in DISTINCT_UNSUPPORTED

    @staticmethod
    def _supports_min_max(data_type: str) -> bool:
        """Ordered, non-textual types only: a min/max over text prints two real
        values verbatim, which is a leak rather than a statistic."""
        normalized = data_type.lower().split("(")[0].strip()
        return any(normalized.startswith(p) for p in ORDERED_PREFIXES)

    def _build_profile(
        self,
        table: Table,
        column: Column,
        index: int,
        aggregates: dict,
        row_count: int,
        sampled: bool,
        source: str,
    ) -> ColumnProfile:
        non_null = int(aggregates[f"c{index}_nonnull"])
        distinct = aggregates.get(f"c{index}_distinct")
        distinct_count = None if distinct is None else int(distinct)

        profile = ColumnProfile(
            null_fraction=None if row_count == 0 else round(1 - non_null / row_count, 4),
            distinct_count=distinct_count,
            is_unique=None
            if distinct_count is None or row_count == 0
            else distinct_count == non_null,
            min_value=aggregates.get(f"c{index}_min"),
            max_value=aggregates.get(f"c{index}_max"),
            sampled=sampled,
        )

        policy = get_settings().atlas_sample_policy
        key_columns = {c for fk in table.foreign_keys for c in fk.columns} | set(table.primary_key)
        reason = value_withholding_reason(
            column.name,
            column.data_type,
            distinct_count,
            row_count,
            is_key=column.name in key_columns,
            policy=policy,
        )
        if reason is not None:
            return profile.model_copy(update={"values_withheld_reason": reason})

        top_values = self._top_values(source, column.name, policy)
        if (
            policy == "strict"
            and top_values
            and all_values_are_opaque_ids([v.value for v in top_values])
        ):
            return profile.model_copy(
                update={"values_withheld_reason": "values are opaque identifiers (UUIDs)"}
            )
        return profile.model_copy(update={"top_values": top_values})

    def _top_values(self, source: str, column_name: str, policy: str) -> list[ValueCount] | None:
        """Frequent values, read from the same rows the aggregate sweep read.

        `source` carries the TABLESAMPLE clause when the table was large enough
        to sample. Reading the full table here instead defeated the sampling
        entirely — one unbounded GROUP BY per column on a table the sweep had
        deliberately declined to scan — and, worse, produced a profile stamped
        `sampled=True` whose values came from a complete scan. A reader cannot
        tell those two apart, so the label has to be true of every field under
        it, not just the ones the sweep computed.
        """
        quoted_column = self._quote(column_name)
        sql = (
            f"SELECT {quoted_column}::text AS value, count(*) AS n "
            f"FROM {source} WHERE {quoted_column} IS NOT NULL "
            f"GROUP BY 1 ORDER BY n DESC LIMIT {TOP_VALUE_LIMIT}"
        )
        try:
            with self._read_only() as connection:
                rows = connection.execute(text(sql)).all()
        except SQLAlchemyError as exc:
            logger.warning("top-value query failed for %s: %s", column_name, exc)
            return None

        # Under strict, a high-cardinality column is an identifier or free text
        # and its values teach nothing. Under full, the most frequent values
        # still show the format, so they are kept.
        if policy == "strict" and len(rows) > ENUM_MAX_DISTINCT:
            return None
        return [ValueCount(value=row.value, count=int(row.n)) for row in rows]

    # --- checks ------------------------------------------------------------

    def execute_check(self, check: Check) -> CheckObservation:
        builders = {
            "grain": self._grain_sql,
            "join": self._join_sql,
            "distribution": self._distribution_sql,
            "nullability": self._nullability_sql,
            "ordering": self._ordering_sql,
        }
        builder = builders.get(check.type)
        if builder is None:
            return CheckObservation(check_type=check.type, error=f"unsupported check {check.type}")

        sql = builder(check)  # type: ignore[operator]
        try:
            rows = self._run(sql)
        except SQLAlchemyError as exc:
            return CheckObservation(check_type=check.type, sql=sql, error=f"{type(exc).__name__}: {exc}")

        if check.type == "distribution":
            observations: dict[str, Any] = {
                "values": [{"value": r["value"], "count": int(r["n"])} for r in rows]
            }
            examined = sum(int(r["n"]) for r in rows)
        else:
            observations = {k: _plain(v) for k, v in (rows[0] if rows else {}).items()}
            examined = observations.get("total") or observations.get("source_rows")

        return CheckObservation(
            check_type=check.type,
            observations=observations,
            complete_scan=True,  # no sampling in checks: a sampled grain is not a grain
            rows_examined=int(examined) if isinstance(examined, int | float) else None,
            sql=sql,
        )

    @contextmanager
    def _read_only(self) -> Iterator[Connection]:
        """The only way this adapter opens a connection.

        Every statement Atlas issues against a database it does not own goes
        through here, so the read-only transaction and the statement timeout
        cannot be forgotten. They were: the guard lived in `_run`, which covers
        typed checks, while the profiling sweep and the top-value query opened
        raw connections and ran unbounded — and those are the most expensive
        statements the adapter issues, a count(DISTINCT) and a GROUP BY per
        column. The cheap, bounded queries were guarded and the ruinous ones
        were not.
        """
        with self.engine.connect() as connection:
            self._begin_read_only(connection)
            yield connection

    def _run(self, sql: str) -> list[dict]:
        with self._read_only() as connection:
            return [dict(r) for r in connection.execute(text(sql)).mappings().all()]

    def _begin_read_only(self, connection: Connection) -> None:
        settings = get_settings()
        if self.capabilities.supports_read_only_transaction:
            connection.execute(text("SET TRANSACTION READ ONLY"))
        if self.capabilities.supports_statement_timeout:
            connection.execute(
                text(f"SET LOCAL statement_timeout = {settings.atlas_statement_timeout_ms}")
            )

    def _grain_sql(self, check: GrainCheck) -> str:
        relation = self._qualify(check.relation)
        keys = ", ".join(self._quote(f) for f in check.key_fields)
        # Any null component makes the key null, not just the first. Checking
        # only `key_fields[0]` reported null_keys = 0 for a composite grain
        # whose second column was nullable, while `count(DISTINCT (a, b))`
        # quietly skipped those same rows — so total and distinct_keys agreed
        # and a grain that does not hold was recorded as PASSED.
        any_null = " OR ".join(f"{self._quote(f)} IS NULL" for f in check.key_fields)
        return (
            f"SELECT count(*) AS total, "
            f"count(DISTINCT ({keys})) AS distinct_keys, "
            f"count(*) FILTER (WHERE {any_null}) AS null_keys "
            f"FROM {relation}"
        )

    def _join_sql(self, check: JoinCheck) -> str:
        source = self._qualify(check.source_relation)
        target = self._qualify(check.target_relation)
        on = " AND ".join(
            f"s.{self._quote(a)} = t.{self._quote(b)}"
            for a, b in zip(check.source_fields, check.target_fields, strict=True)
        )
        # A row that references nothing is not a broken reference. SQL's own
        # MATCH SIMPLE rule says a null key satisfies the constraint, and
        # counting those as orphans reported four declared, enforced foreign
        # keys in one schema as refuted purely because the column was unused.
        keyed = " AND ".join(f"s.{self._quote(a)} IS NOT NULL" for a in check.source_fields)
        # A semi-join, not a LEFT JOIN. The join fans out whenever the target
        # key is not unique, and `count(*)` over the joined result then counts
        # one source row once per match: a 3-row table reported 7 source rows
        # and a 14% orphan rate where the truth was 33%. Nothing downstream can
        # recover the real denominator from those numbers, so the count has to
        # be right here. EXISTS counts each source row exactly once, whatever
        # the target's cardinality.
        matched = f"EXISTS (SELECT 1 FROM {target} t WHERE {on})"
        return (
            f"SELECT count(*) AS source_rows, "
            f"count(*) FILTER (WHERE NOT ({keyed})) AS null_keys, "
            f"count(*) FILTER (WHERE ({keyed}) AND {matched}) AS matched_rows, "
            f"count(*) FILTER (WHERE ({keyed}) AND NOT {matched}) AS orphan_rows "
            f"FROM {source} s"
        )

    def _distribution_sql(self, check: DistributionCheck) -> str:
        relation = self._qualify(check.relation)
        column = self._quote(check.field)
        return (
            f"SELECT {column}::text AS value, count(*) AS n FROM {relation} "
            f"WHERE {column} IS NOT NULL GROUP BY 1 ORDER BY n DESC LIMIT {int(check.limit)}"
        )

    def _nullability_sql(self, check: NullabilityCheck) -> str:
        relation = self._qualify(check.relation)
        parts = ["count(*) AS total"]
        for f in check.fields:
            quoted = self._quote(f)
            parts.append(f"count(*) FILTER (WHERE {quoted} IS NULL) AS {self._quote(f'{f}_nulls')}")
        return f"SELECT {', '.join(parts)} FROM {relation}"

    def _ordering_sql(self, check: OrderingCheck) -> str:
        relation = self._qualify(check.relation)
        earlier, later = self._quote(check.earlier_field), self._quote(check.later_field)
        return (
            f"SELECT count(*) AS total, "
            f"count(*) FILTER (WHERE {later} < {earlier}) AS violations, "
            f"count(*) FILTER (WHERE {earlier} IS NULL OR {later} IS NULL) AS incomparable "
            f"FROM {relation}"
        )


def _plain(value: Any) -> Any:
    """Numbers stay numbers; everything else becomes text so an observation is
    always JSON-serialisable and comparable across engines."""
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)
