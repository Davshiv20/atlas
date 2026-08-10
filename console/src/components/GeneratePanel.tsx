import { useState } from "react";

import type { SchemaOutput } from "@/api/types";
import { useAnalyzeMutation } from "@/store/api";
import { describeError } from "@/api/errors";
import { startJob } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * Generation control.
 *
 * The old button ran a fixed five tables with no indication of what was already
 * done, so the only way to learn its scope was to run it. This shows the
 * remaining work first and lets the run be sized to it.
 */
export function GeneratePanel({
  output,
  busy,
  disabled,
}: {
  output?: SchemaOutput;
  busy: boolean;
  disabled: boolean;
}) {
  const dispatch = useAppDispatch();
  const workspace = useAppSelector((s) => s.ui.workspace);
  const [analyze, { isLoading: starting }] = useAnalyzeMutation();
  const [open, setOpen] = useState(false);
  const [regenerate, setRegenerate] = useState(false);
  // The panel used to `.unwrap()` and drop the rejection, so a 400 looked
  // exactly like a click that did nothing.
  const [failure, setFailure] = useState<string | null>(null);

  const tables = output?.tables ?? [];
  const done = tables.filter((t) => t.analyzed).length;
  const remaining = tables.length - done;
  const target = regenerate ? tables.length : remaining;

  const run = async (limit: number | null) => {
    if (!workspace) return;
    setFailure(null);
    try {
      const started = await analyze({ workspace, limit, regenerate }).unwrap();
      dispatch(startJob(started.id));
      setOpen(false);
    } catch (error) {
      setFailure(describeError(error));
    }
  };

  // Sensible batch sizes, never offering more than there is work for.
  const batches = [5, 10, 25].filter((n) => n < target);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy || disabled}
        aria-expanded={open}
        className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink transition-colors hover:bg-cta-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-4"
      >
        {busy ? "Generating…" : "Generate semantic view"}
      </button>

      {open && !busy && (
        <div className="absolute right-0 z-10 mt-2 w-[320px] rounded-[--radius-panel] border border-line bg-surface p-4 shadow-none">
          <p className="text-panel font-semibold text-ink">Generate</p>

          <dl className="mt-2 flex gap-4 text-meta">
            <div>
              <dt className="text-ink-3">Analyzed</dt>
              <dd className="text-table font-semibold text-teal-strong">{done}</dd>
            </div>
            <div>
              <dt className="text-ink-3">Remaining</dt>
              <dd className="text-table font-semibold text-ink">{remaining}</dd>
            </div>
            <div>
              <dt className="text-ink-3">Tables</dt>
              <dd className="text-table font-semibold text-ink-2">{tables.length}</dd>
            </div>
          </dl>

          <label className="mt-3 flex items-start gap-2 text-body text-ink-2">
            <input
              type="checkbox"
              checked={regenerate}
              onChange={(e) => setRegenerate(e.target.checked)}
              className="mt-[3px]"
            />
            <span>
              Regenerate tables that already have a view
              <span className="mt-0.5 block text-meta text-ink-3">
                Discards their existing claims, evidence, and questions first, rather
                than merging. Scoped to the tables in this run — other tables keep
                their review. Off by default.
              </span>
            </span>
          </label>

          {failure && (
            <p className="mt-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-2.5 py-2 text-body text-red">
              {failure}
            </p>
          )}

          {target === 0 ? (
            <p className="mt-3 rounded-[--radius-control] border border-teal/25 bg-teal-soft px-2.5 py-2 text-body text-teal-strong">
              Every table already has a semantic view. Tick the box above to redo one.
            </p>
          ) : (
            <div className="mt-3 flex flex-col gap-2">
              <button type="button" onClick={() => run(null)} disabled={starting} className={PRIMARY}>
                {starting
                  ? "Starting…"
                  : regenerate
                    ? `Regenerate all ${target}`
                    : `Analyze all ${target} remaining`}
              </button>
              {batches.length > 0 && (
                <div className="flex gap-2">
                  {batches.map((n) => (
                    <button
                      key={n}
                      type="button"
                      onClick={() => run(n)}
                      disabled={starting}
                      className="flex-1 rounded-[--radius-control] border border-line bg-surface px-2 py-1.5 text-meta text-ink hover:bg-raised disabled:text-ink-4"
                    >
                      First {n}
                    </button>
                  ))}
                </div>
              )}
              <p className="text-meta text-ink-3">
                Most-referenced tables first. Roughly a minute each.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const PRIMARY =
  "rounded-[--radius-control] bg-cta px-3 py-2 text-body font-medium text-cta-ink transition-colors hover:bg-cta-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-4";
