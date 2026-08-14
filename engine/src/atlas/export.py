"""The semantic view, packaged to leave Atlas.

Composition and rendering only: this reads what `output` and `semantic_view`
already derived, adds a header saying what the reader is holding, and returns
text. It writes nothing and stores nothing — where the text goes is the
caller's business, which is the same rule `atlas.compile` follows.

Two things distinguish an export from `GET /semantic-view`.

**It is gated on review.** The view carries every captured table with review
state in comments, which is right for a console showing progress. A file that
leaves the building is read by something that does not read comments, so the
default is the tables where nothing consequential is outstanding, and including
the rest takes an explicit decision that the header then states.

**It says when it was taken and from what.** A served view is current by
definition; a file on someone's disk is not, and one that cannot name its
snapshot generation is one nobody can tell is stale.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from atlas.output import SchemaOutput
from atlas.semantic_view import SemanticView, build_semantic_view, render_yaml


class UnknownTable(LookupError):
    """Asked to export a table the view does not have."""


def select(
    output: SchemaOutput, *, ready_only: bool = True, table: str | None = None
) -> tuple[SemanticView, list[str]]:
    """The view to export, and the names held back from it.

    Returns the excluded names rather than just a count so the header can say
    which tables are missing. "4 tables excluded" tells a reader they have an
    incomplete file; naming them tells them whether it matters.
    """
    view = build_semantic_view(output)
    if table is not None and not any(t.name == table for t in view.tables):
        raise UnknownTable(table)

    chosen = [t for t in view.tables if table is None or t.name == table]
    if ready_only:
        kept = [t for t in chosen if t.emittable]
    else:
        kept = chosen

    excluded = sorted({t.name for t in chosen} - {t.name for t in kept})
    return (
        SemanticView(
            database=view.database, schema_name=view.schema_name, tables=kept
        ),
        excluded,
    )


def render(
    view: SemanticView,
    excluded: list[str],
    *,
    workspace: str,
    generation: int,
    total: int,
    fmt: str = "yaml",
    at: datetime | None = None,
) -> str:
    """The export as text, header included."""
    taken = at or datetime.now(UTC)
    if fmt == "json":
        return json.dumps(
            {
                "workspace": workspace,
                "database": view.database,
                "schema": view.schema_name,
                "snapshot_generation": generation,
                "exported_at": taken.isoformat(),
                "tables_total": total,
                "tables_exported": len(view.tables),
                # Named, not counted, and present as a key rather than a
                # comment: JSON has no comments, and a consumer that cannot see
                # what is missing will assume nothing is.
                "tables_excluded": excluded,
                "unreviewed_included": _unreviewed(view),
                "view": view.model_dump(mode="json"),
            },
            indent=2,
        )
    return _header(view, excluded, workspace, generation, total, taken) + render_yaml(view)


def _header(
    view: SemanticView,
    excluded: list[str],
    workspace: str,
    generation: int,
    total: int,
    taken: datetime,
) -> str:
    lines = [
        f"# Atlas semantic view — {workspace} / {view.schema_name}",
        f"# Exported {taken.strftime('%Y-%m-%dT%H:%M:%SZ')} · snapshot generation {generation}",
    ]

    unreviewed = _unreviewed(view)
    if unreviewed:
        # The loud case. This file carries meaning nobody stood behind, and the
        # reader has to meet that before the first claim rather than infer it
        # from a missing comment forty lines down.
        lines += [
            "#",
            f"# ⚠ {len(unreviewed)} of the {len(view.tables)} tables below have claims that",
            "#   nobody has reviewed. Their meaning is the model's, unverified:",
            *[f"#     {name}" for name in unreviewed],
            "#",
        ]
    elif excluded:
        lines.append(
            f"# {len(view.tables)} of {total} tables passed review. "
            f"{len(excluded)} excluded as unvalidated:"
        )
        lines += [f"#   {name}" for name in excluded]
    else:
        lines.append(f"# All {len(view.tables)} tables passed review.")

    return "\n".join(lines) + "\n"


def _unreviewed(view: SemanticView) -> list[str]:
    return [t.name for t in view.tables if not t.emittable]


def etag(view: SemanticView, excluded: list[str], *, generation: int, fmt: str) -> str:
    """A tag over what was exported, never over the rendered bytes.

    The rendered text carries the moment it was taken, so hashing it would mint
    a new tag on every request and the 304 would never fire — a caller polling
    for current context would refetch an unchanged view forever, which is worse
    than offering no tag at all.

    Content-addressed on the view itself plus the things that change what the
    same view means: which generation it came from, what was held back, and the
    format that was asked for.
    """
    material = json.dumps(
        {
            "view": view.model_dump(mode="json"),
            "excluded": excluded,
            "generation": generation,
            "format": fmt,
        },
        sort_keys=True,
        default=str,
    )
    return '"' + hashlib.sha256(material.encode()).hexdigest()[:32] + '"'
