from __future__ import annotations

import pytest

from atlas.adapters.base import (
    CheckObservation,
    DatabaseAdapter,
    DistributionCheck,
    GrainCheck,
    JoinCheck,
    OrderingCheck,
)
from atlas.checks import run_check
from atlas.evidence import Verdict


class FakeAdapter(DatabaseAdapter):
    """Returns canned measurements, so the verdict logic is tested without a
    database and independently of any dialect."""

    def __init__(self, observation: CheckObservation) -> None:
        self.observation = observation

    def test_connection(self) -> None: ...
    def probe(self, namespace): ...
    def extract_structure(self, namespace): ...
    def profile(self, snapshot): ...
    def close(self) -> None: ...

    def execute_check(self, check) -> CheckObservation:
        return self.observation


def observe(check_type: str, **observations) -> CheckObservation:
    return CheckObservation(
        check_type=check_type,
        observations=observations,
        complete_scan=True,
        rows_examined=observations.get("total") or observations.get("source_rows"),
        sql="SELECT 1",
    )


def evaluate(check, observation):
    return run_check(FakeAdapter(observation), check, database="test")


# --- grain -----------------------------------------------------------------


def test_clean_grain_passes() -> None:
    check = GrainCheck(relation="public.conversations", key_fields=["id"])
    record, message = evaluate(check, observe("grain", total=35, distinct_keys=35, null_keys=0))
    assert record.verdict is Verdict.PASSED
    assert "35 rows" in message


def test_duplicate_keys_fail_the_grain() -> None:
    check = GrainCheck(relation="orders", key_fields=["id"])
    record, message = evaluate(check, observe("grain", total=100, distinct_keys=97, null_keys=0))
    assert record.verdict is Verdict.FAILED
    assert "3 duplicate keys" in message


def test_null_keys_fail_before_duplicates_are_considered() -> None:
    check = GrainCheck(relation="orders", key_fields=["id"])
    record, _ = evaluate(check, observe("grain", total=100, distinct_keys=100, null_keys=2))
    assert record.verdict is Verdict.FAILED


def test_an_empty_table_is_inconclusive_not_passed() -> None:
    """Every grain holds vacuously over zero rows, which is not the same as
    having established one."""
    check = GrainCheck(relation="empty", key_fields=["id"])
    record, message = evaluate(check, observe("grain", total=0, distinct_keys=0, null_keys=0))
    assert record.verdict is Verdict.INCONCLUSIVE
    assert "empty" in message


# --- join ------------------------------------------------------------------


def test_full_coverage_passes() -> None:
    check = JoinCheck(
        source_relation="messages", source_fields=["conversation_id"],
        target_relation="conversations", target_fields=["id"],
    )
    record, _ = evaluate(check, observe("join", source_rows=2798, matched_rows=2798, orphan_rows=0))
    assert record.verdict is Verdict.PASSED
    assert record.observation["match_rate"] == 1.0


def test_a_trickle_of_orphans_warns_rather_than_passing_clean() -> None:
    """180 orphans in 500k rows is a real relationship with dirty edges — not a
    guaranteed foreign key, and not a broken join either."""
    check = JoinCheck(
        source_relation="invoices", source_fields=["customer_id"],
        target_relation="customers", target_fields=["id"],
    )
    record, message = evaluate(
        check, observe("join", source_rows=500_000, matched_rows=499_820, orphan_rows=180)
    )
    assert record.verdict is Verdict.PASSED_WITH_WARNING
    assert "0.0360%" in message


def test_a_null_key_is_not_an_orphan() -> None:
    """A row that references nothing is not a broken reference — SQL's own
    MATCH SIMPLE rule. Counting nulls as orphans refuted four declared,
    enforced foreign keys in one schema purely because the column was unused."""
    check = JoinCheck(
        source_relation="engagements", source_fields=["updated_by"],
        target_relation="users", target_fields=["id"],
    )
    record, message = evaluate(
        check,
        observe("join", source_rows=26, null_keys=24, matched_rows=2, orphan_rows=0),
    )
    assert record.verdict is Verdict.PASSED
    assert "all 2 keyed rows matched" in message
    assert "24 of 26 rows have no reference" in message
    assert record.observation["orphan_rate"] == 0.0


def test_a_column_that_is_entirely_null_settles_nothing() -> None:
    """`clients.updated_by` is null in all 25 rows. The relationship is neither
    confirmed nor refuted — there is no data to match."""
    check = JoinCheck(
        source_relation="clients", source_fields=["updated_by"],
        target_relation="users", target_fields=["id"],
    )
    record, message = evaluate(
        check, observe("join", source_rows=25, null_keys=25, matched_rows=0, orphan_rows=0)
    )
    assert record.verdict is Verdict.INCONCLUSIVE
    assert "nothing to match" in message
    assert not record.bears_on_claim


