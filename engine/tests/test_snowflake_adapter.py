from __future__ import annotations

from sqlalchemy import create_engine

from atlas.adapters.base import (
    DatabaseCapabilities,
    GrainCheck,
    JoinCheck,
    NullabilityCheck,
    OrderingCheck,
)
from atlas.adapters.registry import create_adapter
from atlas.adapters.snowflake import SnowflakeAdapter
from atlas.snapshot import Enforcement, Table

URL = "snowflake://user:password@account/ANALYTICS/PUBLIC?warehouse=ATLAS_WH&role=ATLAS_READER"


def adapter() -> SnowflakeAdapter:
    return SnowflakeAdapter(create_engine(URL))


def test_registry_selects_snowflake_without_connecting() -> None:
    selected = create_adapter(URL)
    try:
        assert isinstance(selected, SnowflakeAdapter)
    finally:
        selected.close()


def test_capabilities_do_not_claim_constraint_enforcement() -> None:
    assert SnowflakeAdapter.capabilities == DatabaseCapabilities(
        foreign_keys_enforced=False,
        supports_read_only_transaction=False,
        supports_statement_timeout=True,
    )


def test_namespace_accepts_schema_or_database_schema() -> None:
    source = adapter()
    try:
        assert source._namespace("RAW.EVENTS") == ("RAW", "EVENTS")
        assert source._namespace("PUBLIC") == ("ANALYTICS", "PUBLIC")
    finally:
        source.close()


def test_checks_compile_to_snowflake_sql() -> None:
    source = adapter()
    try:
        grain = source._grain_sql(GrainCheck("RAW.PUBLIC.EVENTS", ["ID"]))
        join = source._join_sql(
            JoinCheck("EVENTS", ["USER_ID"], "USERS", ["ID"])
        )
        nulls = source._nullability_sql(NullabilityCheck("EVENTS", ["USER_ID"]))
        ordering = source._ordering_sql(
            OrderingCheck("EVENTS", "CREATED_AT", "UPDATED_AT")
        )
    finally:
        source.close()

    assert 'FROM "RAW"."PUBLIC"."EVENTS"' in grain
    assert "COUNT_IF" in grain
    assert "FILTER (WHERE" not in grain
    assert "COUNT_IF" in join
    assert "COUNT_IF" in nulls
    assert "COUNT_IF" in ordering


def test_large_tables_use_a_bounded_sample() -> None:
    source = adapter()
    try:
        relation, sampled, fraction = source._table_source(
            Table(
                schema_name="ANALYTICS.PUBLIC",
                name="EVENTS",
                columns=[],
                estimated_rows=2_000_000,
            )
        )
    finally:
        source.close()

    assert sampled is True
    assert fraction == 0.05
    assert relation.endswith("SAMPLE SYSTEM (5.000000)")


def test_row_estimates_are_keyed_by_the_name_the_inspector_reports() -> None:
    """INFORMATION_SCHEMA says ORDERS; the inspector says orders.

    Keyed as returned, every estimate lookup missed, so `estimated_rows` stayed
    None and profiling read that as a small table — sampling never engaged and a
    billion-row table got a full aggregate sweep.
    """
    source = adapter()
    try:
        assert source._normalize("ORDERS") == "orders"
        assert source._normalize("orders") == "orders"
    finally:
        source.close()


def test_large_table_is_sampled_when_the_estimate_comes_back_uppercase(monkeypatch) -> None:
    class Inspector:
        def get_table_names(self, schema):
            return ["events"]

        def get_view_names(self, schema):
            return []

        def get_pk_constraint(self, name, schema):
            return {"constrained_columns": []}

        def get_columns(self, name, schema):
            return [
                {"name": "id", "type": "NUMBER(38,0)", "nullable": False, "default": None}
            ]

        def get_table_comment(self, name, schema):
            return {"text": None}

        def get_foreign_keys(self, name, schema):
            return []

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return self

        def all(self):
            return self._rows

    class Connection:
        """Returns what Snowflake really returns: the stored, uppercase name."""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            return Result([{"table_name": "EVENTS", "row_count": 2_000_000}])

    source = adapter()
    monkeypatch.setattr("atlas.adapters.snowflake.inspect", lambda engine: Inspector())
    monkeypatch.setattr(source.engine, "connect", lambda: Connection())
    try:
        snapshot = source.extract_structure("ANALYTICS.PUBLIC")
        table = snapshot.tables[0]
        _, sampled, _ = source._table_source(table)
    finally:
        source.close()

    assert table.estimated_rows == 2_000_000, "uppercase estimate never reached the table"
    assert sampled is True, "a two-million-row table must not be swept in full"


def test_structure_reflection_marks_keys_declared_not_enforced(monkeypatch) -> None:
    class Inspector:
        def get_table_names(self, schema):
            assert schema == "ANALYTICS.PUBLIC"
            return ["orders"]

        def get_view_names(self, schema):
            return []

        def get_pk_constraint(self, name, schema):
            return {"constrained_columns": ["id"]}

        def get_columns(self, name, schema):
            return [
                {
                    "name": "id",
                    "type": "NUMBER(38,0)",
                    "nullable": False,
                    "default": None,
                    "comment": "Order identifier",
                },
                {
                    "name": "customer_id",
                    "type": "NUMBER(38,0)",
                    "nullable": False,
                    "default": None,
                    "comment": None,
                },
            ]

        def get_table_comment(self, name, schema):
            return {"text": "Customer orders"}

        def get_foreign_keys(self, name, schema):
            return [
                {
                    "name": "orders_customer_fk",
                    "constrained_columns": ["customer_id"],
                    "referred_table": "customers",
                    "referred_columns": ["id"],
                }
            ]

    source = adapter()
    monkeypatch.setattr("atlas.adapters.snowflake.inspect", lambda engine: Inspector())
    monkeypatch.setattr(source, "_row_estimates", lambda namespace: {"orders": 42})
    try:
        snapshot = source.extract_structure("ANALYTICS.PUBLIC")
    finally:
        source.close()

    assert snapshot.database == "ANALYTICS"
    assert snapshot.dialect == "snowflake"
    assert snapshot.tables[0].estimated_rows == 42
    assert snapshot.tables[0].primary_key == ["id"]
    assert snapshot.tables[0].foreign_keys[0].enforcement is Enforcement.DECLARED_NOT_ENFORCED
