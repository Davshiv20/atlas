from __future__ import annotations

import pytest
import yaml

from atlas.compile import render_markdown
from atlas.facts import Fact, FactStatus, FactStore, Provenance, ProvenanceKind
from atlas.metadata.yaml_store import YamlMetadataRepository
from atlas.output import SchemaOutput, build_output
from atlas.questions import Question
from atlas.snapshot import Column, ColumnProfile, ForeignKey, Snapshot, Table, ValueCount

CHECK = Provenance(
    kind=ProvenanceKind.GROUNDED_CHECK, detail="executed: SELECT count(*) FROM orders", result="pass"
)
GUESS = Provenance(kind=ProvenanceKind.LLM_INFERENCE, detail="from column name")


def fact(
    subject: str,
    aspect: str,
    claim: str,
    confidence: float,
    grounded: bool = True,
    discriminator: str | None = None,
) -> Fact:
    return Fact(
        subject=subject,
        aspect=aspect,
        claim=claim,
        confidence=confidence,
        provenance=[GUESS, CHECK] if grounded else [GUESS],
        discriminator=discriminator,
    )


@pytest.fixture
def snapshot() -> Snapshot:
    orders = Table(
        schema_name="public",
        name="orders",
        columns=[
            Column(
                name="status",
                data_type="VARCHAR",
                nullable=False,
                profile=ColumnProfile(
                    distinct_count=2,
                    top_values=[
                        ValueCount(value="open", count=9),
                        ValueCount(value="paid", count=3),
                    ],
                ),
            ),
            Column(
                name="customer_email",
                data_type="VARCHAR",
                nullable=True,
                profile=ColumnProfile(
                    null_fraction=0.1,
                    values_withheld_reason="column name matches sensitive-data pattern",
                ),
            ),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKey(
                name="fk", columns=["customer_id"], referred_table="customers", referred_columns=["id"]
            )
        ],
        exact_rows=12,
    )
    dormant = Table(schema_name="public", name="audit_log", columns=[], estimated_rows=4)
    return Snapshot(
        database="shop", schema_name="public", dialect="postgresql", tables=[orders, dormant]
    )


@pytest.fixture
def store() -> FactStore:
    return FactStore(
        facts=[
            fact("orders", "grain", "One row per customer order.", 0.92),
            fact("orders", "semantics", "Orders placed through the storefront.", 0.75),
            fact("orders.status", "semantics", "Order lifecycle state.", 0.45, grounded=False),
            fact(
                "orders.status",
                "quality",
                "No CHECK constraint enforces the value set.",
                0.75,
                discriminator="unconstrained-enum",
            ),
        ]
    )


@pytest.fixture
def document(snapshot, store) -> SchemaOutput:
    return build_output(snapshot, store, [])


def table(document: SchemaOutput, name: str):
    return next(t for t in document.tables if t.name == name)


# --- structure -------------------------------------------------------------


def test_grain_is_a_first_class_field_with_confidence(document) -> None:
    grain = table(document, "orders").grain
    assert grain is not None
    assert grain.text == "One row per customer order."
    assert grain.confidence == 0.92
    assert grain.grounded is True
    assert grain.evidence == "SELECT count(*) FROM orders"


def test_grain_absent_rather_than_guessed(document) -> None:
    assert table(document, "audit_log").grain is None


def test_column_carries_shape_samples_and_description(document) -> None:
    status = next(c for c in table(document, "orders").columns if c.name == "status")
    assert status.distinct_count == 2
    assert [v.value for v in status.sample_values] == ["open", "paid"]
    assert status.description.text == "Order lifecycle state."
    assert status.description.grounded is False


def test_withheld_columns_expose_the_reason_not_the_values(document) -> None:
    email = next(c for c in table(document, "orders").columns if c.name == "customer_email")
    assert email.sample_values is None
    assert "sensitive-data pattern" in email.values_withheld_reason


def test_secondary_claims_become_notes_rather_than_being_dropped(document) -> None:
    status = next(c for c in table(document, "orders").columns if c.name == "status")
    assert [n.text for n in status.notes] == ["No CHECK constraint enforces the value set."]


def test_declared_and_inferred_joins_are_distinguished(snapshot) -> None:
    store = FactStore(
        facts=[
            fact(
                "orders",
                "join",
                "Also joins shipments by reference.",
                0.75,
                discriminator="shipments",
            )
        ]
    )
    joins = table(build_output(snapshot, store, []), "orders").joins
    assert [j.enforced for j in joins] == [True, False]
    assert joins[0].referred_table == "customers"
    assert joins[1].description.text == "Also joins shipments by reference."


def test_estimated_row_counts_are_flagged(document) -> None:
    assert table(document, "orders").row_count_is_exact is True
    assert table(document, "audit_log").row_count_is_exact is False


def test_questions_attach_to_their_table(snapshot, store) -> None:
    question = Question(
        subject="orders.status", question="Closed set?", evidence="2 values", table="orders"
    )
    document = build_output(snapshot, store, [question])
    assert table(document, "orders").open_questions == ["Closed set?"]


def test_analyzed_tables_sort_first(document) -> None:
    assert [t.name for t in document.tables] == ["orders", "audit_log"]


def test_the_exported_projection_is_the_document(document, tmp_path) -> None:
    """Only ever written out. Nothing in Atlas reads it back — the projection
    is rebuilt from the record per request — so what is asserted here is that
    the export is faithful, not that it is a source."""
    store = YamlMetadataRepository(tmp_path)
    store.write_output("demo", document)
    written = (tmp_path / "demo" / "output.yaml").read_text()
    assert SchemaOutput.model_validate(yaml.safe_load(written)) == document


# --- markdown view ---------------------------------------------------------


def test_markdown_leads_each_table_with_grain(document) -> None:
    assert "**Grain:** One row per customer order. [trust 92/100 · checked]" in render_markdown(document)


def test_markdown_warns_when_grain_is_unknown(document) -> None:
    assert "do not assume one row per entity" in render_markdown(document)


def test_markdown_separates_checked_from_guessed(document) -> None:
    text = render_markdown(document)
    assert "Orders placed through the storefront. [trust 75/100 · checked]" in text
    assert "Order lifecycle state. [trust 45/100 · unsupported]" in text


def test_markdown_marks_enforced_joins(document) -> None:
    assert "`customers(id)` [enforced]" in render_markdown(document)


def test_validated_claims_keep_their_trust_score(snapshot) -> None:
    """Validation and trust answer different questions, so show both."""
    verified = fact("orders", "grain", "One row per order.", 0.92)
    verified = verified.model_copy(update={"status": FactStatus.VERIFIED})
    document = build_output(snapshot, FactStore(facts=[verified]), [])
    assert (
        "**Grain:** One row per order. [trust 92/100 · checked · validated]"
        in render_markdown(document)
    )


def test_markdown_never_leaks_withheld_values(document) -> None:
    assert "@" not in render_markdown(document)


def test_markdown_limit_truncates(document) -> None:
    assert "audit_log" not in render_markdown(document, limit=1)
