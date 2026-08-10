"""Turning a measurement into evidence.

The adapter reports numbers. This decides what they mean — one place, so every
engine reaches the same verdict from the same observation. If each adapter
evaluated its own results, an orphan rate of 0.04% could be `passed` on
Postgres and `passed_with_warning` on Snowflake and nothing would surface the
disagreement.

Each check type declares its assertion *before* running: what would have made
it fail. That is the difference between evidence and a query that happened to
return rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from atlas.adapters.base import (
    Check,
    CheckObservation,
    DatabaseAdapter,
    DistributionCheck,
    GrainCheck,
    JoinCheck,
    NullabilityCheck,
    OrderingCheck,
)
from atlas.evidence import (
    Assertion,
    Authority,
    EvidenceRecord,
    EvidenceType,
    Execution,
    Freshness,
    Scope,
    Verdict,
)

# An orphan rate under this is a real relationship with dirty edges; above it,
# the relationship itself is in doubt. Named because the number is a judgement
# and ought to be arguable in one place rather than buried in a comparison.
ORPHAN_WARNING_RATE = 0.001


def assertion_for(check: Check) -> Assertion:
    """What this check would have to see to fail."""
    if isinstance(check, GrainCheck):
        keys = ", ".join(check.key_fields)
        return Assertion(
            description=f"One row of {check.relation} per distinct ({keys}).",
            conditions={
                "distinct_keys": {"equals_field": "total"},
                "null_keys": {"equals": 0},
            },
        )
    if isinstance(check, JoinCheck):
        fields = ", ".join(check.source_fields)
        return Assertion(
            description=(
                f"Every {check.source_relation} row with a non-null {fields} "
                f"matches a {check.target_relation} row."
            ),
            conditions={"orphan_rows": {"equals": 0}},
            warning_conditions={"orphan_rate": {"at_most": ORPHAN_WARNING_RATE}},
        )
    if isinstance(check, OrderingCheck):
        return Assertion(
            description=f"{check.later_field} never precedes {check.earlier_field}.",
            conditions={"violations": {"equals": 0}},
        )
    if isinstance(check, NullabilityCheck):
        return Assertion(
            description=f"Completeness of {', '.join(check.fields)} in {check.relation}.",
            conditions={},  # an observation, not a test
        )
    if isinstance(check, DistributionCheck):
        return Assertion(
            description=f"Observed values of {check.relation}.{check.field}.",
            conditions={},
        )
    raise TypeError(f"no assertion defined for {type(check).__name__}")


def _verdict(check: Check, observations: dict) -> tuple[Verdict, list[str]]:
    """Apply the assertion. Returns the verdict and what it turned on."""
    if isinstance(check, GrainCheck):
        total = observations.get("total", 0)
        distinct = observations.get("distinct_keys", 0)
        nulls = observations.get("null_keys", 0)
        if nulls:
            return Verdict.FAILED, [f"{nulls} rows have a null key"]
        if total != distinct:
            return Verdict.FAILED, [f"{total - distinct} duplicate keys across {total} rows"]
        if total == 0:
            # An empty table satisfies every grain vacuously, which is not the
            # same as having one.
            return Verdict.INCONCLUSIVE, ["table is empty"]
        return Verdict.PASSED, [f"{total} rows, {distinct} distinct keys, no nulls"]

    if isinstance(check, JoinCheck):
        source = observations.get("source_rows", 0)
        orphans = observations.get("orphan_rows", 0)
        # Rows whose key is null reference nothing, which SQL treats as
        # satisfying the constraint. They are excluded from the denominator
        # rather than counted against the relationship.
        nulls = observations.get("null_keys", 0)
        keyed = source - nulls
        if source == 0:
            return Verdict.INCONCLUSIVE, ["source table is empty"]
        if keyed == 0:
            # Nothing to test. Reporting this as a broken relationship is what
            # refuted four declared foreign keys that were merely unpopulated.
            return Verdict.INCONCLUSIVE, [
                f"every one of {source} rows has a null key — nothing to match"
            ]
        rate = orphans / keyed
        observations["keyed_rows"] = keyed
        observations["match_rate"] = round(1 - rate, 6)
        observations["orphan_rate"] = round(rate, 6)
        unused = f" ({nulls} of {source} rows have no reference)" if nulls else ""
        if orphans == 0:
            return Verdict.PASSED, [f"all {keyed} keyed rows matched{unused}"]
        if rate <= ORPHAN_WARNING_RATE:
            return Verdict.PASSED_WITH_WARNING, [
                f"{orphans} of {keyed} keyed rows ({rate:.4%}) have no match{unused}"
            ]
        return Verdict.FAILED, [
            f"{orphans} of {keyed} keyed rows ({rate:.2%}) have no match{unused}"
        ]

    if isinstance(check, OrderingCheck):
        violations = observations.get("violations", 0)
        total = observations.get("total", 0)
        if total == 0:
            return Verdict.INCONCLUSIVE, ["table is empty"]
        if violations:
            return Verdict.FAILED, [f"{violations} of {total} rows violate the ordering"]
        return Verdict.PASSED, [f"{total} rows, no violations"]

    # Profiles observe rather than test, so they cannot pass or fail. Recording
    # them as PASSED would let a distribution masquerade as a verification;
    # recording them as INCONCLUSIVE claimed the run had failed to settle
    # something, when there was nothing to settle.
    return Verdict.OBSERVED, ["observation only, no assertion"]


def _limitations(check: Check, scope: Scope) -> list[str]:
    notes: list[str] = []
    if not scope.is_durable:
        notes.append(
            "Sampled: establishes what was seen, not that no counterexample exists."
        )
    else:
        notes.append("Establishes structural consistency in the data as captured.")
    if isinstance(check, GrainCheck | JoinCheck):
        notes.append("Does not establish the business meaning of either relation.")
    if isinstance(check, JoinCheck):
        notes.append("Does not establish whether the relationship is mandatory by design.")
    return notes


def run_check(
    adapter: DatabaseAdapter,
    check: Check,
    database: str,
    snapshot_id: str | None = None,
) -> tuple[EvidenceRecord | None, str]:
    """Execute a check and turn it into evidence.

    Returns `(record, message)`. A failed *execution* yields no record and a
    message for the agent — a check that could not run is not evidence that
    anything is wrong.
    """
    observation: CheckObservation = adapter.execute_check(check)
    if not observation.succeeded:
        return None, f"check could not run: {observation.error}"

    scope = Scope(
        complete_scan=observation.complete_scan,
        sampled=observation.sampled,
        rows_examined=observation.rows_examined,
        sample_fraction=observation.sample_fraction,
    )
    verdict, reasons = _verdict(check, observation.observations)
    subjects = _subjects(check)

    record = EvidenceRecord(
        type=EvidenceType.DETERMINISTIC_CHECK,
        authority=Authority.MEASURED,
        subjects=subjects,
        assertion=assertion_for(check),
        observation=observation.observations,
        scope=scope,
        verdict=verdict,
        execution=Execution(
            database=database,
            dialect=adapter.dialect,
            sql=observation.sql,
            snapshot_id=snapshot_id,
        ),
        limitations=_limitations(check, scope) + observation.limitations,
        reasons=reasons,
        freshness=Freshness(
            valid_as_of=datetime.now(UTC),
            invalidated_by=[f"schema_change:{s}" for s in subjects],
        ),
        # Stored so a claim citing this can be checked for agreement: a grain
        # check run on the wrong keys passes, and without the hypothesis there
        # is no way to notice.
        hypothesis=_hypothesis(check),
    )
    return record, f"{verdict.value}: {'; '.join(reasons)}"


def _subjects(check: Check) -> list[str]:
    # The first relation is the one under test — see `primary_relation`. For a
    # join that is the source side: a refuted join is a fact about the table
    # holding the column, not about the table it failed to point at.
    if isinstance(check, JoinCheck):
        return [f"relation:{check.source_relation}", f"relation:{check.target_relation}"]
    if isinstance(check, DistributionCheck):
        return [f"relation:{check.relation}", f"field:{check.relation}.{check.field}"]
    return [f"relation:{check.relation}"]


def _hypothesis(check: Check) -> dict:
    return {k: v for k, v in vars(check).items() if k != "type"} | {"check_type": check.type}
