import type { Claim, ColumnOutput, ReviewState, TableOutput } from "@/api/types";

/*
 * A placeholder id for a row that has no claim behind it.
 *
 * This is NOT how a claim is addressed. The engine owns the id scheme and emits
 * it on every claim (`Claim.id`); mirroring it here is what broke review, since
 * a column's description is whichever of the engine's DESCRIPTION_ASPECTS
 * scored highest — so assuming `#semantics` addressed a claim that did not
 * exist for every column a `unit` claim won, and the review 404'd.
 *
 * The review sheet still renders a row for a claim that was never made
 * ("Not established"), and React needs a stable key for it. That is the only
 * remaining use: a row reaching for one of these has nothing to review anyway.
 * Always prefer `claim.id` when a claim exists.
 */
export function claimId(subject: string, aspect: string): string {
  return `${subject}#${aspect}`;
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

/**
 * Confidence in words. It is an evidence-derived trust score, never the model's
 * estimate of its own probability of being correct.
 */
export function confidenceLabel(claim: Pick<Claim, "confidence" | "grounded" | "trust">): {
  label: string;
  detail: string;
} {
  if (claim.trust) {
    const label = claim.trust.band
      .split("_")
      .map((word) => word[0]!.toUpperCase() + word.slice(1))
      .join(" ");
    const detail = claim.trust.reasons.slice(0, 2).join(" · ");
    return {
      label,
      detail: detail || "Calculated from the evidence factors shown below.",
    };
  }

  // Legacy claims retain their old scalar until they are regenerated. Be
  // explicit about the missing breakdown rather than reverse-engineering one.
  return {
    label: claim.grounded ? "Legacy trust score" : "Legacy unsupported score",
    detail:
      "This claim predates trust-factor breakdowns. Regenerate it to see directness, authority, coverage, consistency, and freshness.",
  };
}

export function trustPercent(confidence: number): number {
  return Math.round(confidence * 100);
}

export function columnSummary(column: ColumnOutput): string {
  const parts = [column.data_type];
  if (column.is_primary_key) parts.push("pk");
  if (!column.nullable) parts.push("not null");
  if (column.null_fraction) parts.push(`${Math.round(column.null_fraction * 100)}% null`);
  if (column.distinct_count !== undefined) parts.push(`${column.distinct_count} distinct`);
  return parts.join(" · ");
}
