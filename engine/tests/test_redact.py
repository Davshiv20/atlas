from __future__ import annotations

import pytest

from atlas.redact import (
    all_values_are_opaque_ids,
    redact_result_value,
    value_withholding_reason,
)


@pytest.mark.parametrize(
    ("column", "data_type", "distinct", "expected_fragment"),
    [
        ("status", "VARCHAR", 3, None),
        ("role", "VARCHAR", 2, None),
        ("email", "VARCHAR", 4, "sensitive-data pattern"),
        ("google_sub", "VARCHAR", 1, "sensitive-data pattern"),
        ("password_hash", "VARCHAR", 3, "sensitive-data pattern"),
        ("title", "VARCHAR", 5, "user-authored free text"),
        ("content", "TEXT", 12, "user-authored free text"),
        ("payload", "JSONB", 4, "opaque type"),
        ("order_ref", "VARCHAR", 5000, "high cardinality"),
    ],
)
def test_withholding_policy(column, data_type, distinct, expected_fragment) -> None:
    reason = value_withholding_reason(column, data_type, distinct, row_count=10_000)
    if expected_fragment is None:
        assert reason is None
    else:
        assert reason is not None and expected_fragment in reason


def test_keys_are_always_withheld() -> None:
    reason = value_withholding_reason("status", "VARCHAR", 3, 100, is_key=True)
    assert reason is not None and "surrogate identifiers" in reason


def test_identifier_shaped_values_are_rejected() -> None:
    assert all_values_are_opaque_ids(["3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"])
    assert all_values_are_opaque_ids(["2026-05-19T13:34:02", "2026-05-20T09:00:00"])
    assert not all_values_are_opaque_ids(["active", "archived"])
    assert not all_values_are_opaque_ids([])


# --- full policy -----------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "data_type", "distinct"),
    [
        ("email", "VARCHAR", 4),
        ("password_hash", "VARCHAR", 3),
        ("title", "VARCHAR", 5),
        ("payload", "JSONB", 4),
        ("order_ref", "VARCHAR", 5000),
    ],
)
def test_full_policy_withholds_nothing(column, data_type, distinct) -> None:
    """Opting out is total. A policy that still hides some columns would be
    worse than either extreme: the reader cannot tell absence from omission."""
    assert value_withholding_reason(column, data_type, distinct, 10_000, policy="full") is None


def test_full_policy_passes_query_results_through() -> None:
    assert redact_result_value("email", "a@b.com", policy="full") == "a@b.com"
    assert redact_result_value("notes", "x" * 300, policy="full") == "x" * 300


def test_strict_remains_the_default() -> None:
    """Callers that pass no policy get the safe one — an unredacted catalogue
    has to be asked for."""
    assert value_withholding_reason("email", "VARCHAR", 4, 100) is not None
    assert redact_result_value("email", "a@b.com") != "a@b.com"
