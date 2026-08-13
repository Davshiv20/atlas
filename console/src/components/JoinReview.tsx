import type { Edge } from "@/lib/layout";
import { ClaimCard } from "@/components/ClaimCard";
import { Badge } from "@/components/StatusBadge";

/**
 * Judging a relationship where a relationship is legible.
 *
 * A join is the one claim shape a reader cannot check by reading. The review
 * queue can only render it as a sentence — "engagements.updated_by references
 * users.id" — and nothing about that sentence tells you whether it is
 * plausible. On the map it is a line between two boxes you are already looking
 * at, and the two ends are named.
 *
 * The map also already sorts the reviewable from the settled, in the one way
 * that matters: a solid edge is a constraint the database itself enforces and
 * there is nothing here to decide, while a dashed one is somebody's inference.
 * That distinction is drawn before anyone clicks.
 */
export function JoinReview({
  edge,
  workspace,
  onClose,
  onSettled,
}: {
  edge: Edge;
  workspace: string;
  onClose: () => void;
  onSettled: () => void;
}) {
  const target = edge.referredColumns.length
    ? `${edge.to}.${edge.referredColumns.join(", ")}`
    : edge.to;

  return (
    <aside className="flex min-h-0 flex-1 flex-col border-l border-line bg-paper">
      <header className="flex shrink-0 items-start gap-2 border-b border-line px-4 py-3">
        <div className="min-w-0 flex-1">
          <p className="text-meta font-semibold uppercase tracking-wide text-ink-3">
            Relationship
          </p>
          {/* Stated in full rather than as the edge label. A reviewer deciding
              whether this join is real needs both sides and both columns, and
              the map can only show one column without crowding the line. */}
          <p className="ident mt-1 break-words text-ink">
            {edge.from}.{edge.via} <span className="text-ink-3">→</span> {target}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          title="Close"
          aria-label="Close the relationship"
          className="-mr-1 shrink-0 rounded-[--radius-control] px-1.5 py-1 text-ink-3 hover:text-ink"
        >
          <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden>
            <path d="M1 1l9 9M10 1l-9 9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
        {edge.enforced ? (
          <>
            <Badge tone="validated">Enforced by the database</Badge>
            <p className="mt-3 text-body text-ink-2">
              A declared foreign key. The database refuses to hold a row that violates it,
              so there is no judgement to make here — approving it would add a human
              opinion to something already guaranteed.
            </p>
          </>
        ) : !edge.claim ? (
          <>
            <Badge tone="review">Not enforced</Badge>
            <p className="mt-3 text-body text-ink-2">
              The map drew this from the schema, but no claim was written about it — so
              there is nothing to approve yet. Analysing{" "}
              <span className="ident text-ink">{edge.from}</span> is what produces one.
            </p>
          </>
        ) : (
          <>
            {/* Said before the claim, not after. Whether the database guarantees
                this is the single most important thing about a relationship,
                and a reviewer who reads the sentence first has already formed a
                view by the time they reach it. */}
            <p className="mb-3 text-body text-ink-2">
              Not enforced by the database. Whatever holds here holds because the data
              currently agrees, which is what the check below measured.
            </p>
            <ClaimCard
              item={{
                id: edge.claim.id,
                table: edge.from,
                label: `${edge.from}.${edge.via} → ${target}`,
                kind: "join",
                claim: edge.claim,
              }}
              workspace={workspace}
              onSettled={onSettled}
            />
          </>
        )}
      </div>
    </aside>
  );
}
