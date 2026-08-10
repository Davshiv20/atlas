import type { Claim, ColumnOutput, Consequence, SchemaOutput, TableOutput } from "@/api/types";
import { claimId, columnClaimId } from "@/lib/review";

/**
 * Table-level review model.
 *
 * The workbench should feel like reading a schema sheet, not clicking through
 * a stack of cards. Every table row is visible; only risky rows are highlighted.
 */

export type Filter = "needs-review" | "weak-trust" | "all";
export type Risk = "red" | "yellow" | "none";

export interface QueueItem {
  id: string;
  table: string;
  label: string;
  kind: "grain" | "description" | "column";
  claim: Claim;
}

export interface ReviewRow {
  id: string;
  table: string;
  role: string;
  source: string;
  proposed: string;
  dataType?: string;
  consequence: Consequence;
  claim?: Claim;
  risk: Risk;
  reason: string;
}

export interface TableProgress {
  table: TableOutput;
  highlighted: number;
  weakTrust: number;
  totalRows: number;
}

export interface Queue {
  tables: TableProgress[];
  needsReview: TableProgress[];
  quiet: TableProgress[];
}

export function buildQueue(output: SchemaOutput, filter: Filter = "needs-review"): Queue {
  const tables = output.tables
    .filter((table) => table.analyzed)
    .map(toProgress)
    .filter((entry) => matchesTable(entry, filter))
    .sort(byAttention);

  return {
    tables,
    needsReview: tables.filter((entry) => entry.highlighted > 0),
    quiet: tables.filter((entry) => entry.highlighted === 0),
  };
}

export function countFor(output: SchemaOutput, filter: Filter): number {
  if (filter === "all") return output.tables.filter((table) => table.analyzed).length;
  return buildQueue(output, filter).tables.length;
}

export function rowsFor(table: TableOutput, filter: Filter = "all"): ReviewRow[] {
  return allRows(table).filter((row) => matchesRow(row, filter));
}

export function needsReview(row: ReviewRow): boolean {
  return row.risk !== "none";
}

function toProgress(table: TableOutput): TableProgress {
  const rows = allRows(table);
  return {
    table,
    highlighted: rows.filter(needsReview).length,
    weakTrust: rows.filter((row) => (row.claim?.confidence ?? 1) < 0.5).length,
    totalRows: rows.length,
  };
}

function allRows(table: TableOutput): ReviewRow[] {
  const rows: ReviewRow[] = [];

  rows.push({
    id: claimId(table.name, "grain"),
    table: table.name,
    role: "Grain",
    source: table.qualified_name,
    proposed: table.grain?.text ?? "Not established",
    consequence: "critical",
    claim: table.grain,
    ...riskFor(table.grain, "critical"),
  });

  rows.push({
    id: claimId(table.name, "semantics"),
    table: table.name,
    role: "Table meaning",
    source: table.qualified_name,
    proposed: table.description?.text ?? table.source_comment ?? "Not established",
    consequence: "high",
    claim: table.description,
    ...riskFor(table.description, "high"),
  });

  for (const column of table.columns) {
    rows.push(rowForColumn(table, column));
  }

  return rows;
}

function rowForColumn(table: TableOutput, column: ColumnOutput): ReviewRow {
  const consequence = column.consequence;
  return {
    id: columnClaimId(table.name, column.name),
    table: table.name,
    role: column.name,
    source: columnSummary(column),
    proposed: column.description?.text ?? "No semantic meaning established",
    dataType: column.data_type,
    consequence,
    claim: column.description,
    ...riskFor(column.description, consequence),
  };
}

function riskFor(claim: Claim | undefined, consequence: Consequence): Pick<ReviewRow, "risk" | "reason"> {
  const highImpact = consequence === "critical" || consequence === "high";

  if (!claim) {
    return highImpact
      ? { risk: "yellow", reason: "important meaning is missing" }
      : { risk: "none", reason: "routine field without established meaning" };
  }

  if (claim.status !== "unverified") {
    return { risk: "none", reason: claim.status.replace("_", " ") };
  }

  if (claim.trust?.state === "contradicted") {
    return { risk: "red", reason: "conflicting evidence" };
  }

  if (claim.confidence < 0.25) {
    return highImpact
      ? { risk: "red", reason: "important claim has very weak support" }
      : { risk: "none", reason: "weak support, but routine impact" };
  }

  if (highImpact) {
    return { risk: "yellow", reason: "important claim is not validated" };
  }

  return { risk: "none", reason: "routine claim can be left inferred" };
}

function matchesTable(entry: TableProgress, filter: Filter): boolean {
  if (filter === "needs-review") return entry.highlighted > 0;
  if (filter === "weak-trust") return entry.weakTrust > 0;
  return true;
}

function matchesRow(row: ReviewRow, filter: Filter): boolean {
  if (filter === "needs-review") return needsReview(row);
  if (filter === "weak-trust") return (row.claim?.confidence ?? 1) < 0.5;
  return true;
}

function byAttention(a: TableProgress, b: TableProgress): number {
  return b.highlighted - a.highlighted || b.weakTrust - a.weakTrust || a.table.name.localeCompare(b.table.name);
}

function columnSummary(column: ColumnOutput): string {
  const parts = [column.data_type];
  if (column.is_primary_key) parts.push("pk");
  if (!column.nullable) parts.push("not null");
  if (column.null_fraction !== undefined) parts.push(`${Math.round(column.null_fraction * 100)}% null`);
  if (column.distinct_count !== undefined) parts.push(`${column.distinct_count} distinct`);
  return parts.join(" · ");
}
