from __future__ import annotations

from datetime import UTC, datetime

from atlas.classify import Consequence
from atlas.facts import FactStatus
from atlas.output import Claim, ColumnOutput, JoinOutput, SchemaOutput, TableOutput
from atlas.semantic_view import build_semantic_view, render_yaml


def claim(text: str, status: FactStatus = FactStatus.VERIFIED, **kw) -> Claim:
    kw.setdefault("id", f"subject#{abs(hash(text)) % 10_000}")
    return Claim(text=text, confidence=0.9, status=status, grounded=True, **kw)


def column(name: str, column_class: str = "categorical", **kw) -> ColumnOutput:
    return ColumnOutput(
        name=name,
        column_class=column_class,
        consequence=Consequence.HIGH,
        data_type=kw.pop("data_type", "VARCHAR"),
        nullable=kw.pop("nullable", True),
        **kw,
    )


def table(**kw) -> TableOutput:
    return TableOutput(
        name=kw.pop("name", "clients"),
        qualified_name="public.clients",
        row_count=kw.pop("row_count", 25),
        row_count_is_exact=True,
        analyzed=kw.pop("analyzed", True),
        **kw,
    )


def view_of(*tables: TableOutput):
    return build_semantic_view(
        SchemaOutput(
            database="db",
            schema_name="public",
            captured_at=datetime.now(UTC),
            table_count=len(tables),
            claim_count=0,
            checked_claim_count=0,
            question_count=0,
            tables=list(tables),
        )
    )


def test_an_undescribed_column_is_excluded_with_its_reason() -> None:
    """Dropping it silently reads as "this column does not exist", and an agent
    told a table has one column when it has two will invent the rest."""
    view = view_of(
        table(
            grain=claim("one row per client"),
            columns=[column("name", description=claim("Display name.")), column("mystery")],
        )
    )
    dimensions = [d.name for d in view.tables[0].dimensions]
    excluded = {e.name: e.reason for e in view.tables[0].excluded}

    assert dimensions == ["name"]
    assert excluded == {"mystery": "no established meaning yet"}


def test_a_shape_determined_column_says_so_rather_than_looking_unfinished() -> None:
    """"no established meaning" about `created_at` implies outstanding work,
    when no claim was ever going to be made about it."""
    view = view_of(
        table(
            grain=claim("one row per client"),
            columns=[
                column("id", "primary_key", is_primary_key=True),
                column("created_at", "audit_timestamp"),
                column("created_by", "foreign_key"),
            ],
        )
    )
    reasons = {e.name: e.reason for e in view.tables[0].excluded}

    assert reasons["id"] == "surrogate key"
    assert reasons["created_at"] == "audit timestamp"
    assert "relationships" in reasons["created_by"]


def test_an_empty_column_is_excluded_before_anything_else_is_considered() -> None:
    view = view_of(
        table(
            grain=claim("one row per client"),
            columns=[column("logo_url", description=claim("A logo URL."), null_fraction=1.0)],
        )
    )
    assert [e.reason for e in view.tables[0].excluded] == ["100% null"]


def test_an_unreviewed_table_is_not_emittable() -> None:
    """The grain is the dangerous one: an agent with it wrong writes silently
    double-counting joins."""
    unsettled = view_of(
        table(
            grain=claim("one row per client", FactStatus.UNVERIFIED),
            columns=[column("name", description=claim("Display name."))],
        )
    )
    assert unsettled.ready == []

    settled = view_of(
        table(grain=claim("one row per client"), columns=[column("name", description=claim("N."))])
    )
    assert len(settled.ready) == 1


def test_review_state_is_visible_in_the_rendered_view() -> None:
    """A reader has to be able to tell the lines a person stood behind from the
    ones that are still the model's."""
    view = view_of(
        table(
            grain=claim("one row per client"),
            description=claim("Client organisations.", reviewer="s.hale"),
            columns=[
                column("name", description=claim("Display name.", FactStatus.UNVERIFIED)),
                column("tier", description=claim("Service tier.")),
            ],
        )
    )
    rendered = render_yaml(view)

    assert "# approved by s.hale" in rendered
    pending = [line for line in rendered.splitlines() if "pending review" in line]
    assert len(pending) == 1  # only the unreviewed one


