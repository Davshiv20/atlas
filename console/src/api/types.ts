/**
 * Types mirroring the engine's OpenAPI schema (`src/api/openapi.json`).
 *
 * Hand-written for now and kept deliberately narrow: every field here exists on
 * the engine's pydantic models. Nothing is optional unless the engine can
 * actually omit it — a field typed as required that arrives undefined is a
 * runtime crash, and a field typed optional that is always present pushes
 * pointless null checks through the UI.
 *
 * Regenerate the schema with `make types` from the repo root when the engine
 * changes, and reconcile this file against it.
 */

/** `auto_accepted` is not `verified`: nobody read it. Never render them alike. */
export type ClaimStatus = "unverified" | "auto_accepted" | "verified" | "rejected";

/** What breaks downstream if the claim is wrong. Drives review order. */
export type Consequence = "critical" | "high" | "routine";

/** What kind of evidence established a claim. Separate from review status. */
export type TrustState =
  | "unsupported"
  | "signal"
  | "observed"
  | "verified"
  | "enforced"
  | "authoritative"
  | "contradicted";

export type TrustBand =
  | "unsupported"
  | "weak_signals"
  | "plausible"
  | "strongly_supported"
  | "highly_trusted"
  | "authoritative_or_enforced"
  | "conflicted";

export interface TrustFactors {
  evidence_directness: number;
  authority: number;
  coverage: number;
  consistency: number;
  freshness: number;
}

/** Inspectable composition of confidence. Confidence is a trust score, not probability. */
export interface TrustAssessment {
  state: TrustState;
  confidence: number;
  factors: TrustFactors;
  reasons: string[];
  limitations: string[];
  band: TrustBand;
}

export interface EvidenceFinding {
  relationship: string;
  verdict: string;
  title: string;
  result: string;
  details: string[];
  evidence_id: string;
  query_hash?: string;
}

/** A single assertion, as it appears embedded in the output document. */
export interface Claim {
  text: string;
  /** Evidence-derived trust score from 0..1, not model certainty. */
  confidence: number;
  /** Missing only on legacy claims written before factor breakdowns existed. */
  trust?: TrustAssessment;
  status: ClaimStatus;
  /** True when an executed check could have falsified the claim and did not. */
  grounded: boolean;
  evidence?: string;
  findings: EvidenceFinding[];
  consequence: Consequence;
}

export interface SampleValue {
  value: string;
  count: number;
}

export interface ColumnOutput {
  name: string;
  column_class: string;
  consequence: Consequence;
  data_type: string;
  nullable: boolean;
  is_primary_key: boolean;
  null_fraction?: number;
  distinct_count?: number;
  min_value?: string;
  max_value?: string;
  sampled: boolean;
  sample_values?: SampleValue[];
  /** Present instead of sample_values when the privacy policy withheld them. */
  values_withheld_reason?: string;
  description?: Claim;
  notes: Claim[];
}

export interface JoinOutput {
  columns: string[];
  referred_table?: string;
  referred_columns: string[];
  /** True only for a declared constraint — the database itself guarantees it. */
  enforced: boolean;
  description?: Claim;
}

/** Validation counted over consequential claims only — see engine output.py. */
export interface ValidationSummary {
  critical_total: number;
  critical_settled: number;
  high_total: number;
  high_settled: number;
  routine_total: number;
  routine_auto_accepted: number;
}


/**
 * A hypothesis that was tested and did not hold.
 *
 * The absence of a claim is ambiguous — nobody looked, or someone looked and
 * the answer was no. This is the second case, stated.
 */
export interface RuledOut {
  hypothesis: string;
  finding: string;
  scope: string;
  evidence_id: string;
}

export interface TableOutput {
  name: string;
  qualified_name: string;
  row_count: number;
  row_count_is_exact: boolean;
  primary_key: string[];
  source_comment?: string;
  grain?: Claim;
  description?: Claim;
  joins: JoinOutput[];
  notes: Claim[];
  columns: ColumnOutput[];
  open_questions: string[];
  ruled_out: RuledOut[];
  analyzed: boolean;
  validation: ValidationSummary;
}

export interface SchemaOutput {
  database: string;
  schema_name: string;
  captured_at: string;
  table_count: number;
  claim_count: number;
  checked_claim_count: number;
  question_count: number;
  tables: TableOutput[];
}

export type ProvenanceKind = "grounded_check" | "llm_inference" | "human";

export interface Provenance {
  kind: ProvenanceKind;
  detail: string;
  result?: "pass" | "fail" | "inconclusive";
}

