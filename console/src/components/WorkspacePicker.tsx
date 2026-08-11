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
      <span className="ident text-meta text-ink-3">
        {workspace.id} · {workspaceState(workspace)} · {snapshotAge(workspace)}
      </span>
    );
  }

  return (
    <label className="flex items-center gap-2 text-meta text-ink-3">
      Workspace
      <select
        value={current ?? ""}
        onChange={(e) => dispatch(selectWorkspace(e.target.value))}
        className="ident rounded-[--radius-control] border border-line bg-surface px-2 py-1 text-ink focus:border-line-ink focus:outline-none"
      >
        {workspaces.map((workspace) => (
          <option key={workspace.id} value={workspace.id}>
            {workspace.id} · {workspace.adapter ?? workspace.source_id} · {workspaceState(workspace)}
            {` · ${snapshotAge(workspace)}`}
          </option>
        ))}
      </select>
    </label>
  );
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
