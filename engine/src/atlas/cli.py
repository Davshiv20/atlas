from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from atlas.adapters.registry import create_adapter
from atlas.catalog import Catalog, WorkspaceConflict, list_workspaces, referencing_source
from atlas.jobs import get_registry
from atlas.manifest import InvalidWorkspace, WorkspaceManifest
from atlas.metadata import WorkspaceBusy
from atlas.sources import Source, SourceNotFound, SourceRegistry, source_registry_lock

app = typer.Typer(help="Grounded schema catalogue generator", no_args_is_help=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _workspace(name: str) -> Catalog:
    """The CLI and the API go through the same metadata store, so a workspace
    written by one is readable by the other."""
    try:
        return Catalog(name)
    except InvalidWorkspace as exc:
        raise typer.BadParameter(str(exc)) from exc


def _existing(name: str) -> Catalog:
    workspace = _workspace(name)
    manifest = _ensure_manifest(workspace)
    if not workspace.has_snapshot():
        raise typer.BadParameter(f"workspace {name!r} has no snapshot; run extract first")
    try:
        workspace.validate_snapshot(manifest)
    except WorkspaceConflict as exc:
        raise typer.BadParameter(str(exc)) from exc
    return workspace


def _ensure_manifest(workspace: Catalog) -> WorkspaceManifest:
    if workspace.is_registered():
        return workspace.manifest()
    if not workspace.has_snapshot():
        raise typer.BadParameter(f"workspace {workspace.name!r} is not bound to a source")
    snapshot = workspace.unchecked_snapshot()
    if not snapshot.source_id:
        raise typer.BadParameter(
            f"workspace {workspace.name!r} has no manifest and no snapshot.source_id; refusing migration"
        )
    with source_registry_lock():
        try:
            SourceRegistry.read().get(snapshot.source_id)
        except SourceNotFound as exc:
            raise typer.BadParameter(
                f"workspace {workspace.name!r} references missing source {snapshot.source_id!r}"
            ) from exc
        try:
            return workspace.adopt(snapshot.source_id)
        except WorkspaceConflict as exc:
            raise typer.BadParameter(str(exc)) from exc


def _source(source_id: str) -> Source:
    try:
        return SourceRegistry.read().get(source_id)
    except SourceNotFound as exc:
        raise typer.BadParameter(f"no source {source_id!r}") from exc


def _source_url(source: Source) -> str:
    try:
        return source.resolve_url()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc


@contextmanager
def _mutation(workspace: Catalog) -> Iterator[None]:
    """Share the API's SQLite reservation and the store's workspace lock."""
    registry = get_registry()
    active = registry.active_workspace_job(workspace.name)
    if active:
        raise typer.BadParameter(f"workspace has active job {active.id}")
    try:
        with workspace.mutation(blocking=False):
            active = registry.active_workspace_job(workspace.name)
            if active:
                raise typer.BadParameter(f"workspace has active job {active.id}")
            yield
    except WorkspaceBusy as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def create_workspace(
    workspace_name: str = typer.Argument(..., help="Workspace name, e.g. 'elara-review'"),
    source_id: str = typer.Argument(..., help="Declared source id to bind immutably"),
) -> None:
    """Create a workspace manifest bound to one source."""
    with source_registry_lock():
        _source(source_id)
        referenced = referencing_source(source_id)
        if referenced and workspace_name not in referenced:
            raise typer.BadParameter(f"source already has workspace(s): {', '.join(referenced)}")
        workspace = _workspace(workspace_name)
        try:
            workspace.register(source_id)
        except WorkspaceConflict as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{workspace.name} -> source {source_id}")


@app.command()
def extract(
    workspace_name: str = typer.Argument(..., help="Workspace name, e.g. 'elara'"),
    skip_profile: bool = typer.Option(False, help="Structure only, no distribution queries"),
    reset_semantics: bool = typer.Option(False, help="Confirm semantic reset when refreshing"),
) -> None:
    """Capture the physical schema for the workspace's bound source.

    Run `create-workspace WORKSPACE SOURCE` first. Extraction never accepts a
    per-run source or URL override, so a workspace cannot silently change
    datastores.
    """
    workspace = _workspace(workspace_name)
    with _mutation(workspace):
        manifest = _ensure_manifest(workspace)
        source = _source(manifest.source_id)
        if workspace.has_snapshot():
            try:
                workspace.validate_snapshot(manifest)
            except WorkspaceConflict as exc:
                raise typer.BadParameter(str(exc)) from exc
            if workspace.has_semantics() and not reset_semantics:
                raise typer.BadParameter(
                    "refresh would discard semantic state; rerun with --reset-semantics"
                )
        with create_adapter(_source_url(source)) as adapter:
            adapter.test_connection()
            snapshot = adapter.extract_structure(source.namespace)
            if not skip_profile:
                snapshot = adapter.profile(snapshot)
        workspace.assert_identity(
            source_id=manifest.source_id,
            incarnation_id=manifest.incarnation_id,
            generation=manifest.snapshot_generation,
        )
        published = workspace.publish(
            snapshot,
            reset_semantics=reset_semantics,
            expected_source_id=manifest.source_id,
            expected_incarnation_id=manifest.incarnation_id,
            expected_generation=manifest.snapshot_generation,
        )
        # Names the workspace, not a file. Where the record is kept is the
        # metadata store's decision and it will not always be a path.
        typer.echo(
            f"{len(snapshot.tables)} tables -> {workspace.name} "
            f"(generation {published.snapshot_generation})"
        )


@app.command()
def summary(workspace_name: str = typer.Argument(..., help="Workspace name")) -> None:
    """Print what was extracted, ranked by structural centrality."""
    snapshot = _existing(workspace_name).snapshot()
    ranked = sorted(
        snapshot.tables,
        key=lambda t: (snapshot.inbound_fk_count(t.name), t.row_count),
        reverse=True,
    )
    typer.echo(f"{'table':<26} {'rows':>8} {'cols':>5} {'in-fk':>6} {'enums':>6} {'held':>5}")
    for table in ranked:
        enums = sum(1 for c in table.columns if c.is_enum_candidate)
        withheld = sum(1 for c in table.columns if c.profile.values_withheld_reason)
        typer.echo(
            f"{table.name:<26} {table.row_count:>8} {len(table.columns):>5} "
            f"{snapshot.inbound_fk_count(table.name):>6} {enums:>6} {withheld:>5}"
        )


@app.command()
def workspaces() -> None:
    """List workspaces and their bound sources."""
    for name in list_workspaces():
        workspace = _workspace(name)
        try:
            manifest = _ensure_manifest(workspace)
        except typer.BadParameter as exc:
            typer.echo(f"{name}\tERROR\t{exc}")
            continue
        typer.echo(f"{name}\t{manifest.source_id}\tgeneration {manifest.snapshot_generation}")


@app.command()
def preflight() -> None:
    """Check the configured model exists on OpenRouter and answers a tool call."""
    from atlas.llm import Tool, build_client, effort, model_id, run_tool_loop

    client = build_client()
    target = model_id()

    available = {m.id for m in client.models.list().data}
    if target not in available:
        near = sorted(m for m in available if "opus" in m or "claude" in m)[:12]
        typer.echo(f"✗ {target} not found. Claude-ish models available:")
        for candidate in near:
            typer.echo(f"    {candidate}")
        raise typer.Exit(1)
    typer.echo(f"✓ model {target} available (effort={effort()})")

    seen: list[str] = []
    probe = Tool(
        name="report_ready",
        description="Call this once to confirm you can invoke tools.",
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
            "additionalProperties": False,
        },
        run=lambda note: seen.append(note) or "ok",
    )
    run_tool_loop(client, "You test tool wiring.", "Call report_ready.", [probe])
    if seen:
        typer.echo(f"✓ tool calling works ({seen[0][:60]})")
    else:
        typer.echo("✗ model did not call the tool — check tool-calling support for this slug")
        raise typer.Exit(1)


