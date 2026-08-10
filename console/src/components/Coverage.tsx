import type { SchemaOutput } from "@/api/types";
import { reviewState } from "@/lib/review";

/**
 * How far through the schema you are, at a glance.
 *
 * The single most common question in a review session is "how much is left",
 * and counting badges down a 23-row list to answer it is why the workspace felt
 * shapeless. Segment widths are proportional, so the bar reads as an amount of
 * work rather than a decoration.
 */
export function Coverage({ output }: { output: SchemaOutput }) {
  const states = output.tables.map(reviewState);
  const counts = {
    validated: states.filter((s) => s === "validated").length,
    partial: states.filter((s) => s === "partial").length,
    needsReview: states.filter((s) => s === "needs-review").length,
    notGenerated: states.filter((s) => s === "not-generated").length,
  };
  const total = output.tables.length || 1;

  const segments = [
    { key: "validated", n: counts.validated, className: "bg-teal", label: "validated" },
    { key: "partial", n: counts.partial, className: "bg-amber", label: "partially reviewed" },
    { key: "needsReview", n: counts.needsReview, className: "bg-violet", label: "needs review" },
    { key: "notGenerated", n: counts.notGenerated, className: "bg-line", label: "not generated" },
  ].filter((s) => s.n > 0);

  return (
    <div className="flex min-w-[180px] flex-col gap-1">
      <div
        className="flex h-[6px] overflow-hidden rounded-full bg-raised"
        role="img"
        aria-label={segments.map((s) => `${s.n} ${s.label}`).join(", ")}
      >
        {segments.map((segment) => (
          <span
            key={segment.key}
            className={segment.className}
            style={{ width: `${(segment.n / total) * 100}%` }}
            title={`${segment.n} ${segment.label}`}
          />
        ))}
      </div>
      <p className="text-meta text-ink-3">
        {counts.validated} of {output.tables.length} validated
        {counts.notGenerated > 0 && ` · ${counts.notGenerated} not generated`}
      </p>
    </div>
  );
}
