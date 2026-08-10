from __future__ import annotations

import logging
from pathlib import Path

import typer

from atlas.adapters.base import UnsupportedDatabase
from atlas.adapters.registry import create_adapter
from atlas.workspace import InvalidWorkspace, Workspace

app = typer.Typer(help="Grounded schema catalogue generator", no_args_is_help=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _workspace(name: str) -> Workspace:
    """The CLI and the API address the same layout, so a workspace written by
    one is readable by the other."""
    try:
        return Workspace(name)
    except InvalidWorkspace as exc:
        raise typer.BadParameter(str(exc)) from exc


def _existing(name: str) -> Workspace:
    workspace = _workspace(name)
    if not workspace.exists():
        raise typer.BadParameter(f"workspace {name!r} has no snapshot; run extract first")
    return workspace


def _adapter(url: str | None):
    from atlas.settings import get_settings

    try:
        resolved = url or get_settings().require_database_url()
        return create_adapter(resolved)
    except (RuntimeError, UnsupportedDatabase) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def extract(
    workspace_name: str = typer.Argument(..., help="Workspace name, e.g. 'elara'"),
    url: str = typer.Option(None, help="SQLAlchemy URL, e.g. postgresql+psycopg://u:p@host/db"),
    schema: str = typer.Option("public"),
    skip_profile: bool = typer.Option(False, help="Structure only, no distribution queries"),
) -> None:
    """Capture the physical schema. Deterministic; no model involved."""
    workspace = _workspace(workspace_name)
    with _adapter(url) as adapter:
        adapter.test_connection()
        snapshot = adapter.extract_structure(schema)
        if not skip_profile:
            snapshot = adapter.profile(snapshot)
    snapshot.write(workspace.snapshot_path)
    typer.echo(f"{len(snapshot.tables)} tables -> {workspace.snapshot_path}")


@app.command()
def summary(workspace_name: str = typer.Argument(..., help="Workspace name")) -> None:
    """Print what was extracted, ranked by structural centrality."""
    snapshot = _existing(workspace_name).read_snapshot()
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
    """List workspaces that have a snapshot."""
    for name in Workspace.list_all():
        typer.echo(name)


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
    url: str = typer.Option(None, help="SQLAlchemy URL; must match the snapshot's database"),
    limit: int = typer.Option(None, help="How many tables; omit for every remaining one"),
    tables: str = typer.Option(None, help="Comma-separated table names to analyze"),
    regenerate: bool = typer.Option(False, help="Re-analyze tables that already have claims"),
) -> None:
    """Run the analysis agent. Skips already-analyzed tables unless --regenerate."""
    from atlas.agent import analyze_schema  # imported lazily: needs an API key

    workspace = _existing(workspace_name)
    snapshot = workspace.read_snapshot()
    existing = workspace.read_facts()
    analyzed = {f.subject.split(".")[0] for f in existing.facts}

    selected = [t.strip() for t in tables.split(",")] if tables else None
    if regenerate:
        from atlas.agent import select_tables

        names = {t.name for t in select_tables(snapshot, limit, selected)}
        dropped = workspace.drop(names)
        typer.echo(f"discarded {dropped} for {len(names)} tables")

    store, questions, evidence = analyze_schema(
        _adapter(url),
        snapshot,
        limit,
        tables=selected,
        already_analyzed=set() if regenerate else analyzed,
    )

    # Merge into what is already stored so prior human verdicts survive.
    merged = workspace.read_facts().merge(store.facts)
    merged.write(workspace.facts_path)
    questions.write(workspace.questions_path)

    kept = workspace.read_evidence()
    for record in evidence.records:
        kept.add(record)
    kept.links.extend(evidence.links)
    kept.write(workspace.evidence_path)
    typer.echo(
        f"{len(store.facts)} claims, {len(questions.questions)} questions -> {workspace.root}"
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
    document = build_output(
        workspace.read_snapshot(),
        workspace.read_facts(),
        workspace.read_questions(),
        workspace.read_evidence(),
    )
    document.write(workspace.output_path)
    out = workspace.output_path
    described = sum(1 for t in document.tables if t.description)
    with_grain = sum(1 for t in document.tables if t.grain)
    typer.echo(
        f"{document.table_count} tables ({described} described, {with_grain} with grain) -> {out}"
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
    store = assess_facts(workspace.read_facts(), workspace.read_evidence())
    pending = store.needing_review()
    typer.echo(f"{len(store.facts)} facts, {len(pending)} awaiting review")
    for fact in pending[:20]:
        typer.echo(
            f"  [trust {round(fact.confidence * 100)}/100] {fact.id}: {fact.claim}"
        )


if __name__ == "__main__":
    app()