@app.command()
def analyze(
    workspace_name: str = typer.Argument(..., help="Workspace name"),
    limit: int = typer.Option(None, help="How many tables; omit for every remaining one"),
    tables: str = typer.Option(None, help="Comma-separated table names to analyze"),
    regenerate: bool = typer.Option(False, help="Re-analyze tables that already have claims"),
) -> None:
    """Run the analysis agent. Skips already-analyzed tables unless --regenerate."""
    from atlas.agent import (  # imported lazily: needs an API key
        analyze_schema,
        completely_described_tables,
        repair_columns_by_table,
    )

    workspace = _existing(workspace_name)
    with _mutation(workspace):
        manifest = workspace.manifest()
        source = _source(manifest.source_id)
        start_generation = manifest.snapshot_generation
        snapshot = workspace.snapshot()
        existing = workspace.facts()
        analyzed = completely_described_tables(snapshot, existing.facts)
        repairs = {} if regenerate else repair_columns_by_table(snapshot, existing.facts)

        selected = [t.strip() for t in tables.split(",")] if tables else None
        if regenerate:
            from atlas.agent import select_tables

            names = {t.name for t in select_tables(snapshot, limit, selected)}
            dropped = workspace.drop(names)
            typer.echo(f"discarded {dropped} for {len(names)} tables")

        store, questions, evidence = analyze_schema(
            create_adapter(_source_url(source)),
            snapshot,
            limit,
            tables=selected,
            already_analyzed=set() if regenerate else analyzed,
            repair_columns=repairs,
        )

        # Merge into what is already stored so prior human verdicts survive.
        workspace.assert_identity(
            source_id=manifest.source_id,
            incarnation_id=manifest.incarnation_id,
            generation=start_generation,
        )
        workspace.write_facts(workspace.facts().merge(store.facts))
        workspace.write_questions(questions)

        kept = workspace.evidence()
        for record in evidence.records:
            kept.add(record)
        kept.links.extend(evidence.links)
        workspace.write_evidence(kept)
        typer.echo(
            f"{len(store.facts)} claims, {len(questions.questions)} questions -> {workspace.name}"
        )


