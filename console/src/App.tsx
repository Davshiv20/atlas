import { useEffect } from "react";

import { AppHeader } from "@/components/AppHeader";
import { Questions } from "@/components/Questions";
import { ReviewQueue } from "@/components/ReviewQueue";
import { SchemaMap } from "@/components/SchemaMap";
import { Sources } from "@/components/Sources";
import { ErrorState, LoadingState } from "@/components/States";
import { WorkspacePicker } from "@/components/WorkspacePicker";
import {
  api,
  useConfigQuery,
  useJobQuery,
  useJobsQuery,
  useOutputQuery,
  useQuestionsQuery,
  useWorkspacesQuery,
} from "@/store/api";
import { adoptJob, finishJob, selectWorkspace, setView } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

export default function App() {
  const dispatch = useAppDispatch();
  const { workspace, runningJobId, finishedJob, view } = useAppSelector((s) => s.ui);

  const { data: config } = useConfigQuery();
  const { data: workspaces, isLoading: loadingWorkspaces, error: workspacesError } =
    useWorkspacesQuery();
  const { data: output, error: outputError } = useOutputQuery(workspace ?? "", {
    skip: !workspace,
  });
  const { data: questions } = useQuestionsQuery(workspace ?? "", { skip: !workspace });
  const openQuestions = (questions ?? []).filter((q) => q.status === "open").length;
  // Poll only while a job is live. RTK Query stops the interval the moment the
  // subscription is skipped, so a finished run costs nothing.
  const { data: job } = useJobQuery(runningJobId ?? "", {
    skip: !runningJobId,
    pollingInterval: 2000,
  });
  // A run belongs to the engine, not to the tab that started it. Without this
  // the console only knew about a job it had dispatched itself, so a reload
  // mid-run — or a second tab, or a run started from the CLI — showed an idle
  // workspace while the engine was minutes into analysing it.
  const { data: workspaceJobs, error: jobsError } = useJobsQuery(workspace ?? "", {
    skip: !workspace || Boolean(runningJobId),
    pollingInterval: 10_000,
  });

  useEffect(() => {
    if (runningJobId || !workspaceJobs) return;
    const live = workspaceJobs.find(
      (candidate) => candidate.status === "running" || candidate.status === "pending",
    );
    if (live) dispatch(adoptJob(live.id));
  }, [workspaceJobs, runningJobId, dispatch]);

  useEffect(() => {
    if (!workspace && workspaces?.length) dispatch(selectWorkspace(workspaces[0]!));
  }, [workspace, workspaces, dispatch]);

  // Each finished table is on disk the moment the engine reports it, so the
  // catalogue is refetched as the count grows rather than only at the end —
  // otherwise the header reads "3 of 5" while the table list shows none of
  // them, which is what it did before the engine wrote incrementally.
  const completedCount = job?.progress?.completed.length ?? 0;
  useEffect(() => {
    if (completedCount > 0) {
      dispatch(api.util.invalidateTags(["Output", "Claims", "Questions"]));
    }
  }, [completedCount, dispatch]);

  // A finished run also settles counts and the job badge.
  useEffect(() => {
    if (!job || job.status === "running" || job.status === "pending") return;
    dispatch(finishJob(job));
    if (job.status === "succeeded") {
      dispatch(api.util.invalidateTags(["Output", "Claims", "Questions", "Workspaces"]));
    }
  }, [job, dispatch]);




  if (workspacesError) {
    return <Shell><ErrorState message="The workspace list could not be loaded." /></Shell>;
  }
  if (loadingWorkspaces) {
    return <Shell><LoadingState label="Connecting to the engine…" /></Shell>;
  }

  return (
    <div className="grid h-full grid-rows-[auto_1fr] overflow-hidden">
      <AppHeader
        output={output}
        config={config}
        job={runningJobId ? job : (finishedJob ?? undefined)}
        busy={Boolean(runningJobId)}
        jobsUnavailable={Boolean(jobsError)}
      >
        {output && (
          <>
            <WorkspacePicker workspaces={workspaces ?? []} />
            <ViewToggle openQuestions={openQuestions} />
          </>
        )}
      </AppHeader>

      {view === "sources" ? (
        <Sources />
      ) : !workspace || !output ? (
        // No catalogue yet: Connections is the screen. Plugging a database in
        // is the first thing, not a checklist that links to it.
        <Sources />
      ) : view === "questions" ? (
        <Questions workspace={workspace} />
      ) : view === "map" ? (
        <SchemaMap output={output} workspace={workspace} />
      ) : (
        <ReviewQueue output={output} workspace={workspace} />
      )}
    </div>
  );
}

const VIEWS = {
  sources: "Sources",
  map: "Map",
  workspace: "Review",
  questions: "Questions",
} as const;

function ViewToggle({ openQuestions }: { openQuestions: number }) {
  const dispatch = useAppDispatch();
  const view = useAppSelector((s) => s.ui.view);
  return (
    <div className="flex rounded-[--radius-control] border border-line bg-raised p-[2px]">
      {(Object.keys(VIEWS) as (keyof typeof VIEWS)[]).map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => dispatch(setView(option))}
          aria-pressed={view === option}
          className={`flex items-center gap-1.5 rounded-[--radius-control] px-2.5 py-1 text-meta transition-colors ${
            view === option
              ? "bg-canvas text-ink shadow-[0_0_0_1px_var(--color-line)]"
              : "text-ink-3 hover:text-ink"
          }`}
        >
          {VIEWS[option]}
          {/* The count is the point of the tab: an unanswered question is the
              one thing blocking a claim from being trusted. */}
          {option === "questions" && openQuestions > 0 && (
            <span className="rounded-full bg-amber-soft px-1.5 text-badge font-semibold text-amber-strong">
              {openQuestions}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid h-full grid-rows-[auto_1fr]">
      <AppHeader busy={false} />
      {children}
    </div>
  );
}

