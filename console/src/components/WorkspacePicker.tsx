import { useEffect, useRef, useState } from "react";

import type { SchemaOutput, WorkspaceSummary } from "@/api/types";
import { Coverage } from "@/components/Coverage";
import { selectWorkspace } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * Which catalogue you are looking at, and how much of it is done.
 *
 * Both facts belong to the same question, so they share one control on the left
 * of the toolbar. Previously the selector sat on the far right beside Generate
 * while the schema name, claim counts and coverage bar occupied the whole left
 * half of the header — the toolbar led with the contents of a workspace and
 * buried the choice of workspace next to the button that rewrites it.
 *
 * Rendered as a button even when there is one workspace. A bare label read as
 * decoration, and the detail underneath is worth opening for regardless of how
 * many workspaces exist.
 */
export function WorkspacePicker({
  workspaces,
  output,
}: {
  workspaces: WorkspaceSummary[];
  output?: SchemaOutput;
}) {
  const dispatch = useAppDispatch();
  const current = useAppSelector((s) => s.ui.workspace);
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLDivElement>(null);

  // Escape and a click elsewhere both close it. A popover that only closes by
  // pressing its own trigger is a popover you fight.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!anchor.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  if (workspaces.length === 0) return null;
  const active = workspaces.find((candidate) => candidate.id === current) ?? workspaces[0]!;

  return (
    <div ref={anchor} className="relative min-w-0">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={describe(active)}
        className="flex min-w-0 items-center gap-2 rounded-[--radius-control] border border-line bg-surface px-2.5 py-1 text-meta text-ink hover:bg-raised"
      >
        {/* Both bounded. The state string runs to "cached · credentials
            missing", and an unyielding element beside a long workspace id is
            what pushed this toolbar off the right edge of the window before. */}
        <span className="ident max-w-[180px] truncate">{active.id}</span>
        <span className="hidden max-w-[170px] truncate text-ink-3 md:block">
          {workspaceState(active)}
        </span>
        <Chevron open={open} />
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Workspace"
          className="absolute left-0 z-30 mt-2 w-[300px] rounded-[--radius-panel] border border-line bg-surface p-4"
        >
          {output ? (
            <Contents output={output} />
          ) : (
            <p className="text-body text-ink-3">Opening {active.id}…</p>
          )}

          {workspaces.length > 1 && (
            <>
              <p className="mt-4 border-t border-line pt-3 text-meta font-semibold uppercase tracking-wide text-ink-3">
                Workspaces
              </p>
              <ul className="mt-1.5 flex flex-col">
                {workspaces.map((workspace) => (
                  <li key={workspace.id}>
                    <button
                      type="button"
                      onClick={() => {
                        dispatch(selectWorkspace(workspace.id));
                        setOpen(false);
                      }}
                      aria-current={workspace.id === active.id}
                      title={describe(workspace)}
                      className={`flex w-full min-w-0 items-baseline gap-2 rounded-[--radius-control] px-2 py-1.5 text-left hover:bg-raised ${
                        workspace.id === active.id ? "bg-raised" : ""
                      }`}
                    >
                      <span className="ident min-w-0 flex-1 truncate text-body text-ink">
                        {workspace.id}
                      </span>
                      <span className="shrink-0 text-meta text-ink-3">
                        {workspaceState(workspace)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** What this workspace holds — the facts the header used to spread across itself. */
function Contents({ output }: { output: SchemaOutput }) {
  return (
    <>
      <p className="ident break-all text-body text-ink" title={qualifiedSchema(output)}>
        {qualifiedSchema(output)}
      </p>
      <p className="mt-1 text-meta text-ink-3">
        {output.claim_count} claims · {output.question_count} open questions
      </p>
      <div className="mt-3">
        <Coverage output={output} />
      </div>
    </>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 10 6"
      className={`size-[8px] shrink-0 text-ink-3 ${open ? "rotate-180" : ""}`}
      aria-hidden
    >
      <path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

/**
 * The namespace, said once.
 *
 * Adapters disagree about what `schema_name` holds. Postgres stores a bare
 * `public` and needs the database in front of it; Snowflake stores the whole
 * `DATABASE.SCHEMA`, because that is what its queries have to be qualified
 * with. Prepending unconditionally rendered `POC_DB.POC_DB.TRELLIS_SOURCE`.
 */
function qualifiedSchema(output: SchemaOutput): string {
  return output.schema_name.startsWith(`${output.database}.`)
    ? output.schema_name
    : `${output.database}.${output.schema_name}`;
}

/** Everything about a workspace, for the tooltip rather than the control. */
function describe(workspace: WorkspaceSummary): string {
  return [
    workspace.id,
    workspace.adapter ?? workspace.source_id,
    workspaceState(workspace),
    snapshotAge(workspace),
  ].join(" · ");
}

function snapshotAge(workspace: WorkspaceSummary): string {
  return workspace.snapshot_time
    ? `snapshot ${new Date(workspace.snapshot_time).toLocaleDateString()}`
    : "no snapshot";
}

function workspaceState(workspace: WorkspaceSummary): string {
  if (!workspace.snapshot_available) return "not extracted";
  if (!workspace.source_configured) return "cached · credentials missing";
  if (workspace.source_health.state === "failed") return "cached · source offline";
  if (workspace.source_health.state === "connected") return "live";
  return "cached · connection not checked";
}
