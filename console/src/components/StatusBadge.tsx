import type { ClaimStatus, ReviewState } from "@/api/types";

/**
 * The colour vocabulary from in one place.
 *
 * Every badge pairs colour with a word. Colour alone would encode review state
 * for sighted users only, and this is the interface's primary signal.
 */
const TONE = {
  generated: "bg-violet-soft text-violet-strong border-violet/30",
  validated: "bg-teal-soft text-teal-strong border-teal/25",
  review: "bg-amber-soft text-amber-strong border-amber/30",
  failed: "bg-red-soft text-red border-red/25",
  neutral: "bg-raised text-ink-2 border-line",
} as const;

export type Tone = keyof typeof TONE;

export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: Tone;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-[2px] text-badge font-semibold tracking-[0.02em] ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}

const CLAIM_TONE: Record<ClaimStatus, { tone: Tone; label: string; title?: string }> = {
  verified: { tone: "validated", label: "Validated" },
  // Neutral, not teal. Auto-accepted claims were never read by a person, and
  // giving them the validated colour would make the catalogue overstate itself
  // at exactly the point a reader is deciding how much to trust it.
  auto_accepted: {
    tone: "neutral",
    label: "Accepted",
    title:
      "Accepted without human review: routine column, grounded, high confidence. Not verified.",
  },
  unverified: { tone: "generated", label: "AI generated" },
  rejected: { tone: "failed", label: "Rejected" },
};

export function ClaimBadge({ status }: { status: ClaimStatus }) {
  const { tone, label, title } = CLAIM_TONE[status];
  return (
    <Badge tone={tone} title={title}>
      {label}
    </Badge>
  );
}

const REVIEW_TONE: Record<ReviewState, { tone: Tone; label: string }> = {
  "not-generated": { tone: "neutral", label: "Not generated" },
  "needs-review": { tone: "review", label: "Needs review" },
  partial: { tone: "review", label: "Partially reviewed" },
  validated: { tone: "validated", label: "Validated" },
};

export function ReviewStateBadge({ state }: { state: ReviewState }) {
  const { tone, label } = REVIEW_TONE[state];
  return <Badge tone={tone}>{label}</Badge>;
}

/** A status dot for dense rows where a full badge would crowd the line. */
export function StateDot({ state }: { state: ReviewState }) {
  const colour = {
    "not-generated": "bg-ink-3/40",
    "needs-review": "bg-amber",
    partial: "bg-amber",
    validated: "bg-teal",
  }[state];
  return <span aria-hidden className={`size-[7px] shrink-0 rounded-full ${colour}`} />;
}