def test_the_orphan_rate_is_measured_against_keyed_rows_only() -> None:
    """Diluting the rate with nulls hides a genuinely broken relationship: 5
    orphans among 10 real references is 50%, not 5%."""
    check = JoinCheck(
        source_relation="a", source_fields=["b_id"], target_relation="b", target_fields=["id"]
    )
    record, message = evaluate(
        check, observe("join", source_rows=100, null_keys=90, matched_rows=5, orphan_rows=5)
    )
    assert record.verdict is Verdict.FAILED
    assert "50.00%" in message


def test_a_high_orphan_rate_fails() -> None:
    check = JoinCheck(
        source_relation="a", source_fields=["b_id"], target_relation="b", target_fields=["id"]
    )
    record, _ = evaluate(check, observe("join", source_rows=1000, matched_rows=900, orphan_rows=100))
    assert record.verdict is Verdict.FAILED


# --- observations are not verifications ------------------------------------


def test_a_distribution_cannot_pass() -> None:
    """A profile observes; it does not test. Recording it as PASSED would let a
    value distribution masquerade as a verification."""
    check = DistributionCheck(relation="orders", field="status")
    record, _ = evaluate(check, observe("distribution", values=[]))
    assert record.verdict is Verdict.OBSERVED
    assert not record.is_verification


def test_a_distribution_is_an_observation_not_a_failed_conclusion() -> None:
    """OBSERVED and INCONCLUSIVE were one value, so a distribution reporting
    exactly the values a claim described was filed as contradicting it."""
    check = DistributionCheck(relation="orders", field="status")
    record, _ = evaluate(check, observe("distribution", values=[]))
    assert record.is_observation
    assert record.bears_on_claim


def test_an_empty_table_still_settles_nothing() -> None:
    """The other half of the split: an assertion existed and could not be
    settled, which is not the same as having nothing to settle."""
    check = GrainCheck(relation="orders", key_fields=["id"])
    record, _ = evaluate(check, observe("grain", total=0, distinct_keys=0, null_keys=0))
    assert record.verdict is Verdict.INCONCLUSIVE
    assert not record.is_observation
    assert not record.bears_on_claim


# --- the record itself -----------------------------------------------------


def test_the_assertion_is_recorded_with_the_result() -> None:
    check = GrainCheck(relation="orders", key_fields=["id"])
    record, _ = evaluate(check, observe("grain", total=10, distinct_keys=10, null_keys=0))
    assert "One row of orders per distinct (id)" in record.assertion.description
    assert record.assertion.conditions["null_keys"] == {"equals": 0}


def test_the_hypothesis_is_stored_so_wrong_parameters_are_detectable() -> None:
    check = GrainCheck(relation="orders", key_fields=["tenant_id"])
    record, _ = evaluate(check, observe("grain", total=10, distinct_keys=10, null_keys=0))
    assert record.hypothesis["key_fields"] == ["tenant_id"]


def test_limitations_name_what_the_check_does_not_establish() -> None:
    check = JoinCheck(
        source_relation="a", source_fields=["b_id"], target_relation="b", target_fields=["id"]
    )
    record, _ = evaluate(check, observe("join", source_rows=10, matched_rows=10, orphan_rows=0))
    joined = " ".join(record.limitations)
    assert "business meaning" in joined
    assert "mandatory by design" in joined


def test_a_check_that_could_not_run_produces_no_evidence() -> None:
    """A failed execution is not evidence that anything is wrong."""
    check = GrainCheck(relation="missing", key_fields=["id"])
    record, message = evaluate(
        check, CheckObservation(check_type="grain", error="relation does not exist")
    )
    assert record is None
    assert "could not run" in message


def test_ordering_violations_fail() -> None:
    check = OrderingCheck(relation="users", earlier_field="created_at", later_field="updated_at")
    record, _ = evaluate(check, observe("ordering", total=100, violations=3, incomparable=0))
    assert record.verdict is Verdict.FAILED


@pytest.mark.parametrize("rows", [0, 1])
def test_evidence_ids_are_stable_for_the_same_measurement(rows) -> None:
    check = GrainCheck(relation="orders", key_fields=["id"])
    first, _ = evaluate(check, observe("grain", total=rows, distinct_keys=rows, null_keys=0))
    second, _ = evaluate(check, observe("grain", total=rows, distinct_keys=rows, null_keys=0))
    assert first.id == second.id
