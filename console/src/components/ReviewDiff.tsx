import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ClaimStatus, TableOutput } from "@/api/types";
import { describeError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { isGap, needsReview, rowsFor, type ReviewRow } from "@/lib/queue";
import { canVerify, trustPercent } from "@/lib/review";
import { useReviewMutation } from "@/store/api";
import { useAppSelector } from "@/store";

/**
 * Review as a diff.
 *
 * The schema is the pull request, a table is a file, a claim is a hunk. What
 * the database physically shows is the context line; what Atlas proposes it
 * *means* is the added line. One reading column, a gutter carrying state, and
 * hairlines — no grid, no cards, no per-row control strip.
 *
 * The sheet this replaces gave 221 rows equal weight and asked a reviewer to
 * scan them. A diff gives a queue and asks for a decision.
 *
 * Nothing reaches the engine until Submit. That is what makes single-key
 * acting safe, and it is why `a` advances: the dominant path is a run of
 * confirmations broken by the occasional edit.
 */

type Decision = "endorse" | "dispute";

interface Staged {
  decision: Decision;
  text?: string;
}

/** The gutter alphabet. Legible with no colour at all. */
function markFor(row: ReviewRow, staged: Staged | undefined): {
  glyph: string;
  tone: string;
  label: string;
} {
  if (staged) {
    return staged.decision === "endorse"
      ? { glyph: "✓", tone: "text-cta", label: "staged" }
      : { glyph: "✗", tone: "text-cta", label: "staged" };
  }
  if (isGap(row)) return { glyph: "·", tone: "text-ink-4", label: "not generated" };
  if (row.risk === "red") return { glyph: "✗", tone: "text-red", label: "contradicted" };
  if (row.risk === "yellow") return { glyph: "?", tone: "text-amber", label: "needs you" };
  if (row.claim) return { glyph: "✓", tone: "text-teal", label: "settled" };
  return { glyph: "·", tone: "text-ink-4", label: "not generated" };
}

export function ReviewDiff({
  table,
  workspace,
  analysed,
}: {
  table: TableOutput;
  workspace: string;
  /** False when nothing in this table has been analysed. */
  analysed: boolean;
}) {
  const reviewer = useAppSelector((state) => state.ui.reviewer);
  const [review, { isLoading: submitting }] = useReviewMutation();
  const [staged, setStaged] = useState<Record<string, Staged>>({});
  const [cursor, setCursor] = useState(0);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const cursorRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => rowsFor(table, "all"), [table]);
  // Only rows a person can actually decide. A field with no claim is a gap; it
  // is shown, but it is not a stop on the way through.
  const actionable = useMemo(() => rows.filter((row) => Boolean(row.claim)), [rows]);
  const current = actionable[Math.min(cursor, actionable.length - 1)];

  useEffect(() => {
    setStaged({});
    setCursor(0);
    setEditing(null);
    setFailure(null);
  }, [table.name]);

  // Deliberately not `scrollIntoView`. That scrolls every scrollable ancestor
  // on both axes — and an `overflow: hidden` ancestor is still programmatically
  // scrollable — so a focused row even slightly wider than its container made
  // the browser shift the whole application sideways to reveal it. The sidebar
  // and the title slid off the left edge of the window.
  //
  // This moves one element's scrollTop and can do nothing else.
  useEffect(() => {
    const scroller = scrollerRef.current;
    const focused = cursorRef.current;
    if (!scroller || !focused) return;

    const view = scroller.getBoundingClientRect();
    const row = focused.getBoundingClientRect();
    // Clears the sticky file header when scrolling back up.
    const margin = 72;

    if (row.top < view.top + margin) {
      scroller.scrollTop -= view.top + margin - row.top;
    } else if (row.bottom > view.bottom) {
      scroller.scrollTop += row.bottom - view.bottom + 12;
    }
  }, [cursor]);

  const stage = useCallback(
    (row: ReviewRow, decision: Decision, text?: string) => {
      setStaged((held) => ({ ...held, [row.id]: { decision, ...(text ? { text } : {}) } }));
      setEditing(null);
      // Advance. One key per claim is the whole point of staging being safe.
      setCursor((at) => Math.min(at + 1, Math.max(actionable.length - 1, 0)));
    },
    [actionable.length],
  );

  const submit = useCallback(async () => {
    setFailure(null);
    const entries = Object.entries(staged);
    for (const [claimId, decision] of entries) {
      try {
        await review({
          workspace,
          claimId,
          body: {
            decision: (decision.decision === "endorse"
              ? "verified"
              : "rejected") as ClaimStatus,
            reviewer,
            ...(decision.text ? { claim: decision.text } : {}),
          },
        }).unwrap();
      } catch (error) {
        setFailure(describeError(error, `Could not record the decision on ${claimId}.`));
        return;
      }
    }
    setStaged({});
  }, [staged, review, workspace, reviewer]);

  useEffect(() => {
    if (editing !== null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) {
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
          event.preventDefault();
          void submit();
        }
        return;
      }
      if (event.repeat) return;

      const key = event.key.toLowerCase();
      if (key === "j" || key === "k") {
        event.preventDefault();
        setCursor((at) =>
          Math.min(Math.max(at + (key === "j" ? 1 : -1), 0), Math.max(actionable.length - 1, 0)),
        );
        return;
      }
      if (!current) return;
      if (key === "a" && canVerify(current.claim!)) {
        event.preventDefault();
        stage(current, "endorse");
      } else if (key === "r") {
        event.preventDefault();
        stage(current, "dispute");
      } else if (key === "e") {
        event.preventDefault();
        setDraft(current.claim?.text ?? current.proposed);
        setEditing(current.id);
      } else if (key === "u") {
        event.preventDefault();
        setStaged((held) => {
          const keys = Object.keys(held);
          if (keys.length === 0) return held;
          const { [keys[keys.length - 1]!]: _dropped, ...rest } = held;
          return rest;
        });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [actionable.length, current, editing, stage, submit]);

  const stagedCount = Object.keys(staged).length;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <div ref={scrollerRef} className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
        {/* The file header. Grain and table meaning describe the file itself,
            not a line in it, so they stay pinned while the columns scroll. */}
        <header className="sticky top-0 z-10 border-b border-line bg-canvas/95 px-6 py-3 backdrop-blur">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <h2 className="ident text-table text-ink">{table.qualified_name}</h2>
            <span className="text-meta tabular-nums text-ink-3">
              {table.row_count.toLocaleString()} rows · {table.columns.length} columns
            </span>
          </div>
          {analysed ? (
            <dl className="mt-2 grid grid-cols-[70px_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-meta">
              <dt className="text-ink-4">grain</dt>
              <dd className="text-ink-2">{table.grain?.text ?? "not established"}</dd>
              <dt className="text-ink-4">meaning</dt>
              <dd className="text-ink-2">{table.description?.text ?? "not established"}</dd>
            </dl>
          ) : (
            <p className="mt-1.5 text-meta text-ink-3">
              Captured, not analysed. The shape below is what Atlas read from the database;
              no meaning has been proposed for any of it yet.
            </p>
          )}
        </header>

        <div className="pb-24">
          {rows.map((row, index) => (
            <Hunk
              key={row.id}
              row={row}
              staged={staged[row.id]}
              focused={Boolean(current) && current!.id === row.id}
              cursorRef={Boolean(current) && current!.id === row.id ? cursorRef : undefined}
              editing={editing === row.id}
              draft={draft}
              onDraft={setDraft}
              onFocus={() => {
                const at = actionable.findIndex((candidate) => candidate.id === row.id);
                if (at >= 0) setCursor(at);
              }}
              onStage={(decision, text) => stage(row, decision, text)}
              onEdit={() => {
                setDraft(row.claim?.text ?? row.proposed);
                setEditing(row.id);
              }}
              onCancelEdit={() => setEditing(null)}
              analysed={analysed}
              position={index}
            />
          ))}
        </div>
      </div>

      {(stagedCount > 0 || failure) && (
        <footer className="shrink-0 border-t border-line bg-surface px-6 py-2.5">
          {failure && <p className="mb-2 text-meta text-red">{failure}</p>}
          <div className="flex flex-wrap items-center gap-3 text-meta text-ink-3">
            <span>
              <b className="text-cta">{stagedCount}</b> staged
            </span>
            <span className="text-ink-4">
              nothing has reached the engine yet · <kbd className="font-mono">u</kbd> undo
            </span>
            <span className="ml-auto flex gap-2">
              <Button size="sm" variant="ghost" onClick={() => setStaged({})}>
                Discard
              </Button>
              <Button size="sm" variant="primary" disabled={submitting} onClick={() => void submit()}>
                {submitting ? "Submitting…" : "Submit review"}
              </Button>
            </span>
          </div>
        </footer>
      )}
    </div>
  );
}