/** The reviewable record. `id` is `subject#aspect`, assigned by the engine. */
export interface Fact {
  id: string;
  subject: string;
  aspect: string;
  claim: string;
  /** Evidence-derived trust score from 0..1, not model certainty. */
  confidence: number;
  trust?: TrustAssessment;
  provenance: Provenance[];
  status: ClaimStatus;
  consequence: Consequence;
  applies_to_class?: string;
  verified_by?: string;
  supersedes?: string;
}

export type QuestionStatus = "open" | "answered" | "dismissed";

/**
 * Something no query can settle.
 *
 * Answering one is the only thing that lifts a business claim past the
 * OBSERVED ceiling — data establishes what a column contains, never what it
 * means to the organisation.
 */
export interface Question {
  id: string;
  subject: string;
  question: string;
  evidence: string;
  table: string;
  /** What the answer would establish about the subject. */
  aspect: string;
  status: QuestionStatus;
  answer?: string;
  answered_by?: string;
  answered_at?: string;
}

/** `interrupted` means the engine restarted mid-run: distinct from `failed`,
 *  where the work itself raised and the request was at fault. */
export type JobStatus = "pending" | "running" | "succeeded" | "failed" | "interrupted";

/**
 * What a running job is doing. Analysis costs minutes per table, so a spinner
 * alone tells the reviewer nothing — `current` and `completed` say how far in
 * the run is and which table is being read right now.
 */
export interface JobProgress {
  message: string;
  tables: string[];
  completed: string[];
  /** Plural: tables are read concurrently, so several are in flight at once. */
  current: string[];
}

/** What an analyze or extract job produced. */
export interface AnalyzeResult {
  claims?: number;
  questions?: number;
  evidence?: number;
  tables?: string[];
  skipped?: string[];
  /** Tables the model was cut off on: their reading is partial. */
  partial?: string[];
  discarded?: Record<string, number>;
  output?: string;
}

export interface Job {
  id: string;
  kind: string;
  workspace: string;
  status: JobStatus;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  progress?: JobProgress;
  result?: AnalyzeResult;
  error?: string;
}

export type SnowflakeAuthMethod =
  | "password"
  | "mfa_push"
  | "mfa_totp"
  | "external_browser";

export interface SnowflakeCredentials {
  account_identifier: string;
  username: string;
  auth_method: SnowflakeAuthMethod;
  password?: string;
  passcode?: string;
  warehouse: string;
  role: string;
}

/** A declared source. Deliberately never carries the connection string. */
export interface SourceStatus {
  id: string;
  adapter: string;
  /** The *name* of the env var holding the URL, not the URL. */
  url_env: string;
  namespace: string;
  label?: string;
  /** Whether the engine's environment holds a value for url_env. Not "connected". */
  configured: boolean;
  /** True when Atlas holds the credential, rather than reading an exported one. */
  managed: boolean;
  health: ConnectionHealth;
}

/**
 * The result of actually connecting.
 *
 * `configured` on SourceStatus means only that the env var holds something —
 * a wrong password leaves it true. `health.state` is the checked answer.
 */
export interface ConnectionHealth {
  state: "unknown" | "connected" | "failed";
  checked_at?: string;
  detail?: string;
  server_version?: string;
  table_count?: number;
}

export interface EngineConfig {
  model: string;
  effort: string;
  base_url: string;
  api_key_configured: boolean;
  database_url_configured: boolean;
  max_turns: number;
  max_rows: number;
  statement_timeout_ms: number;
}

export interface ReviewRequest {
  decision: ClaimStatus;
  reviewer: string;
  /** Corrected wording. Omit to approve or reject the text unchanged. */
  claim?: string;
}

/** Review state of a table, derived from its claims. */
export type ReviewState = "not-generated" | "needs-review" | "partial" | "validated";

/**
 * The emitted view — what an agent is handed, as opposed to the catalogue,
 * which keeps every claim with its evidence and review state.
 */
export interface Dimension {
  name: string;
  expr: string;
  data_type: string;
  description?: string;
  unique: boolean;
  nullable: boolean;
  reviewed: boolean;
}

export interface ViewRelationship {
  to: string;
  left: string;
  right: string;
  enforced: boolean;
}

/** A column deliberately left out, and why — silence would read as absence. */
export interface Excluded {
  name: string;
  reason: string;
}

export interface TableView {
  name: string;
  base_table: string;
  row_count: number;
  grain?: string;
  description?: string;
  reviewed_by?: string;
  dimensions: Dimension[];
  relationships: ViewRelationship[];
  excluded: Excluded[];
  /** Consequential claims nobody has settled; non-zero blocks a clean emit. */
  pending: number;
}

export interface SemanticView {
  database: string;
  schema_name: string;
  tables: TableView[];
}

export interface SemanticViewResponse {
  view: SemanticView;
  yaml: string;
  ready: number;
  tables: number;
}
