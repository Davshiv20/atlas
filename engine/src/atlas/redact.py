"""Which column values are safe to carry into the catalogue.

Sample values are where the semantics live, so the policy is deliberately not
"redact everything" — it is "emit values only where they are both low-risk and
informative". A closed set of 6 status strings is the single most useful signal
for inferring what a column means; a column of email addresses is pure risk with
no inferential payoff, because the type already tells you what it is.
"""

from __future__ import annotations

import re

SENSITIVE_NAME = re.compile(
    r"(pass(word|wd)|secret|salt|token|api[_-]?key|private[_-]?key|hash"
    r"|ssn|tax[_-]?id|email|phone|mobile|address|postcode|zip"
    r"|first[_-]?name|last[_-]?name|full[_-]?name|surname"
    r"|dob|birth|credit|card|iban|account[_-]?no|account[_-]?number"
    r"|session|refresh|cookie|signature|otp"
    r"|(^|_)sub$|oauth|openid|external[_-]?id|provider[_-]?id)",
    re.IGNORECASE,
)

# Columns humans type into. On a small or dev-sized dataset these look
# categorical purely because there are few rows, which is the most dangerous
# false negative in this policy: it is precisely the case where real user
# content gets promoted into a catalogue that later ships to an agent.
FREE_TEXT_NAME = re.compile(
    r"(^|_)(title|name|label|description|desc|note|notes|comment|comments"
    r"|summary|subject|body|message|content|reason|feedback)(_|$)",
    re.IGNORECASE,
)

# Types whose values are unbounded free-form content: never worth emitting, and
# the most likely place for personal data to hide.
OPAQUE_TYPES = {"text", "json", "jsonb", "xml", "bytea", "tsvector"}

# Above this many distinct values a column is an identifier or free text, not a
# categorical, so the values teach you nothing about meaning.
ENUM_MAX_DISTINCT = 40


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}|$)")


def all_values_are_opaque_ids(values: list[str]) -> bool:
    """A low-cardinality FK column on a small database looks categorical but
    teaches nothing — the values are surrogate keys, not domain vocabulary.
    Timestamps are the same story: a distribution, not a vocabulary."""
    if not values:
        return False
    return all(UUID_RE.match(v) for v in values) or all(TIMESTAMP_RE.match(v) for v in values)


def value_withholding_reason(
    column_name: str,
    data_type: str,
    distinct_count: int | None,
    row_count: int | None,
    is_key: bool = False,
    policy: str = "strict",
) -> str | None:
    """Return why values must be withheld, or None if they may be emitted."""
    if policy == "full":
        return None

    if is_key:
        return "primary or foreign key; values are surrogate identifiers"

    if SENSITIVE_NAME.search(column_name):
        return "column name matches sensitive-data pattern"

    if FREE_TEXT_NAME.search(column_name):
        return "user-authored free text; low cardinality here is an artefact of dataset size"

    normalized_type = data_type.lower().split("(")[0].strip()
    if normalized_type in OPAQUE_TYPES:
        return f"opaque type ({normalized_type}) may contain free-form personal data"

    if distinct_count is None:
        return "distinct count unavailable, cannot confirm categorical"

    if distinct_count > ENUM_MAX_DISTINCT:
        return f"high cardinality ({distinct_count} distinct), not a categorical"

    if row_count and distinct_count == row_count and row_count > 1:
        return "distinct count equals row count, column is an identifier"

    return None


MAX_RESULT_STRING_LENGTH = 80


def redact_result_value(column_name: str, value: object, policy: str = "strict") -> object:
    """Policy for values coming back from an agent-authored query.

    Weaker than the profiling policy by necessity — an arbitrary SELECT has no
    declared column types to reason about, only the alias the agent chose. It
    is defense in depth, not a guarantee: an agent that writes
    `SELECT substr(email, 1, 3) AS x` defeats it. The read-only role and the
    reviewer are the other two layers.
    """
    if policy == "full" or value is None or isinstance(value, (int, float, bool)):
        return value

    if SENSITIVE_NAME.search(column_name) or FREE_TEXT_NAME.search(column_name):
        return "‹withheld: sensitive or free-text column name›"

    text_value = str(value)
    if UUID_RE.match(text_value):
        return "‹withheld: identifier›"
    if len(text_value) > MAX_RESULT_STRING_LENGTH:
        return f"‹withheld: {len(text_value)}-char value, likely free text›"
    return value


def is_profilable_type(data_type: str) -> bool:
    """Whether count(distinct ...) is legal for this type in Postgres."""
    normalized_type = data_type.lower().split("(")[0].strip()
    return normalized_type not in {"json", "xml", "tsvector", "point", "line"}


def supports_min_max(data_type: str) -> bool:
    """min/max are emitted only for ordered, non-textual types — a min/max over
    text is a leak vector (it prints two real values verbatim)."""
    normalized_type = data_type.lower().split("(")[0].strip()
    return any(
        normalized_type.startswith(prefix)
        for prefix in (
            "int", "bigint", "smallint", "numeric", "decimal", "real",
            "double", "float", "money", "date", "timestamp", "time",
        )
    )
