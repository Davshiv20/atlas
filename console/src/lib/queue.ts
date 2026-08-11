import type { Claim, ColumnOutput, Consequence, EvidenceFinding, SchemaOutput, TableOutput } from "@/api/types";
import { claimId, columnClaimId } from "@/lib/review";

/**
 * Table-level review model.
 *
 * The workbench should feel like reading a schema sheet, not clicking through
 * a stack of cards. Every table row is visible; only risky rows are highlighted.
 */

export type Filter = "needs-review" | "weak-trust" | "not-generated" | "all";

/**
 * What a row is asking of the reader.
 *
 * `ungenerated` is not a degree of risk — it is the absence of a claim, and it
 * is kept apart from `yellow` for a reason. Both used to render as "important
 * meaning is missing" on an amber row, so a field the engine never wrote about
 * was indistinguishable from one awaiting approval. A reviewer who had approved
 * everything still saw a screen of amber and concluded their approvals had not
 * saved. A gap is closed by regenerating the table, never by a decision here.
 */
export type Risk = "red" | "yellow" | "ungenerated" | "none";

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
  samples: string[];
  sampleNote?: string;
  findings: EvidenceFinding[];
  lineage: string[];
  consequence: Consequence;
  claim?: Claim;
  risk: Risk;
  reason: string;
}

export interface TableProgress {
  table: TableOutput;
  highlighted: number;
  /** Fields the engine never made a claim about. Counted apart from
   *  `highlighted` so an empty review queue reads as empty. */
  gaps: number;
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

/** A row asking for a decision. A gap asks for a regeneration, so it is not one. */
export function needsReview(row: ReviewRow): boolean {
  return row.risk === "red" || row.risk === "yellow";
}

/** A field the engine never made a claim about. Visible, but not a task. */
export function isGap(row: ReviewRow): boolean {
  return row.risk === "ungenerated";
}

function toProgress(table: TableOutput): TableProgress {
  const rows = allRows(table);
  return {
    table,
    highlighted: rows.filter(needsReview).length,
    gaps: rows.filter(isGap).length,
    weakTrust: rows.filter((row) => (row.claim?.confidence ?? 1) < 0.5).length,
    totalRows: rows.length,
  };
}

function allRows(table: TableOutput): ReviewRow[] {
  const rows: ReviewRow[] = [];

  rows.push({
    // The claim's own id when there is one. Reconstructing it from subject and
    // aspect addressed a claim that did not exist whenever the engine picked a
    // different aspect for the description — `unit` beat `semantics` on any
    // column whose meaning is a measure, and reviewing it returned 404. The
    // reconstructed form survives only as a key for rows with no claim to
    // address, which are exactly the rows that cannot be reviewed anyway.
    id: table.grain?.id ?? claimId(table.name, "grain"),
    table: table.name,
    role: "Grain",
    source: table.qualified_name,
    proposed: table.grain?.text ?? "Not established",
    samples: [`${table.row_count.toLocaleString()} rows`],
    findings: table.grain?.findings ?? [],
    lineage: lineageFor(table, "grain", table.grain),
    consequence: "critical",
    claim: table.grain,
    ...riskFor(table.grain, "critical"),
  });

  rows.push({
    id: table.description?.id ?? claimId(table.name, "semantics"),
    table: table.name,
    role: "Table meaning",
    source: table.qualified_name,
    proposed: table.description?.text ?? table.source_comment ?? "Not established",
    samples: [`${table.columns.length} columns`, `${table.row_count.toLocaleString()} rows`],
    findings: table.description?.findings ?? [],
    lineage: lineageFor(table, "table meaning", table.description),
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
    id: column.description?.id ?? columnClaimId(table.name, column.name),
    table: table.name,
    role: column.name,
    source: columnSummary(column),
    proposed: column.description?.text ?? "No semantic meaning established",
    dataType: column.data_type,
    samples: samplesFor(column),
    sampleNote: sampleNoteFor(column),
    findings: column.description?.findings ?? [],
    lineage: lineageFor(table, column.name, column.description, column.name),
    consequence,
    claim: column.description,
    ...riskFor(column.description, consequence),
  };
}

function riskFor(claim: Claim | undefined, consequence: Consequence): Pick<ReviewRow, "risk" | "reason"> {
  const highImpact = consequence === "critical" || consequence === "high";

  if (!claim) {
    return highImpact
      ? { risk: "ungenerated", reason: "no claim was generated — regenerate the table" }
      : { risk: "none", reason: "routine field, no claim generated" };
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
  if (filter === "not-generated") return entry.gaps > 0;
  return true;
}

function matchesRow(row: ReviewRow, filter: Filter): boolean {
  if (filter === "needs-review") return needsReview(row);
  if (filter === "weak-trust") return (row.claim?.confidence ?? 1) < 0.5;
  if (filter === "not-generated") return isGap(row);
  return true;
}

function byAttention(a: TableProgress, b: TableProgress): number {
  return b.highlighted - a.highlighted || b.weakTrust - a.weakTrust || a.table.name.localeCompare(b.table.name);
}

function samplesFor(column: ColumnOutput): string[] {
  const values = column.sample_values?.map((sample) => `${sample.value} (${sample.count})`) ?? [];
  if (values.length > 0) return values.slice(0, 5);
  const range = column.min_value !== undefined && column.max_value !== undefined
    ? [`${column.min_value} → ${column.max_value}`]
    : [];
  return range;
}

function sampleNoteFor(column: ColumnOutput): string | undefined {
  if (column.sample_values?.length) return undefined;
  if (column.values_withheld_reason) {
    return `${column.values_withheld_reason}. Re-extract with ATLAS_SAMPLE_POLICY=full to show raw samples.`;
  }
  if (column.sampled) return "Profiled from a sample of rows.";
  return "No sample values in this snapshot.";
}

function lineageFor(
  table: TableOutput,
  role: string,
  claim: Claim | undefined,
  column?: string,
): string[] {
  const source = column ? `${table.qualified_name}.${column}` : table.qualified_name;
  const lines = [`source: ${source}`];
  if (claim?.evidence) lines.push(`evidence: ${claim.evidence}`);
  if (claim?.trust) lines.push(`trust: ${claim.trust.state}, ${Math.round(claim.confidence * 100)}/100`);
  if (claim) lines.push(`claim: ${claim.text}`);
  lines.push(`output: semantic_view.yaml → ${table.name}.${role}`);
  return lines;
}

function columnSummary(column: ColumnOutput): string {
  const parts = [column.data_type];
  if (column.is_primary_key) parts.push("pk");
  if (!column.nullable) parts.push("not null");
  if (column.null_fraction !== undefined) parts.push(`${Math.round(column.null_fraction * 100)}% null`);
  if (column.distinct_count !== undefined) parts.push(`${column.distinct_count} distinct`);
  return parts.join(" · ");
}
