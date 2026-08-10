from __future__ import annotations

import time

import pytest

from atlas.adapters.base import CheckObservation, DatabaseAdapter
from atlas.agent import AnalysisSink, build_tools, render_table
from atlas.facts import FactStatus
from atlas.snapshot import Column, ColumnProfile, Snapshot, Table, ValueCount


class ScriptedAdapter(DatabaseAdapter):
    """Returns a canned measurement per check type, so the agent's tool surface
    is tested without a database."""

    def __init__(self, results: dict[str, CheckObservation] | None = None) -> None:
        self.results = results or {}
        self.calls: list[object] = []

    def test_connection(self) -> None: ...
    def probe(self, namespace): ...
    def extract_structure(self, namespace): ...
    def profile(self, snapshot): ...
    def close(self) -> None: ...

    def execute_check(self, check) -> CheckObservation:
        self.calls.append(check)
        return self.results.get(
            check.type, CheckObservation(check_type=check.type, error="not scripted")
        )


def make_snapshot() -> Snapshot:
    table = Table(
        schema_name="public",
        name="deliverables",
        columns=[
            Column(
                name="id",
                data_type="VARCHAR",
                nullable=False,
                is_primary_key=True,
                profile=ColumnProfile(distinct_count=111),
            ),
            Column(
                name="stage",
                data_type="VARCHAR",
                nullable=False,
                profile=ColumnProfile(
                    null_fraction=0.0,
                    distinct_count=5,
                    top_values=[ValueCount(value="S0", count=22)],
                ),
            ),
        ],
        primary_key=["id"],
        exact_rows=111,
    )
    return Snapshot(database="elara", schema_name="public", dialect="postgresql", tables=[table])


def wire(observations: dict[str, CheckObservation]):
    snapshot = make_snapshot()
    adapter = ScriptedAdapter(observations)
    sink = AnalysisSink()
    tools = {t.name: t for t in build_tools(adapter, snapshot, sink)}
    return tools, sink, adapter


CLEAN_GRAIN = CheckObservation(
    check_type="grain",
    observations={"total": 111, "distinct_keys": 111, "null_keys": 0},
    complete_scan=True,
    rows_examined=111,
    sql="SELECT …",
)


@pytest.fixture
def wired():
    return wire({"grain": CLEAN_GRAIN})


def evidence_id_from(result: str) -> str:
    return result.split(" ")[0]


# --- the agent cannot write SQL --------------------------------------------


def test_there_is_no_generic_sql_tool(wired) -> None:
    """The point of the adapter layer: arbitrary SQL is not expressible, so
    `SELECT substr(email, 1, 4)` cannot be written at all."""
    tools, _, _ = wired
    assert "run_query" not in tools
    assert set(tools) == {
        "run_grain_check",
        "run_join_check",
        "run_distribution_check",
        "run_ordering_check",
        "describe_table",
        "record_claim",
        "ask_human",
    }


def test_a_check_returns_an_evidence_id(wired) -> None:
    tools, sink, _ = wired
    result = tools["run_grain_check"].call({"relation": "deliverables", "key_fields": ["id"]})
    assert result.startswith("evidence:")
    assert len(sink.evidence.records) == 1


def test_the_agent_proposes_parameters_not_sql(wired) -> None:
    tools, _, adapter = wired
    tools["run_grain_check"].call({"relation": "deliverables", "key_fields": ["id"]})
    assert adapter.calls[0].key_fields == ["id"]


# --- claims must cite evidence that exists ---------------------------------


def test_a_fabricated_evidence_id_is_refused(wired) -> None:
    tools, sink, _ = wired
    result = tools["record_claim"].call(
        {
            "subject": "deliverables",
            "aspect": "grain",
            "claim": "One row per deliverable.",
            "evidence_ids": ["evidence:madeup"],
        }
    )
    assert "REJECTED" in result
    assert sink.facts == []


def test_a_consequential_claim_cannot_be_recorded_without_evidence(wired) -> None:
    """The escape hatch that produced 76 unbacked claims: the agent could
    previously drop the citation and save the claim anyway."""
    tools, sink, _ = wired
    result = tools["record_claim"].call(
        {
            "subject": "deliverables",
            "aspect": "grain",
            "claim": "One row per deliverable.",
            "evidence_ids": [],
        }
    )
    assert "REJECTED" in result
    assert "without supporting evidence" in result
    assert sink.facts == []


def test_a_grounded_claim_is_recorded_with_a_computed_confidence(wired) -> None:
    tools, sink, _ = wired
    evidence = evidence_id_from(
        tools["run_grain_check"].call({"relation": "deliverables", "key_fields": ["id"]})
    )
    result = tools["record_claim"].call(
        {
            "subject": "deliverables",
            "aspect": "grain",
            "claim": "One row per deliverable.",
            "evidence_ids": [evidence],
        }
    )
    assert "Recorded" in result
    fact = sink.facts[0]
    assert fact.confidence > 0.8  # computed from the evidence, not chosen
    assert sink.evidence.for_claim(fact.id)