@app.command()
def compile(
    workspace_name: str = typer.Argument(..., help="Workspace name"),
    markdown: Path = typer.Option(None, help="Also render a Markdown view for review"),
    limit: int = typer.Option(None, help="Markdown view only: the N most relevant tables"),
) -> None:
    """Rebuild the output document from the stored snapshot and claims.

    Output is derived, never edited: corrections belong on the claim they came
    from, so that regenerating can never lose them.
    """
    from atlas.compile import render_markdown
    from atlas.output import build_output

    workspace = _existing(workspace_name)
    with _mutation(workspace):
        document = build_output(
            workspace.snapshot(),
            workspace.facts(),
            workspace.questions().questions,
            workspace.evidence(),
        )
        workspace.export_output(document)
        described = sum(1 for t in document.tables if t.description)
        with_grain = sum(1 for t in document.tables if t.grain)
        typer.echo(
            f"{document.table_count} tables ({described} described, "
            f"{with_grain} with grain) -> {workspace.name}"
        )

        if markdown:
            markdown.parent.mkdir(parents=True, exist_ok=True)
            markdown.write_text(render_markdown(document, limit))
            typer.echo(f"markdown view -> {markdown}")


@app.command()
def claims(workspace_name: str = typer.Argument(..., help="Workspace name")) -> None:
    """Show review status with current evidence-derived trust scores."""
    from atlas.output import assess_facts

    workspace = _existing(workspace_name)
    store = assess_facts(workspace.facts(), workspace.evidence())
    pending = store.needing_review()
    typer.echo(f"{len(store.facts)} facts, {len(pending)} awaiting review")
    for fact in pending[:20]:
        typer.echo(
            f"  [trust {round(fact.confidence * 100)}/100] {fact.id}: {fact.claim}"
        )


if __name__ == "__main__":
    app()
