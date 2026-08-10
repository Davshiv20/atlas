import { selectWorkspace } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * Not part of DESIGN.md's first cut, which assumes one imported schema. The
 * engine is multi-workspace, so hiding that would be a lie about what the API
 * does — but the control stays out of the way until there is a choice to make.
 */
export function WorkspacePicker({ workspaces }: { workspaces: string[] }) {
  const dispatch = useAppDispatch();
  const current = useAppSelector((s) => s.ui.workspace);

  if (workspaces.length <= 1) return null;

  return (
    <label className="flex items-center gap-2 text-meta text-ink-3">
      Source
      <select
        value={current ?? ""}
        onChange={(e) => dispatch(selectWorkspace(e.target.value))}
        className="ident rounded-[--radius-control] border border-line bg-surface px-2 py-1 text-ink focus:border-line-ink focus:outline-none"
      >
        {workspaces.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </label>
  );
}