def test_a_relationship_says_whether_the_database_enforces_it() -> None:
    view = view_of(
        table(
            grain=claim("one row per client"),
            columns=[column("name", description=claim("N."))],
            joins=[
                JoinOutput(columns=["created_by"], referred_table="users",
                           referred_columns=["id"], enforced=True),
                JoinOutput(columns=["owner_id"], referred_table="teams",
                           referred_columns=["id"], enforced=False),
            ],
        )
    )
    rendered = render_yaml(view)

    assert "to: users" in rendered and "to: teams" in rendered
    assert rendered.count("# verified by check, not enforced") == 1


def test_a_table_nobody_analysed_appears_but_establishes_nothing() -> None:
    """It used to be omitted. That hid the table's existence.

    An agent reading the view could not distinguish a table Atlas has nothing
    to say about from a table that is not in the database. The first is a gap to
    route around; the second is a fact about the schema, and silently conflating
    them is the opposite of keeping unknown meaning explicit.

    The cost is real — a column list with no meanings is close to the schema an
    agent could already read — so the entry must never look settled. `ready`
    stays false while grain is missing, and no meaning is invented.
    """
    view = view_of(table(analyzed=False, columns=[column("name")]))

    assert [entry.name for entry in view.tables] == ["clients"]
    entry = view.tables[0]
    assert entry.grain is None, "no grain was established, and none may be implied"
    assert entry.description is None
    assert entry.dimensions == [], "a column with no meaning is not a dimension"
    # The column is named and its absence of meaning is stated, rather than the
    # column being silently dropped.
    assert [(x.name, x.reason) for x in entry.excluded] == [
        ("name", "no established meaning yet")
    ]


def test_a_dimension_carries_the_description_it_was_given() -> None:
    """A column list without meanings hands an agent the schema it already had
    — the descriptions are the reason this file exists."""
    view = view_of(
        table(
            grain=claim("one row per client"),
            columns=[
                column(
                    "tier",
                    description=claim("Commercial tier; drives billing rate."),
                    nullable=False,
                )
            ],
        )
    )
    import yaml

    parsed = yaml.safe_load(render_yaml(view))
    dimension = parsed["tables"][0]["dimensions"][0]

    assert view.tables[0].dimensions[0].description == "Commercial tier; drives billing rate."
    assert dimension["description"] == "Commercial tier; drives billing rate."
    assert dimension["nullable"] is False


def test_the_emitted_document_is_valid_yaml() -> None:
    """It did not parse. A grain reading "...enforces this grain: 538 rows"
    put a colon in a hand-written plain scalar and broke the whole file — and
    the file is the product."""
    import yaml

    view = view_of(
        table(
            grain=claim("the composite key enforces this grain: 538 rows, 538 distinct"),
            description=claim("Contains: colons, #hashes, 'quotes' and - dashes."),
            columns=[column("tier", description=claim("A value: with a colon."))],
        )
    )
    parsed = yaml.safe_load(render_yaml(view))
    assert parsed["tables"][0]["grain"].startswith("the composite key")


def test_every_description_survives_the_round_trip() -> None:
    """Folding is for reading; the text an agent parses back must be the text a
    reviewer approved, unchanged."""
    import yaml

    long_text = (
        "The user's email address; serves as the unique login identifier for "
        "credential-based authentication, enforced by a unique index across all "
        "3 rows, and never reused after deletion."
    )
    view = view_of(
        table(
            grain=claim("one row per client"),
            columns=[column("email", description=claim(long_text))],
        )
    )
    parsed = yaml.safe_load(render_yaml(view))
    assert parsed["tables"][0]["dimensions"][0]["description"] == long_text