function Hunk({
  row,
  staged,
  focused,
  cursorRef,
  editing,
  draft,
  onDraft,
  onFocus,
  onStage,
  onEdit,
  onCancelEdit,
  analysed,
  position,
}: {
  row: ReviewRow;
  staged: Staged | undefined;
  focused: boolean;
  cursorRef?: React.RefObject<HTMLDivElement | null>;
  editing: boolean;
  draft: string;
  onDraft: (value: string) => void;
  onFocus: () => void;
  onStage: (decision: Decision, text?: string) => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  analysed: boolean;
  position: number;
}) {
  const mark = markFor(row, staged);
  const decidable = Boolean(row.claim);
  const finding = row.findings[0];

  return (
    <div
      ref={cursorRef}
      onClick={onFocus}
      className={`grid grid-cols-[2.4ch_minmax(0,1fr)] gap-x-3 border-b border-line/60 px-4 py-2.5 ${
        focused ? "bg-surface" : ""
      }`}
    >
      <span className={`select-none text-center font-mono text-body ${mark.tone}`} aria-hidden>
        {mark.glyph}
      </span>

      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <span className="ident shrink-0 text-ink">{row.role}</span>
          <span className="min-w-0 truncate text-meta text-ink-3" title={row.source}>
            {row.source}
          </span>
          {staged ? (
            <span className="ml-auto shrink-0 text-meta font-semibold text-cta">
              {staged.decision === "endorse" ? "staged · endorse" : "staged · dispute"}
            </span>
          ) : row.claim ? (
            <span className="ml-auto shrink-0 text-meta tabular-nums text-ink-3">
              {trustPercent(row.claim.confidence)} trust
            </span>
          ) : null}
        </div>

        {row.samples.length > 0 && (
          <p className="ident mt-1 truncate text-meta text-ink-3" title={row.samples.join("  ")}>
            {row.samples.join("   ")}
          </p>
        )}
        {row.samples.length === 0 && row.sampleNote && (
          <p className="mt-1 text-meta text-ink-4">{row.sampleNote}</p>
        )}

        {editing ? (
          <div className="mt-2">
            <textarea
              value={draft}
              onChange={(event) => onDraft(event.target.value)}
              rows={3}
              autoFocus
              className="w-full resize-y rounded-[--radius-control] border border-line bg-canvas px-2.5 py-2 text-body text-ink focus:border-line-ink focus:outline-none"
            />
            <div className="mt-1.5 flex gap-2">
              <Button
                size="sm"
                variant="primary"
                disabled={!draft.trim()}
                onClick={() => onStage("endorse", draft.trim())}
              >
                Stage this wording
              </Button>
              <Button size="sm" variant="ghost" onClick={onCancelEdit}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <p
            className={`mt-1.5 pl-3.5 ${
              decidable ? "text-body text-ink" : "text-body text-ink-4"
            }`}
          >
            <span
              className={`-ml-3.5 mr-1.5 font-mono ${decidable ? "text-teal" : "text-line-strong"}`}
              aria-hidden
            >
              {decidable ? "+" : "░"}
            </span>
            {staged?.text ?? (decidable ? row.claim!.text : messageFor(row, analysed))}
          </p>
        )}

        {finding && (
          <p className="mt-1 pl-3.5 text-meta text-ink-3">
            <span
              className={`-ml-3.5 mr-1.5 font-mono ${
                finding.relationship === "contradicts" || finding.verdict === "failed"
                  ? "text-red"
                  : "text-teal"
              }`}
              aria-hidden
            >
              {finding.relationship === "contradicts" || finding.verdict === "failed" ? "✗" : "✓"}
            </span>
            {finding.result}
          </p>
        )}

        {focused && decidable && !editing && !staged && (
          <p className="mt-2 flex items-center gap-3 text-meta text-ink-4">
            {canVerify(row.claim!) ? (
              <button type="button" className="hover:text-ink" onClick={() => onStage("endorse")}>
                <kbd className="font-mono text-ink-3">a</kbd> endorse
              </button>
            ) : (
              <span title="Nothing has tested this claim yet">
                <kbd className="font-mono">a</kbd> endorse · grounds it on your say-so
              </span>
            )}
            <button type="button" className="hover:text-ink" onClick={onEdit}>
              <kbd className="font-mono text-ink-3">e</kbd> edit
            </button>
            <button type="button" className="hover:text-ink" onClick={() => onStage("dispute")}>
              <kbd className="font-mono text-ink-3">r</kbd> dispute
            </button>
            <span className="ml-auto tabular-nums">#{position + 1}</span>
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * What to say where no claim exists.
 *
 * "Regenerate the table" is only true once the table has been analysed and the
 * engine still produced nothing for this field. On a table nobody has analysed
 * the answer is to run the analysis, and telling a reviewer to regenerate
 * something that was never generated sends them nowhere.
 */
function messageFor(row: ReviewRow, analysed: boolean): string {
  if (!analysed) return "no meaning proposed yet — run Generate semantic view";
  if (needsReview(row)) return row.reason;
  return "the engine proposed nothing here — regenerating this table would retry it";
}
