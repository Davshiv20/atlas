import { useMemo, useState } from "react";

import type { Question } from "@/api/types";
import { describeError } from "@/api/errors";
import { Badge } from "@/components/StatusBadge";
import {
  useAnswerQuestionMutation,
  useDismissQuestionMutation,
  useQuestionsQuery,
} from "@/store/api";
import { selectTable, setView } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * The open-questions queue.
 *
 * These are the decisions the product exists to collect. Data establishes what
 * a column contains and never what it means, so a business claim is capped at
 * "observed" — around 0.65 — however much of the table is scanned. An answer
 * here is the only thing that moves it, and until this screen existed the
 * questions were write-only: raised by the agent, counted in the header, and
 * unanswerable.
 */
export function Questions({ workspace }: { workspace: string }) {
  const { data: questions, isLoading } = useQuestionsQuery(workspace);
  const [showSettled, setShowSettled] = useState(false);

  const { open, settled } = useMemo(() => {
    const all = questions ?? [];
    return {
      open: all.filter((q) => q.status === "open"),
      settled: all.filter((q) => q.status !== "open"),
    };
  }, [questions]);

  if (isLoading) {
    return <p className="p-6 text-body text-ink-3">Loading questions…</p>;
  }

  const shown = showSettled ? settled : open;

  return (
    <section className="min-h-0 overflow-y-auto">
      <div className="mx-auto w-full max-w-[820px] px-6 py-6">
        <header className="mb-5">
          <h2 className="display text-title text-ink">Open questions</h2>
          <p className="mt-1 text-body text-ink-3">
            What no query can settle. An answer here is recorded as evidence and is the
            only thing that lifts a claim about business meaning above what the data
            alone can show.
          </p>
        </header>

        <div className="mb-4 flex items-center gap-2">
          <Toggle active={!showSettled} onClick={() => setShowSettled(false)}>
            Open · {open.length}
          </Toggle>
          <Toggle active={showSettled} onClick={() => setShowSettled(true)}>
            Settled · {settled.length}
          </Toggle>
        </div>

        {shown.length === 0 ? (
          <p className="rounded-[--radius-panel] border border-line bg-surface px-4 py-8 text-center text-body text-ink-3">
            {showSettled
              ? "Nothing settled yet."
              : "No open questions. Generate a semantic view to raise some."}
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {shown.map((question) => (
              <li key={question.id}>
                <QuestionCard question={question} workspace={workspace} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function QuestionCard({ question, workspace }: { question: Question; workspace: string }) {
  const dispatch = useAppDispatch();
  const reviewer = useAppSelector((s) => s.ui.reviewer);
  const [answer, { isLoading: answering }] = useAnswerQuestionMutation();
  const [dismiss, { isLoading: dismissing }] = useDismissQuestionMutation();

  const [text, setText] = useState("");
  const [mode, setMode] = useState<"answer" | "dismiss">("answer");
  const [failure, setFailure] = useState<string | null>(null);

  const settled = question.status !== "open";
  const busy = answering || dismissing;

  // The intent is an argument, not state. Read from `mode` it came from the
  // closure of the render that created this function, so `setMode("dismiss")`
  // immediately followed by `submit()` still saw "answer" — and setting a
  // question aside recorded the reason as an authoritative claim instead.
  const submit = async (intent: "answer" | "dismiss") => {
    setFailure(null);
    setMode(intent);
    const request = { workspace, id: question.id, answer: text.trim(), reviewer };
    try {
      await (intent === "answer" ? answer(request) : dismiss(request)).unwrap();
      setText("");
    } catch (error) {
      setFailure(describeError(error, "The answer could not be saved."));
    }
  };

  return (
    <article className="rounded-[--radius-panel] border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => {
            dispatch(setView("workspace"));
            if (question.table) dispatch(selectTable(question.table));
          }}
          className="ident text-ink-2 underline decoration-line-strong underline-offset-2 hover:text-ink"
        >
          {question.subject}
        </button>
        <Badge tone="neutral">{question.aspect}</Badge>
        {question.status === "answered" && <Badge tone="validated">Answered</Badge>}
        {question.status === "dismissed" && <Badge tone="neutral">Set aside</Badge>}
      </div>

      <p className="mt-2 text-table text-ink">{question.question}</p>

      {question.evidence && (
        <p className="mt-2 text-meta text-ink-3">
          What prompted it: <span className="ident">{question.evidence}</span>
        </p>
      )}

      {settled ? (
        <div className="mt-3 rounded-[--radius-control] border border-line bg-raised px-3 py-2">
          <p className="text-body text-ink">{question.answer}</p>
          <p className="mt-1 text-meta text-ink-3">
            {question.answered_by}
            {question.answered_at && ` · ${new Date(question.answered_at).toLocaleString()}`}
          </p>
        </div>
      ) : (
        <div className="mt-3">
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={3}
            placeholder={
              mode === "answer"
                ? "What does this actually mean? Your answer becomes the claim."
                : "Why is this not worth answering?"
            }
            className="w-full resize-y rounded-[--radius-control] border border-line bg-canvas px-2.5 py-2 text-body text-ink placeholder:text-ink-4 focus:border-line-ink focus:outline-none"
          />

          {failure && (
            <p className="mt-2 rounded-[--radius-control] border border-red/25 bg-red-soft px-2.5 py-2 text-body text-red">
              {failure}
            </p>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy || !text.trim()}
              onClick={() => void submit("answer")}
              className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink transition-colors hover:bg-cta-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-4"
            >
              {answering ? "Recording…" : "Answer"}
            </button>
            <button
              type="button"
              disabled={busy || !text.trim()}
              onClick={() => void submit("dismiss")}
              title="Settles the question without establishing anything"
              className="rounded-[--radius-control] border border-line px-3 py-1.5 text-body text-ink-2 hover:bg-raised disabled:text-ink-4"
            >
              Set aside
            </button>
            <span className="text-meta text-ink-3">
              Recorded against <span className="ident">{reviewer}</span>
            </span>
          </div>
        </div>
      )}
    </article>
  );
}

function Toggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-[--radius-control] border px-2.5 py-1 text-meta transition-colors ${
        active
          ? "border-line-ink bg-raised text-ink"
          : "border-line text-ink-3 hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}