def test_the_model_cannot_choose_its_own_confidence(wired) -> None:
    tools, _, _ = wired
    assert "confidence" not in tools["record_claim"].parameters["properties"]


def test_a_failed_check_links_as_contradicting() -> None:
    """A check that ran and failed is kept and surfaced, not dropped."""
    tools, sink, _ = wire(
        {
            "grain": CheckObservation(
                check_type="grain",
                observations={"total": 111, "distinct_keys": 90, "null_keys": 0},
                complete_scan=True,
                sql="SELECT …",
            )
        }
    )
    evidence = evidence_id_from(
        tools["run_grain_check"].call({"relation": "deliverables", "key_fields": ["id"]})
    )
    tools["record_claim"].call(
        {
            "subject": "deliverables",
            "aspect": "grain",
            "claim": "One row per deliverable.",
            "evidence_ids": [evidence],
        }
    )
    assert len(sink.evidence.contradictions("deliverables#grain")) == 1


def test_questions_remain_available_when_no_check_can_settle_it(wired) -> None:
    tools, sink, _ = wired
    tools["ask_human"].call(
        {
            "subject": "deliverables.process_id_ref",
            "question": "One identifier space or several?",
            "evidence": "P1, PR-02, P001, Inc-1",
        }
    )
    assert sink.questions[0].subject == "deliverables.process_id_ref"


def test_routine_claims_are_auto_accepted_when_grounded(wired) -> None:
    tools, sink, _ = wired
    evidence = evidence_id_from(
        tools["run_grain_check"].call({"relation": "deliverables", "key_fields": ["id"]})
    )
    tools["record_claim"].call(
        {
            "subject": "deliverables.id",
            "aspect": "semantics",
            "claim": "Surrogate primary key.",
            "evidence_ids": [evidence],
        }
    )
    assert sink.facts[0].status is FactStatus.AUTO_ACCEPTED


# --- rendering -------------------------------------------------------------


def test_render_shows_values() -> None:
    snapshot = make_snapshot()
    rendered = render_table(snapshot, snapshot.table("deliverables"))
    assert "values: S0(22)" in rendered
    assert "111 rows" in rendered


def test_render_separates_shape_determined_columns() -> None:
    snapshot = make_snapshot()
    rendered = render_table(snapshot, snapshot.table("deliverables"))
    describe, routine = rendered.split("SHAPE-DETERMINED COLUMNS")
    assert "stage" in describe.split("COLUMNS TO DESCRIBE")[1]
    assert "id" in routine
    assert "do not record claims for these" in rendered


# --- a truncated reading says so -------------------------------------------


def test_the_turn_ceiling_is_reported_not_swallowed(monkeypatch) -> None:
    """A run cut off mid-way looks identical to a thorough one from outside.
    One observed run spent every turn on checks and recorded no claims, and the
    job still reported success."""
    from atlas import agent

    monkeypatch.setattr(agent, "run_tool_loop", lambda *a, **k: True)
    sink = agent.analyze_table(object(), ScriptedAdapter(), make_snapshot(), make_snapshot().table("deliverables"))
    assert sink.truncated is True


def test_a_completed_reading_is_not_marked_partial(monkeypatch) -> None:
    from atlas import agent

    monkeypatch.setattr(agent, "run_tool_loop", lambda *a, **k: False)
    sink = agent.analyze_table(object(), ScriptedAdapter(), make_snapshot(), make_snapshot().table("deliverables"))
    assert sink.truncated is False


def test_progress_callbacks_name_the_table_under_way(monkeypatch) -> None:
    from atlas import agent

    monkeypatch.setattr(agent, "run_tool_loop", lambda *a, **k: True)
    started: list[str] = []
    done: list[tuple[str, bool]] = []
    agent.analyze_schema(
        ScriptedAdapter(),
        make_snapshot(),
        client=object(),
        tables=["deliverables"],
        on_table_start=started.append,
        # The sink comes back so the caller can persist this table now rather
        # than holding it in memory until the whole run ends.
        on_table_done=lambda name, sink: done.append((name, sink.truncated)),
    )
    assert started == ["deliverables"]
    assert done == [("deliverables", True)]


# --- evidence must be about the claim's subject ----------------------------


DISTRIBUTION = CheckObservation(
    check_type="distribution",
    observations={"values": [{"value": "S0", "count": 22}]},
    complete_scan=True,
    rows_examined=111,
    sql="SELECT …",
)


def test_evidence_about_another_column_is_refused() -> None:
    """A claim about `password_hash` could cite the distribution of `role` and
    be scored on it: existence was the only thing checked."""
    tools, sink, _ = wire({"distribution": DISTRIBUTION})
    evidence = evidence_id_from(
        tools["run_distribution_check"].call({"relation": "deliverables", "field": "stage"})
    )
    result = tools["record_claim"].call(
        {
            "subject": "deliverables.id",
            "aspect": "semantics",
            "claim": "Surrogate primary key.",
            "evidence_ids": [evidence],
        }
    )
    assert "REJECTED" in result
    assert "deliverables.stage" in result
    assert sink.facts == []


