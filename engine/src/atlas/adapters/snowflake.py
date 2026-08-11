"""Snowflake source adapter.

Snowflake exposes familiar SQL but different guarantees from PostgreSQL:
ordinary-table keys are metadata, not enforcement; scans consume warehouse
credits; and read-only transactions are not a safety boundary. The adapter
therefore treats declared keys as hints, tags every Atlas query, applies a
session timeout, and relies on a SELECT-only Snowflake role.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Inspector
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
from atlas.redact import ENUM_MAX_DISTINCT, all_values_are_opaque_ids, value_withholding_reason
from atlas.settings import get_settings
from atlas.snapshot import (
    Column,
    ColumnProfile,
    Enforcement,
    ForeignKey,
    Snapshot,
    Table,
    ValueCount,
)

logger = logging.getLogger(__name__)

TOP_VALUE_LIMIT = 15
SAMPLE_ABOVE_ROWS = 500_000
SAMPLE_TARGET_ROWS = 100_000
DISTINCT_UNSUPPORTED = {"array", "object", "variant", "geography", "geometry"}
ORDERED_PREFIXES = (
    "number",
    "decimal",
    "numeric",
    "int",
    "integer",
    "bigint",
    "smallint",
    "float",
    "double",
    "real",
    "date",
    "datetime",
    "timestamp",
    "time",
)


class SnowflakeAdapter(DatabaseAdapter):
    capabilities = DatabaseCapabilities(
        # Ordinary Snowflake table constraints are informational. Hybrid table
        # enforcement can be added per constraint once table kind is retained
        # in the canonical snapshot.
        foreign_keys_enforced=False,
        supports_read_only_transaction=False,
        # Implemented as a Snowflake session parameter, not SET LOCAL.
        supports_statement_timeout=True,
    )

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # --- lifecycle -----------------------------------------------------

    def test_connection(self) -> None:
        with self.engine.connect() as connection:
            self._setup_session(connection)
            connection.execute(text("SELECT CURRENT_VERSION()"))

    def probe(self, namespace: str) -> ConnectionInfo:
        database, schema = self._namespace(namespace)
        source = self._information_schema(database, "TABLES")
        with self.engine.connect() as connection:
            self._setup_session(connection)
            version = connection.execute(text("SELECT CURRENT_VERSION()")).scalar_one()
            tables = connection.execute(
                text(
                    f"SELECT COUNT(*) FROM {source} "
                    "WHERE (TABLE_SCHEMA = :schema OR TABLE_SCHEMA = UPPER(:schema)) "
                    "AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')"
                ),
                {"schema": schema},
            ).scalar_one()
        return ConnectionInfo(
            server_version=f"Snowflake {version}",
            namespace=namespace,
            table_count=int(tables),
        )

    def close(self) -> None:
        self.engine.dispose()

    # --- identifiers ---------------------------------------------------

    def _quote(self, identifier: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(identifier)

    def _qualify(self, relation: str) -> str:
        return ".".join(self._quote(part) for part in relation.split(".") if part)

    def _namespace(self, namespace: str) -> tuple[str, str]:
        parts = [part for part in namespace.replace("/", ".").split(".") if part]
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 1:
            configured = [
                part for part in (self.engine.url.database or "").split("/") if part
            ]
            database = configured[0] if configured else ""
            return database, parts[0]
        raise ValueError("Snowflake namespace must be SCHEMA or DATABASE.SCHEMA")

    def _information_schema(self, database: str, view: str) -> str:
        prefix = f"{self._quote(database)}." if database else ""
        return f"{prefix}INFORMATION_SCHEMA.{self._quote(view)}"

    def _normalize(self, identifier: str) -> str:
        """Put an identifier into the casing the inspector reports.

        INFORMATION_SCHEMA returns names as Snowflake stored them — uppercase
        unless they were created quoted — while the inspector normalizes those
        same names to lowercase. Anything that keys off one and looks up with
        the other matches nothing.
        """
        normalize = getattr(self.engine.dialect, "normalize_name", None)
        return normalize(identifier) if callable(normalize) else identifier.lower()

    # --- structure -----------------------------------------------------

    def extract_structure(self, namespace: str) -> Snapshot:
        inspector = inspect(self.engine)
        estimates = self._row_estimates(namespace)
        table_names = set(inspector.get_table_names(schema=namespace))
        view_names = set(inspector.get_view_names(schema=namespace))
        names = sorted(table_names | view_names)
        tables = [
            self._extract_table(inspector, namespace, name, estimates.get(name))
            for name in names
        ]
        database, _ = self._namespace(namespace)
        return Snapshot(
            database=database or (self.engine.url.database or "unknown").split("/")[0],
            schema_name=namespace,
            dialect="snowflake",
            sample_policy=get_settings().atlas_sample_policy,
            tables=tables,
        )

    def _row_estimates(self, namespace: str) -> dict[str, int]:
        database, schema = self._namespace(namespace)
        source = self._information_schema(database, "TABLES")
        sql = text(
            f"SELECT TABLE_NAME, ROW_COUNT FROM {source} "
            "WHERE (TABLE_SCHEMA = :schema OR TABLE_SCHEMA = UPPER(:schema))"
        )
        with self.engine.connect() as connection:
            self._setup_session(connection)
            rows = connection.execute(sql, {"schema": schema}).mappings().all()
        # Normalized on the way in, because the only consumer looks these up by
        # the inspector's name. Keyed raw, every lookup missed, `estimated_rows`
        # stayed None, and `_table_source` then read that as a small table and
        # scanned the whole thing — the opposite of the sampling this exists for.
        return {
            self._normalize(str(row["table_name"])): int(row["row_count"] or 0)
            for row in rows
        }

    def _extract_table(
        self,
        inspector: Inspector,
        namespace: str,
        name: str,
        estimated_rows: int | None,
    ) -> Table:
        primary_key = (
            inspector.get_pk_constraint(name, schema=namespace).get("constrained_columns") or []
        )
        columns = [
            Column(
                name=column["name"],
                data_type=str(column["type"]),
                nullable=bool(column["nullable"]),
                default=None if column.get("default") is None else str(column["default"]),
                comment=column.get("comment"),
                is_primary_key=column["name"] in primary_key,
            )
            for column in inspector.get_columns(name, schema=namespace)
        ]
        comment = inspector.get_table_comment(name, schema=namespace).get("text")
        return Table(
            schema_name=namespace,
            name=name,
            comment=comment,
            columns=columns,
            primary_key=list(primary_key),
            foreign_keys=self._foreign_keys(inspector, namespace, name),
            # Snowflake has clustering keys rather than OLTP indexes. The
            # canonical model does not represent clustering yet.
            indexes=[],
            estimated_rows=estimated_rows,
        )

    def _foreign_keys(
        self, inspector: Inspector, namespace: str, name: str
    ) -> list[ForeignKey]:
        return [
            ForeignKey(
                name=foreign_key.get("name"),
                columns=list(foreign_key.get("constrained_columns") or []),
                referred_table=foreign_key["referred_table"],
                referred_columns=list(foreign_key.get("referred_columns") or []),
                enforcement=Enforcement.DECLARED_NOT_ENFORCED,
            )
            for foreign_key in inspector.get_foreign_keys(name, schema=namespace)
        ]

    # --- profiling -----------------------------------------------------

    def profile(self, snapshot: Snapshot) -> Snapshot:
        return snapshot.model_copy(
            update={"tables": [self._profile_table(table) for table in snapshot.tables]}
        )

    def _profile_table(self, table: Table) -> Table:
        source, sampled, sample_fraction = self._table_source(table)
        aggregates = self._aggregate_sweep(table, source)
        if aggregates is None:
            failed = ColumnProfile(error="aggregate sweep failed")
            return table.model_copy(
                update={
                    "columns": [
                        column.model_copy(update={"profile": failed})
                        for column in table.columns
                    ]
                }
            )

        row_count = int(aggregates["_row_count"])
        columns = [
            column.model_copy(
                update={
                    "profile": self._build_profile(
                        table,
                        column,
                        index,
                        aggregates,
                        row_count,
                        sampled,
                    )
                }
            )
            for index, column in enumerate(table.columns)
        ]
        estimate = (
            round(row_count / sample_fraction)
            if sampled and sample_fraction
            else row_count
        )
        return table.model_copy(
            update={
                "columns": columns,
                "exact_rows": None if sampled else row_count,
                "estimated_rows": estimate if sampled else table.estimated_rows,
            }
        )

    def _table_source(self, table: Table) -> tuple[str, bool, float | None]:
        qualified = self._qualify(f"{table.schema_name}.{table.name}")
        estimated = table.estimated_rows or 0
        if estimated <= SAMPLE_ABOVE_ROWS:
            return qualified, False, None
        fraction = max(0.0001, min(1.0, SAMPLE_TARGET_ROWS / estimated))
        percent = fraction * 100
        return f"{qualified} SAMPLE SYSTEM ({percent:.6f})", True, fraction

    def _aggregate_sweep(self, table: Table, source: str) -> dict[str, Any] | None:
        selects = ["COUNT(*) AS _row_count"]
        for index, column in enumerate(table.columns):
            quoted = self._quote(column.name)
            selects.append(f"COUNT({quoted}) AS c{index}_nonnull")
            if self._supports_distinct(column.data_type):
                selects.append(f"COUNT(DISTINCT {quoted}) AS c{index}_distinct")
            if self._supports_min_max(column.data_type):
                selects.append(f"TO_VARCHAR(MIN({quoted})) AS c{index}_min")
                selects.append(f"TO_VARCHAR(MAX({quoted})) AS c{index}_max")
        sql = f"SELECT {', '.join(selects)} FROM {source}"
        try:
            rows = self._run(sql)
            return rows[0] if rows else None
        except SQLAlchemyError as exc:
            logger.warning("aggregate sweep failed for %s: %s", table.qualified_name, exc)
            return None

    @staticmethod
    def _supports_distinct(data_type: str) -> bool:
        normalized = data_type.lower().split("(")[0].strip()
        return normalized not in DISTINCT_UNSUPPORTED

    @staticmethod
    def _supports_min_max(data_type: str) -> bool:
        normalized = data_type.lower().split("(")[0].strip()
        return any(normalized.startswith(prefix) for prefix in ORDERED_PREFIXES)

    def _build_profile(
        self,
        table: Table,
        column: Column,
        index: int,
        aggregates: dict[str, Any],
        row_count: int,
        sampled: bool,
    ) -> ColumnProfile:
        non_null = int(aggregates[f"c{index}_nonnull"])
        distinct = aggregates.get(f"c{index}_distinct")
        distinct_count = None if distinct is None else int(distinct)
        profile = ColumnProfile(
            null_fraction=None if row_count == 0 else round(1 - non_null / row_count, 4),
            distinct_count=distinct_count,
            is_unique=(
                None
                if distinct_count is None or row_count == 0
                else distinct_count == non_null
            ),
            min_value=aggregates.get(f"c{index}_min"),
            max_value=aggregates.get(f"c{index}_max"),
            sampled=sampled,
        )

        policy = get_settings().atlas_sample_policy
        key_columns = {
            key
            for foreign_key in table.foreign_keys
            for key in foreign_key.columns
        } | set(table.primary_key)
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

        top_values = self._top_values(table, column.name, policy)
        if (
            policy == "strict"
            and top_values
            and all_values_are_opaque_ids([value.value for value in top_values])
        ):
            return profile.model_copy(
                update={"values_withheld_reason": "values are opaque identifiers (UUIDs)"}
            )
        return profile.model_copy(update={"top_values": top_values})

    def _top_values(
        self, table: Table, column_name: str, policy: str
    ) -> list[ValueCount] | None:
        column = self._quote(column_name)
        relation = self._qualify(f"{table.schema_name}.{table.name}")
        sql = (
            f"SELECT TO_VARCHAR({column}) AS value, COUNT(*) AS n "
            f"FROM {relation} WHERE {column} IS NOT NULL "
            f"GROUP BY {column} ORDER BY n DESC LIMIT {TOP_VALUE_LIMIT}"
        )
        try:
            rows = self._run(sql)
        except SQLAlchemyError as exc:
            logger.warning("top-value query failed for %s.%s: %s", table.name, column_name, exc)
            return None
        if policy == "strict" and len(rows) > ENUM_MAX_DISTINCT:
            return None
        return [ValueCount(value=str(row["value"]), count=int(row["n"])) for row in rows]

    # --- checks --------------------------------------------------------

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
            return CheckObservation(
                check_type=check.type,
                sql=sql,
                error=f"{type(exc).__name__}: {exc}",
            )

        if check.type == "distribution":
            observations: dict[str, Any] = {
                "values": [
                    {"value": str(row["value"]), "count": int(row["n"])}
                    for row in rows
                ]
            }
            examined = sum(value["count"] for value in observations["values"])
        else:
            observations = {
                key: _plain(value)
                for key, value in (rows[0] if rows else {}).items()
            }
            examined = observations.get("total") or observations.get("source_rows")

        return CheckObservation(
            check_type=check.type,
            observations=observations,
            complete_scan=True,
            rows_examined=int(examined) if isinstance(examined, int | float) else None,
            sql=sql,
            limitations=["Snowflake constraints may be declared without enforcement."],
        )

    def _run(self, sql: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._setup_session(connection)
            return [dict(row) for row in connection.execute(text(sql)).mappings().all()]

    def _setup_session(self, connection: Any) -> None:
        timeout_seconds = max(
            1, math.ceil(get_settings().atlas_statement_timeout_ms / 1000)
        )
        connection.execute(text("ALTER SESSION SET QUERY_TAG = 'ATLAS'"))
        connection.execute(
            text(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}")
        )

    def _grain_sql(self, check: GrainCheck) -> str:
        relation = self._qualify(check.relation)
        keys = ", ".join(self._quote(field) for field in check.key_fields)
        null_condition = " OR ".join(
            f"{self._quote(field)} IS NULL" for field in check.key_fields
        )
        return (
            "SELECT COUNT(*) AS total, "
            f"COUNT(DISTINCT {keys}) AS distinct_keys, "
            f"COUNT_IF({null_condition}) AS null_keys FROM {relation}"
        )

    def _join_sql(self, check: JoinCheck) -> str:
        source = self._qualify(check.source_relation)
        target = self._qualify(check.target_relation)
        on = " AND ".join(
            f"s.{self._quote(left)} = t.{self._quote(right)}"
            for left, right in zip(
                check.source_fields, check.target_fields, strict=True
            )
        )
        keyed = " AND ".join(
            f"s.{self._quote(field)} IS NOT NULL" for field in check.source_fields
        )
        target_field = self._quote(check.target_fields[0])
        return (
            "SELECT COUNT(*) AS source_rows, "
            f"COUNT_IF(NOT ({keyed})) AS null_keys, "
            f"COUNT_IF(({keyed}) AND t.{target_field} IS NOT NULL) AS matched_rows, "
            f"COUNT_IF(({keyed}) AND t.{target_field} IS NULL) AS orphan_rows "
            f"FROM {source} s LEFT JOIN {target} t ON {on}"
        )

    def _distribution_sql(self, check: DistributionCheck) -> str:
        relation = self._qualify(check.relation)
        column = self._quote(check.field)
        return (
            f"SELECT TO_VARCHAR({column}) AS value, COUNT(*) AS n FROM {relation} "
            f"WHERE {column} IS NOT NULL GROUP BY {column} "
            f"ORDER BY n DESC LIMIT {int(check.limit)}"
        )

    def _nullability_sql(self, check: NullabilityCheck) -> str:
        relation = self._qualify(check.relation)
        parts = ["COUNT(*) AS total"]
        for field in check.fields:
            quoted = self._quote(field)
            alias = self._quote(f"{field}_nulls")
            parts.append(f"COUNT_IF({quoted} IS NULL) AS {alias}")
        return f"SELECT {', '.join(parts)} FROM {relation}"

    def _ordering_sql(self, check: OrderingCheck) -> str:
        relation = self._qualify(check.relation)
        earlier = self._quote(check.earlier_field)
        later = self._quote(check.later_field)
        return (
            "SELECT COUNT(*) AS total, "
            f"COUNT_IF({later} < {earlier}) AS violations, "
            f"COUNT_IF({earlier} IS NULL OR {later} IS NULL) AS incomparable "
            f"FROM {relation}"
        )


def _plain(value: Any) -> Any:
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)
