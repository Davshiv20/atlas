import { useCallback, useEffect, useState } from "react";

import type { ClaimStatus, TrustFactors } from "@/api/types";
import { describeError } from "@/api/errors";
import { Badge } from "@/components/StatusBadge";
import { Button, Key } from "@/components/ui/Button";
import type { QueueItem } from "@/lib/queue";
import { canVerify, confidenceLabel, trustPercent } from "@/lib/review";
import { useReviewMutation } from "@/store/api";
import { useAppSelector } from "@/store";

/**
 * One decision, with everything needed to take it.
 *
 * Evidence sits under the claim rather than in a panel beside it. Splitting
 * them meant a reviewer read the sentence in one column and what backs it in
 * another, and the two could scroll independently — so the common failure was
 * approving a claim without having looked at the check under it.
 */
export function ClaimCard({
  item,
  workspace,
  onSettled,
}: {
  item: QueueItem;
  workspace: string;
  onSettled: () => void;
}) {
  const reviewer = useAppSelector((s) => s.ui.reviewer);
  const [review, { isLoading, error, reset }] = useReviewMutation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.claim.text);

  const { label, detail } = confidenceLabel(item.claim);
  const verifiable = canVerify(item.claim);

  useEffect(() => {
    setEditing(false);
    setDraft(item.claim.text);
    reset();
  }, [item.id, item.claim.text, reset]);

  const submit = useCallback(
    async (decision: ClaimStatus, text?: string) => {
      try {
        await review({
          workspace,
          claimId: item.id,
          body: { decision, reviewer, ...(text !== undefined ? { claim: text } : {}) },
        }).unwrap();
        setEditing(false);
        // No toast: the item leaving the queue is the confirmation, and one
        // per approval would be noise in a run of forty.
        onSettled();
      } catch {
        /* surfaced from `error` below */
      }
    },
    [item.id, workspace, reviewer, review, onSettled],
  );

  useEffect(() => {
    if (editing) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      const key = event.key.toLowerCase();
      if (key === "a" && verifiable) {
        event.preventDefault();
        void submit("verified");
      } else if (key === "e") {
        event.preventDefault();
        setEditing(true);
      } else if (key === "r") {
        event.preventDefault();
        void submit("rejected");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [editing, verifiable, submit]);

  return (
    <article className="rounded-[--radius-panel] border border-line bg-surface p-5">
      <header className="flex flex-wrap items-center gap-2">
        <span className="ident text-table text-ink">{item.table}</span>
        <span className="text-body text-ink-3">{item.label}</span>
        {!item.claim.grounded && <Badge tone="generated">AI generated</Badge>}
        <span
          className={`ml-auto rounded-full px-2 py-[2px] text-meta font-medium ${
            item.claim.confidence < 0.5
              ? "bg-amber-soft text-amber-strong"
              : "bg-teal-soft text-teal-strong"
          }`}
          title="Evidence-derived trust score; not a probability"
        >
          Trust {trustPercent(item.claim.confidence)}/100 · {label}
        </span>
      </header>

      {editing ? (
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={4}
          autoFocus
          className="mt-3 w-full resize-y rounded-[--radius-control] border border-line bg-canvas px-3 py-2 text-table text-ink focus:border-line-ink focus:outline-none"
        />
      ) : (
        <p className="mt-3 text-table text-ink">{item.claim.text}</p>
      )}

      <div className="mt-4 grid gap-4 sm:grid-cols-[minmax(0,1fr)_200px]">
        <div>
          <p className="text-meta font-semibold uppercase tracking-wide text-ink-3">Evidence</p>
          {item.claim.evidence ? (
            <pre className="ident mt-1.5 whitespace-pre-wrap rounded-[--radius-control] border border-line bg-raised px-3 py-2 text-ink-2">
              {item.claim.evidence}
            </pre>
          ) : (
            <p className="mt-1.5 text-body text-ink-3">
              No check was run that could have contradicted this.
            </p>
          )}
        </div>

        <div>
          <p className="text-meta font-semibold uppercase tracking-wide text-ink-3">
            Trust basis
          </p>
          <p className="mt-1.5 text-body text-ink-2">{detail}</p>
        </div>
      </div>

      {item.claim.trust && (
        <TrustBreakdown
          factors={item.claim.trust.factors}
          limitations={item.claim.trust.limitations}
        />
      )}

      {error && (
        <p className="mt-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {describeError(error, "The review could not be saved.")}
        </p>
      )}

      {/* Shortcut hints sit beside their button, never inside it: a bordered
          chip within a filled control is what made these read as lumpy. */}
      <footer className="mt-4 flex flex-wrap items-center gap-2">
        {editing ? (
          <>
            <Button
              variant="primary"
              disabled={isLoading || !draft.trim()}
              onClick={() => void submit("verified", draft.trim())}
            >
              Save and approve
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setEditing(false);
                setDraft(item.claim.text);
              }}
            >
              Cancel
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="primary"
              disabled={isLoading || !verifiable}
              onClick={() => void submit("verified")}
              title={
                verifiable
                  ? undefined
                  : "Nothing was run that could have contradicted this — edit it or ask a question instead"
              }
            >
              Approve
            </Button>
            <Key>A</Key>
            <Button variant="secondary" onClick={() => setEditing(true)}>
              Edit
            </Button>
            <Key>E</Key>
            <Button variant="danger" disabled={isLoading} onClick={() => void submit("rejected")}>
              Reject
            </Button>
            <Key>R</Key>
          </>
        )}
      </footer>
    </article>
  );
}

function TrustBreakdown({
  factors,
  limitations,
}: {
  factors: TrustFactors;
  limitations: string[];
}) {
  const entries: [string, number][] = [
    ["Directness", factors.evidence_directness],
    ["Authority", factors.authority],
    ["Coverage", factors.coverage],
    ["Consistency", factors.consistency],
    ["Freshness", factors.freshness],
  ];

  return (
    <section className="mt-4 border-t border-line pt-3">
      <p className="text-meta font-semibold uppercase tracking-wide text-ink-3">
        Trust score factors
      </p>
      <div className="mt-2 grid gap-2 sm:grid-cols-5">
        {entries.map(([name, value]) => (
          <div key={name} className="rounded-[--radius-control] bg-raised px-2.5 py-2">
            <div className="flex items-center justify-between gap-2 text-meta text-ink-3">
              <span>{name}</span>
              <span className="tabular-nums">{Math.round(value * 100)}</span>
            </div>
            <span className="mt-1 block h-1 overflow-hidden rounded-full bg-sunken">
              <span
                className="block h-full rounded-full bg-teal"
                style={{ width: `${Math.round(value * 100)}%` }}
              />
            </span>
          </div>
        ))}
      </div>
      {limitations.length > 0 && (
        <p className="mt-2 text-meta text-ink-3">
          <span className="font-semibold">Limits:</span> {limitations.slice(0, 2).join(" · ")}
        </p>
      )}
    </section>
  );
}

