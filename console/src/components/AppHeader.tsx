import type { EngineConfig, Job, SchemaOutput } from "@/api/types";
import { Coverage } from "@/components/Coverage";
import { GeneratePanel } from "@/components/GeneratePanel";
import { Badge } from "@/components/StatusBadge";
import { dismissFinishedJob } from "@/store/uiSlice";
import { useAppDispatch } from "@/store";

/** compact, and it must not dominate the workspace. */
export function AppHeader({
  output,
  config,
  job,
  busy,
  jobsUnavailable = false,
  children,
}: {
  output?: SchemaOutput;
  config?: EngineConfig;
  job?: Job;
  busy: boolean;
  /** The engine could not be asked what is running. Shown rather than
   *  swallowed: a failing job list looks exactly like an idle workspace, and
   *  it hid a live run once already. */
  jobsUnavailable?: boolean;
  children?: React.ReactNode;
}) {
  const blocked = config !== undefined && !config.api_key_configured;

  return (
    // Never wraps. The header is a fixed 60px, so anything that reflows pushes
    // the toggles onto a second row and shifts the whole toolbar mid-run —
    // which is what a long "Reading …" line did every few seconds.
    <header className="sticky top-0 z-40 flex h-[60px] items-center gap-x-4 border-b border-line bg-surface/85 px-5 backdrop-blur-md">
      {/* Truncates first. If the header ever runs out of room the title is the
          least costly thing to lose — clipping the toolbar loses controls. */}
      <h1 className="display min-w-0 truncate text-title text-ink">Semantic View Generator</h1>

      {output && (
        <>
          <span className="ident shrink-0 text-ink-2">
            {output.database}.{output.schema_name}
          </span>
          <span className="hidden min-w-0 truncate text-meta text-ink-3 lg:block">
            {output.claim_count} claims · {output.question_count} open questions
          </span>
          <div className="hidden shrink-0 xl:block">
            <Coverage output={output} />
          </div>
        </>
      )}

      <div className="ml-auto flex shrink-0 items-center gap-3">
        {children}
        {job && <GenerationStatus job={job} />}
        {!job && jobsUnavailable && (
          <Badge tone="review" title="GET /jobs failed — a run may be in progress">
            Can't see running jobs
          </Badge>
        )}
        {output && blocked && <Badge tone="failed">No API key configured</Badge>}
        {output && <GeneratePanel output={output} busy={busy} disabled={blocked} />}
      </div>
    </header>
  );
}

/**
 * show the current stage, never a full-page spinner while
 * usable data exists. Analysis runs for minutes, so the header carries the
 * state and the catalogue underneath stays readable throughout.
 *
 * The finished states persist until the next run rather than disappearing the
 * moment the job flips: a ten-minute run that ends in silence reads as a run
 * that did nothing.
 */
function GenerationStatus({ job }: { job: Job }) {
  const dispatch = useAppDispatch();

  if (job.status === "interrupted") {
    return (
      <Badge tone="review" title={job.error}>
        Run interrupted — the engine restarted
      </Badge>
    );
  }

  if (job.status === "failed") {
    return (
      <Badge tone="failed" title={job.error}>
        {job.error ? shorten(job.error) : "Generation failed"}
      </Badge>
    );
  }

  if (job.status === "succeeded") {
    return (
      <button
        type="button"
        onClick={() => dispatch(dismissFinishedJob())}
        title="Dismiss"
        className="rounded-[--radius-control]"
      >
        <Badge tone="validated">{summarize(job)}</Badge>
      </button>
    );
  }

  return <RunningStatus job={job} />;
}

/**
 * A run's live state, in a shape that cannot move the toolbar.
 *
 * Naming the tables inline made this line grow and shrink every few seconds as
 * workers swapped, wrapping the header onto a second row. The counts are what
 * belong at a glance; which six tables are being read right now is detail, and
 * detail goes in the tip.
 */
function RunningStatus({ job }: { job: Job }) {
  const progress = job.progress;
  const reading = progress?.current ?? [];
  const total = progress?.tables.length ?? 0;
  const done = progress?.completed.length ?? 0;
  const queued = Math.max(total - done - reading.length, 0);

  return (
    <span
      className="group relative flex items-center gap-2"
      role="status"
      aria-live="polite"
      tabIndex={reading.length ? 0 : undefined}
    >
      <span className="size-[7px] shrink-0 animate-pulse rounded-full bg-violet" aria-hidden />
      <span className="whitespace-nowrap text-meta text-violet-strong">
        {reading.length === 0
          ? (progress?.message ?? "Starting…")
          : `Reading ${reading.length}`}
        {total > 0 && ` · ${done}/${total}`}
      </span>

      {reading.length > 0 && (
        <span className="pointer-events-none absolute right-0 top-full z-50 mt-2 hidden w-[240px] rounded-[--radius-panel] border border-line bg-surface p-3 text-left group-hover:block group-focus-within:block">
          <span className="block text-meta font-semibold uppercase tracking-wide text-ink-3">
            Reading now
          </span>
          <span className="mt-1.5 block">
            {reading.map((table) => (
              <span key={table} className="ident block truncate py-[1px] text-ink">
                {table}
              </span>
            ))}
          </span>
          <span className="mt-2 block border-t border-line pt-2 text-meta text-ink-3">
            {done} done · {queued} queued · {total} in this run
          </span>
        </span>
      )}
    </span>
  );
}

/** What the run actually produced, so "complete" is not the only thing said. */
function summarize(job: Job): string {
  const result = job.result;
  if (!result) return "Generation complete";

  const claims = result.claims ?? 0;
  const tables = result.tables?.length ?? 0;
  const partial = result.partial?.length ?? 0;
  const noun = claims === 1 ? "claim" : "claims";
  const over = tables === 1 ? "table" : "tables";
  const base = `${claims} ${noun} across ${tables} ${over}`;
  // A run that was cut off mid-table is not a complete reading, and saying so
  // is the difference between "the model found little" and "the model stopped".
  return partial ? `${base} · ${partial} cut short` : base;
}

function shorten(message: string): string {
  const first = message.split("\n")[0] ?? message;
  return first.length > 90 ? `${first.slice(0, 89)}…` : first;
}
