import type { Claim, ColumnOutput, ReviewState, TableOutput } from "@/api/types";

/**
 * The engine's id scheme, mirrored so the console can address a claim.
 *
 * Plural aspects (join, quality, metric, lifecycle) carry a discriminator so a
 * subject can hold several of them; grain and semantics are singular and take
 * none. See `PLURAL_ASPECTS` in the engine's facts.py.
 */
export function claimId(subject: string, aspect: string, discriminator?: string): string {
  return discriminator ? `${subject}#${aspect}#${discriminator}` : `${subject}#${aspect}`;
}

export function columnClaimId(table: string, column: string): string {
  return claimId(`${table}.${column}`, "semantics");
}

/**
 * counted over *consequential* claims only.
 *
 * A table with 800 columns is validated when every claim that could make an
 * agent write wrong SQL has been judged — not when all 800 have. Counting
 * routine claims here is what made the number unreachable and therefore
 * meaningless.
 */
export function reviewState(table: TableOutput): ReviewState {
  if (!table.analyzed) return "not-generated";
  const { critical_total, critical_settled, high_total, high_settled } = table.validation;
  const total = critical_total + high_total;
  if (total === 0) return "not-generated";

  const settled = critical_settled + high_settled;
  if (settled === total) return "validated";
  if (settled > 0) return "partial";
  return "needs-review";
}

/** Consequential claims only, matching what `reviewState` reports. */
export function reviewCounts(table: TableOutput): { verified: number; total: number } {
  const { critical_total, critical_settled, high_total, high_settled } = table.validation;
  return {
    verified: critical_settled + high_settled,
    total: critical_total + high_total,
  };
}

/**
 * Whether a claim can be promoted to verified.
 *
 * The engine returns 409 for an ungrounded claim, so the button is disabled
 * with the reason shown rather than left enabled to fail. Predicting the
 * server's rule client-side is a duplication risk, but the alternative — a
 * primary action that always errors — is worse.
 */
export function canVerify(claim: Pick<Claim, "grounded">): boolean {
  return claim.grounded;
}

/** confidence in words, since a bare 0.75 means nothing. */
export function confidenceLabel(claim: Pick<Claim, "confidence" | "grounded">): {
  label: string;
  detail: string;
} {
  if (!claim.grounded) {
    return {
      label: "Unverified guess",
      detail:
        "No query was run that could have contradicted this. It rests on the column name, its shape, and sample values alone.",
    };
  }
  if (claim.confidence >= 0.9) {
    return {
      label: "High confidence",
      detail:
        "A check ran that could have contradicted this and did not — on the data present at capture. That is not a guarantee about data you have not seen.",
    };
  }
  if (claim.confidence >= 0.7) {
    return {
      label: "Medium confidence",
      detail:
        "Backed by an executed check, but the model was itself unsure. Worth reading the evidence before approving.",
    };
  }
  return {
    label: "Low confidence",
    detail: "Backed by a check, but the model reported low certainty.",
  };
}

export function columnSummary(column: ColumnOutput): string {
  const parts = [column.data_type];
  if (column.is_primary_key) parts.push("pk");
  if (!column.nullable) parts.push("not null");
  if (column.null_fraction) parts.push(`${Math.round(column.null_fraction * 100)}% null`);
  if (column.distinct_count !== undefined) parts.push(`${column.distinct_count} distinct`);
  return parts.join(" · ");
}
