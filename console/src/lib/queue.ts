import type { Claim, SchemaOutput, TableOutput } from "@/api/types";
import { claimId, columnClaimId } from "@/lib/review";

/**
 * The review queue: every decision still outstanding, in the order it should
 * be taken.
 *
 * The table list was never the work. It answered "what exists", and a reviewer
 * then had to open each table and hunt for the claims inside it that nobody
 * had judged. This inverts that — the claims are the list, and the tables are
 * how far through it you are.
 */

export interface QueueItem {
  id: string;
  table: string;
  /** `grain`, `table description`, or the column name — what is being judged. */
  label: string;
  kind: "grain" | "description" | "column";
  claim: Claim;
}

export interface TableProgress {
  table: TableOutput;
  settled: number;
  total: number;
  /** Outstanding claims, so the ledger and the queue can never disagree. */
  outstanding: number;
}

export interface Queue {
  items: QueueItem[];
  inProgress: TableProgress[];
  done: TableProgress[];
}

/** Critical first: a wrong grain makes an agent write silently wrong SQL. */
const WEIGHT = { critical: 0, high: 1, routine: 2 } as const;

export function buildQueue(output: SchemaOutput, filter: Filter = "needs-review"): Queue {
  const progress = output.tables
    .filter((table) => table.analyzed)
    .map(toProgress)
    .sort((a, b) => b.outstanding - a.outstanding || a.table.name.localeCompare(b.table.name));

  const items = progress
    .filter((entry) => entry.outstanding > 0)
    .flatMap((entry) => claimsOf(entry.table))
    .filter((item) => matches(item, filter))
    .sort(byUrgency);

  return {
    items,
    inProgress: progress.filter((entry) => entry.outstanding > 0),
    done: progress.filter((entry) => entry.outstanding === 0),
  };
}

export type Filter = "needs-review" | "low-confidence" | "all";

export function countFor(output: SchemaOutput, filter: Filter): number {
  return buildQueue(output, filter).items.length;
}

function matches(item: QueueItem, filter: Filter): boolean {
  if (filter === "low-confidence") return item.claim.confidence < 0.7;
  return true;
}

/**
 * Consequence first, then least-confident.
 *
 * Confidence ascending rather than descending on purpose: the claims a
 * reviewer can most improve are the ones the evidence supports least, and a
 * queue sorted the other way spends the first hour approving things that were
 * already near-certain.
 */
function byUrgency(a: QueueItem, b: QueueItem): number {
  const weight = WEIGHT[a.claim.consequence] - WEIGHT[b.claim.consequence];
  if (weight !== 0) return weight;
  if (a.claim.confidence !== b.claim.confidence) return a.claim.confidence - b.claim.confidence;
  return a.id.localeCompare(b.id);
}

function toProgress(table: TableOutput): TableProgress {
  const { critical_total, critical_settled, high_total, high_settled } = table.validation;
  const total = critical_total + high_total;
  const settled = critical_settled + high_settled;
  return { table, settled, total, outstanding: claimsOf(table).length };
}

/** Every unsettled claim on a table, as queue items. */
function claimsOf(table: TableOutput): QueueItem[] {
  const items: QueueItem[] = [];

  if (unsettled(table.grain)) {
    items.push({
      id: claimId(table.name, "grain"),
      table: table.name,
      label: "grain",
      kind: "grain",
      claim: table.grain!,
    });
  }
  if (unsettled(table.description)) {
    items.push({
      id: claimId(table.name, "semantics"),
      table: table.name,
      label: "table description",
      kind: "description",
      claim: table.description!,
    });
  }
  for (const column of table.columns) {
    if (!unsettled(column.description)) continue;
    items.push({
      id: columnClaimId(table.name, column.name),
      table: table.name,
      label: column.name,
      kind: "column",
      claim: column.description!,
    });
  }
  return items;
}

/**
 * Whether this claim still needs a person.
 *
 * Auto-accepted is settled: those are routine claims whose shape already said
 * everything, and putting them in front of a reviewer is what made the queue
 * feel endless when almost none of it was decidable.
 */
function unsettled(claim: Claim | undefined): boolean {
  return claim?.status === "unverified";
}

/** The settled claims for a table, newest decision first — shown as context. */
export function settledOf(table: TableOutput): QueueItem[] {
  const all: (QueueItem | null)[] = [
    table.grain && !unsettled(table.grain)
      ? {
          id: claimId(table.name, "grain"),
          table: table.name,
          label: "grain",
          kind: "grain" as const,
          claim: table.grain,
        }
      : null,
    table.description && !unsettled(table.description)
      ? {
          id: claimId(table.name, "semantics"),
          table: table.name,
          label: "table description",
          kind: "description" as const,
          claim: table.description,
        }
      : null,
  ];
  return all.filter((item): item is QueueItem => item !== null);
}