def test_a_distribution_supports_a_claim_about_its_own_column() -> None:
    """The observation is the right evidence for what the column contains, and
    it was being filed as contradicting the claim it was run to inform."""
    tools, sink, _ = wire({"distribution": DISTRIBUTION})
    evidence = evidence_id_from(
        tools["run_distribution_check"].call({"relation": "deliverables", "field": "stage"})
    )
    result = tools["record_claim"].call(
        {
            "subject": "deliverables.stage",
            "aspect": "semantics",
            "claim": "The workflow stage of the deliverable.",
            "evidence_ids": [evidence],
        }
    )
    assert "Recorded" in result
    fact = sink.facts[0]
    assert fact.confidence == 0.65  # observed, capped there for business meaning
    assert sink.evidence.contradictions(fact.id) == []


def test_relation_scoped_evidence_still_bears_on_a_column() -> None:
    """A grain check names no field, so it is not evidence *against* a column
    claim — the relevance rule must not reject it."""
    tools, sink, _ = wire({"grain": CLEAN_GRAIN})
    evidence = evidence_id_from(
        tools["run_grain_check"].call({"relation": "deliverables", "key_fields": ["id"]})
    )
    result = tools["record_claim"].call(
        {
            "subject": "deliverables.id",
            "aspect": "semantics",
            "claim": "Surrogate primary key.",
            "evidence_ids": [evidence],
        }
    )
    assert "Recorded" in result
    assert sink.facts[0].confidence > 0.6


# --- tables are read concurrently ------------------------------------------


def _snapshot_of(*names: str) -> Snapshot:
    return Snapshot(
        database="db",
        schema_name="public",
        dialect="postgresql",
        tables=[
            Table(
                schema_name="public",
                name=name,
                columns=[Column(name="id", data_type="VARCHAR", nullable=False)],
                primary_key=["id"],
                exact_rows=1,
            )
            for name in names
        ],
    )


def test_tables_are_read_at_the_same_time(monkeypatch) -> None:
    """The point of the change: five tables at five minutes each took
    twenty-five minutes, and nothing about one table depends on another."""
    import threading

    from atlas import agent

    started = threading.Barrier(3, timeout=5)

    def blocking(*args, **kwargs):
        # Only completes if three workers reach it together.
        started.wait()
        return False

    monkeypatch.setattr(agent, "run_tool_loop", blocking)
    agent.analyze_schema(
        ScriptedAdapter(),
        _snapshot_of("a", "b", "c"),
        client=object(),
        workers=3,
    )  # a timeout here means the reads were serialised


def test_the_worker_count_is_bounded(monkeypatch) -> None:
    """Every worker issues checks against a database Atlas does not own."""
    import threading

    from atlas import agent

    live = 0
    peak = 0
    guard = threading.Lock()

    def counting(*args, **kwargs):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with guard:
            live -= 1
        return False

    monkeypatch.setattr(agent, "run_tool_loop", counting)
    agent.analyze_schema(
        ScriptedAdapter(), _snapshot_of(*"abcdefgh"), client=object(), workers=2
    )

    assert peak <= 2


def test_results_do_not_depend_on_which_worker_finishes_first(monkeypatch) -> None:
    """Folding sinks in completion order makes a run's output a race."""
    from atlas import agent

    order: list[str] = []

    def slow_for_the_first(client, system, user, tools, on_text=None):
        name = user.split()[1].split(".")[-1]
        time.sleep(0.05 if name == "a" else 0.0)
        order.append(name)
        return False

    monkeypatch.setattr(agent, "run_tool_loop", slow_for_the_first)
    done: list[str] = []
    agent.analyze_schema(
        ScriptedAdapter(),
        _snapshot_of("a", "b", "c"),
        client=object(),
        workers=3,
        on_table_done=lambda name, sink: done.append(name),
    )

    # `a` finishes last but was selected first; questions fold in selection
    # order regardless.
    assert order[-1] == "a"
    assert set(done) == {"a", "b", "c"}


def test_persistence_is_never_concurrent(monkeypatch) -> None:
    """The workspace rewrites whole files. Two workers landing together drop
    one of them, which is why the callbacks hold a lock."""
    import threading

    from atlas import agent

    overlapping = False
    inside = 0
    guard = threading.Lock()

    def absorbing(name, sink) -> None:
        nonlocal overlapping, inside
        with guard:
            inside += 1
            if inside > 1:
                overlapping = True
        time.sleep(0.01)
        with guard:
            inside -= 1

    monkeypatch.setattr(agent, "run_tool_loop", lambda *a, **k: False)
    agent.analyze_schema(
        ScriptedAdapter(),
        _snapshot_of(*"abcdef"),
        client=object(),
        workers=6,
        on_table_done=absorbing,
    )

    assert not overlapping
