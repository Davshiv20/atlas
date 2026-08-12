import type { WorkspaceSummary } from "@/api/types";
import { selectWorkspace } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/** Selects an explicit workspace, never a source id masquerading as one. */
export function WorkspacePicker({ workspaces }: { workspaces: WorkspaceSummary[] }) {
  const dispatch = useAppDispatch();
  const current = useAppSelector((s) => s.ui.workspace);

  if (workspaces.length === 0) return null;
  if (workspaces.length === 1) {
    const workspace = workspaces[0]!;
    return (
      <span
        className="ident max-w-[220px] truncate text-meta text-ink-3"
        title={describe(workspace)}
      >
        {workspace.id} · {workspaceState(workspace)}
      </span>
    );
  }

  return (
    <label className="flex min-w-0 items-center gap-2 text-meta text-ink-3">
      <span className="shrink-0">Workspace</span>
      {/* Bounded, because a native select sizes itself to its widest option and
          nothing above it can push back. Four facts per option — id, adapter,
          liveness, snapshot date — made this wider than the rest of the toolbar
          combined and pushed the controls off the right edge of the window.
          The option identifies; the tooltip carries the detail. */}
      <select
        value={current ?? ""}
        onChange={(e) => dispatch(selectWorkspace(e.target.value))}
        title={describe(workspaces.find((w) => w.id === current) ?? workspaces[0]!)}
        className="ident max-w-[190px] truncate rounded-[--radius-control] border border-line bg-surface px-2 py-1 text-ink focus:border-line-ink focus:outline-none"
      >
        {workspaces.map((workspace) => (
          <option key={workspace.id} value={workspace.id}>
            {workspace.id} · {workspaceState(workspace)}
          </option>
        ))}
      </select>
    </label>
  );
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
