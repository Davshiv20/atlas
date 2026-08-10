import { useMemo, useState } from "react";

import type { ClaimStatus, SchemaOutput, TrustFactors } from "@/api/types";
import { describeError } from "@/api/errors";
import { SemanticViewPane } from "@/components/SemanticViewPane";
import { Key } from "@/components/ui/Button";
import {
  buildQueue,
  countFor,
  needsReview,
  rowsFor,
  type Filter,
  type ReviewRow as ReviewLine,
  type TableProgress,
} from "@/lib/queue";
import { canVerify, confidenceLabel, trustPercent } from "@/lib/review";
import { setSearch } from "@/store/uiSlice";
import { useReviewMutation } from "@/store/api";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * Table-sheet review.
 *
 * YAML remains the generated output. Review is simpler: pick a table, scan all
 * fields in one sheet, and touch only the highlighted rows. A routine column
 * can stay inferred without becoming a task for a human.
 */
export function ReviewQueue({
  output,
  workspace,
}: {
  output: SchemaOutput;
  workspace: string;
}) {
  const dispatch = useAppDispatch();
  const search = useAppSelector((s) => s.ui.search);
  const [filter, setFilter] = useState<Filter>("needs-review");
  const [selectedTable, setSelectedTable] = useState<string | null>(null);
  const [showYaml, setShowYaml] = useState(false);

  const queue = useMemo(() => buildQueue(output, filter), [output, filter]);
  const tableName = selectedTable ?? queue.tables[0]?.table.name ?? null;
  const table = output.tables.find((candidate) => candidate.name === tableName) ?? null;

  return (
    <main className="grid min-h-0 grid-cols-[268px_minmax(0,1fr)] overflow-hidden">
      <Ledger
        queue={queue}
        filter={filter}
        onFilter={setFilter}
        output={output}
        search={search}
        onSearch={(value) => dispatch(setSearch(value))}
        active={tableName}
        onSelect={setSelectedTable}
      />

      <section className="flex min-h-0 min-w-0 flex-col overflow-hidden">
        <div className="flex shrink-0 items-center gap-3 border-b border-line px-6 py-3">
          <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">
            Table review
          </span>
          <span className="text-body text-ink-2">
            {table ? (
              <>
                Scan the sheet. Touch only highlighted rows.
                <span className="ident ml-2 text-ink">{table.name}</span>
              </>
            ) : (
              "nothing to review"
            )}
          </span>

          <span className="ml-auto flex items-center gap-3">
            <SpecSwitch on={showYaml} onToggle={() => setShowYaml((value) => !value)} />
          </span>
        </div>

        {showYaml ? (
          <SemanticViewPane
            workspace={workspace}
            table={table?.name ?? null}
            onReview={() => setShowYaml(false)}
            bordered={false}
          />
        ) : table ? (
          <ReviewSheet table={table} workspace={workspace} />
        ) : (
          <p className="m-6 rounded-[--radius-panel] border border-teal/25 bg-teal-soft px-4 py-8 text-center text-body text-teal-strong">
            No highlighted rows. Switch to <span className="font-semibold">All</span> to audit every field.
          </p>
        )}
      </section>
    </main>
  );
}

function Ledger({
  queue,
  filter,
  onFilter,
  output,
  search,
  onSearch,
  active,
  onSelect,
}: {
  queue: ReturnType<typeof buildQueue>;
  filter: Filter;
  onFilter: (filter: Filter) => void;
  output: SchemaOutput;
  search: string;
  onSearch: (value: string) => void;
  active: string | null;
  onSelect: (table: string) => void;
}) {
  const needle = search.trim().toLowerCase();
  const shown = (entries: TableProgress[]) =>
    needle ? entries.filter((entry) => entry.table.name.toLowerCase().includes(needle)) : entries;

  return (
    <nav className="flex h-full min-h-0 flex-col border-r border-line bg-surface">
      <div className="shrink-0 border-b border-line p-3">
        <div className="relative">
          <input
            id="table-search"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder={`Filter ${output.tables.length} tables`}
            aria-label="Filter tables"
            className="w-full rounded-[--radius-control] border border-line bg-raised py-1.5 pl-2.5 pr-8 text-body text-ink placeholder:text-ink-3 focus:border-line-ink focus:bg-surface focus:outline-none"
          />
          <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">
            <Key>/</Key>
          </span>
        </div>

        <div className="mt-2 flex flex-wrap gap-1.5">
          <Chip active={filter === "needs-review"} onClick={() => onFilter("needs-review")}>
            Needs review {countFor(output, "needs-review")}
          </Chip>
          <Chip active={filter === "weak-trust"} onClick={() => onFilter("weak-trust")}>
            Weak trust {countFor(output, "weak-trust")}
          </Chip>
          <Chip active={filter === "all"} onClick={() => onFilter("all")}>
            All
          </Chip>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-2">
        <Section
          label="Highlighted"
          entries={shown(queue.needsReview)}
          active={active}
          onSelect={onSelect}
        />
        <Section
          label={`Quiet · ${queue.quiet.length}`}
          entries={shown(queue.quiet)}
          active={active}
          onSelect={onSelect}
        />
      </div>
    </nav>
  );
}

function Section({
  label,
  entries,
  active,
  onSelect,
}: {
  label: string;
  entries: TableProgress[];
  active: string | null;
  onSelect: (table: string) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <>
      <p className="px-3 pb-1 pt-3 text-meta font-semibold uppercase tracking-wide text-ink-3">
        {label}
      </p>
      <ul>
        {entries.map((entry) => (
          <li key={entry.table.name}>
            <button
              type="button"
              onClick={() => onSelect(entry.table.name)}
              aria-current={entry.table.name === active ? "true" : undefined}
              className={`flex w-full items-center gap-2 border-l-2 px-3 py-1.5 text-left ${
                entry.table.name === active
                  ? "border-l-line-ink bg-raised"
                  : "border-l-transparent hover:bg-raised/70"
              }`}
            >
              <span className="ident min-w-0 flex-1 truncate text-ink">{entry.table.name}</span>
              {entry.highlighted > 0 && (
                <span className="rounded-full bg-amber-soft px-1.5 text-badge font-semibold text-amber-strong">
                  {entry.highlighted}
                </span>
              )}
              <span className="shrink-0 text-meta tabular-nums text-ink-3">
                {entry.totalRows}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}

function ReviewSheet({
  table,
  workspace,
}: {
  table: SchemaOutput["tables"][number];
  workspace: string;
}) {
  const rows = rowsFor(table, "all");
  const highlighted = rows.filter(needsReview).length;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
      <header className="mb-3 rounded-[--radius-panel] border border-line bg-surface px-4 py-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 className="ident text-table text-ink">{table.qualified_name}</h2>
          <span className="text-body text-ink-3">
            {table.row_count.toLocaleString()} rows · {highlighted} highlighted
          </span>
        </div>
        <p className="mt-1 text-body text-ink-3">
          Yellow means “worth a human look.” Red means conflicting or very weak support on an important claim.
          Routine rows can stay as-is.
        </p>
      </header>

      <div className="overflow-x-auto rounded-[--radius-panel] border border-line bg-surface">
        <div className="grid min-w-[1060px] grid-cols-[150px_minmax(190px,1fr)_minmax(170px,.8fr)_70px_115px_125px] gap-3 border-b border-line bg-raised px-3 py-2 text-meta font-semibold uppercase tracking-wide text-ink-3">
          <span>Field</span>
          <span>Suggested meaning</span>
          <span>Sample values</span>
          <span>Trust</span>
          <span>Review</span>
          <span>Action</span>
        </div>
        {rows.map((row) => (
          <ReviewRow key={row.id} row={row} workspace={workspace} />
        ))}
      </div>
    </div>
  );
}

function ReviewRow({ row, workspace }: { row: ReviewLine; workspace: string }) {
  const reviewer = useAppSelector((state) => state.ui.reviewer);
  const [review, { isLoading, error }] = useReviewMutation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(row.claim?.text ?? row.proposed);
  const claim = row.claim;
  const trust = claim ? confidenceLabel(claim) : null;
  const canApprove = claim ? canVerify(claim) : false;

  const submit = async (decision: ClaimStatus, text?: string) => {
    if (!claim) return;
    await review({
      workspace,
      claimId: row.id,
      body: { decision, reviewer, ...(text !== undefined ? { claim: text } : {}) },
    }).unwrap();
    setEditing(false);
  };

  const rowTone = row.risk === "red"
    ? "bg-red-soft/70"
    : row.risk === "yellow"
      ? "bg-amber-soft/70"
      : "bg-surface";

  return (
    <article className={`border-b border-line last:border-b-0 ${rowTone}`}>
      <div className="grid min-w-[1060px] grid-cols-[150px_minmax(190px,1fr)_minmax(170px,.8fr)_70px_115px_125px] gap-3 px-3 py-2">
        <div className="min-w-0">
          <p className="ident truncate text-ink">{row.role}</p>
          <p className="truncate text-meta text-ink-3">{row.source}</p>
        </div>

        <div className="min-w-0">
          {editing ? (
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={3}
              autoFocus
              className="w-full resize-y rounded-[--radius-control] border border-line bg-canvas px-2 py-1 text-body text-ink focus:border-line-ink focus:outline-none"
            />
          ) : (
            <p className="text-body text-ink">{row.proposed}</p>
          )}
          {row.findings[0] ? (
            <Finding finding={row.findings[0]} />
          ) : trust ? (
            <p className="mt-1 text-meta text-ink-3">{trust.detail}</p>
          ) : null}
          {error && (
            <p className="mt-1 text-meta text-red">
              {describeError(error, "Could not save review.")}
            </p>
          )}
        </div>

        <div className="min-w-0">
          {row.samples.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {row.samples.map((sample) => (
                <span
                  key={sample}
                  className="max-w-full truncate rounded-[--radius-control] bg-raised px-1.5 py-[1px] text-meta text-ink-2"
                  title={sample}
                >
                  {sample}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-meta text-amber-strong">No samples</p>
          )}
          {row.sampleNote && <p className="mt-1 text-meta text-ink-3">{row.sampleNote}</p>}
        </div>

        <div>
          {claim ? (
            <span className="rounded-full bg-raised px-1.5 text-badge font-semibold tabular-nums text-ink-2">
              {trustPercent(claim.confidence)}
            </span>
          ) : (
            <span className="text-meta text-ink-3">—</span>
          )}
        </div>

        <div>
          <span
            className={`rounded-full px-1.5 text-badge font-semibold ${
              row.risk === "red"
                ? "bg-red-soft text-red"
                : row.risk === "yellow"
                  ? "bg-amber-soft text-amber-strong"
                  : "bg-teal-soft text-teal-strong"
            }`}
          >
            {row.risk === "none" ? "quiet" : row.risk}
          </span>
          <p className="mt-1 text-meta text-ink-3">{row.reason}</p>
        </div>

        <div className="flex flex-wrap items-start gap-1.5">
          {editing ? (
            <>
              <button
                type="button"
                disabled={isLoading || !draft.trim()}
                onClick={() => void submit("verified", draft.trim())}
                className={ACTION_PRIMARY}
              >
                Save
              </button>
              <button type="button" onClick={() => setEditing(false)} className={ACTION_SECONDARY}>
                Cancel
              </button>
            </>
          ) : claim ? (
            <>
              <button
                type="button"
                disabled={isLoading || !canApprove}
                title={canApprove ? undefined : "Ground it before marking verified"}
                onClick={() => void submit("verified")}
                className={ACTION_PRIMARY}
              >
                Confirm
              </button>
              <button type="button" onClick={() => setEditing(true)} className={ACTION_SECONDARY}>
                Edit
              </button>
              <button
                type="button"
                disabled={isLoading}
                onClick={() => void submit("rejected")}
                className="rounded-[--radius-control] px-2 py-1 text-meta text-red hover:bg-red-soft"
              >
                Reject
              </button>
            </>
          ) : (
            <span className="text-meta text-ink-3">No claim</span>
          )}
        </div>
      </div>

      <details className="min-w-[1060px] border-t border-line/70 px-3 py-1.5">
        <summary className="cursor-pointer text-meta font-semibold uppercase tracking-wide text-ink-3">
          Why Atlas thinks this
        </summary>
        {row.findings.length > 0 && (
          <div className="mt-2 grid gap-2 pb-1">
            {row.findings.map((finding) => (
              <Finding key={finding.evidence_id} finding={finding} expanded />
            ))}
          </div>
        )}
        <ol className="mt-2 grid gap-1 pb-1 text-meta text-ink-2">
          {row.lineage.map((line) => (
            <li key={line} className="ident rounded-[--radius-control] bg-raised px-2 py-1">
              {line}
            </li>
          ))}
        </ol>
      </details>
    </article>
  );
}

function Finding({
  finding,
  expanded = false,
}: {
  finding: ReviewLine["findings"][number];
  expanded?: boolean;
}) {
  const bad = finding.relationship === "contradicts" || finding.verdict === "failed";
  return (
    <div
      className={`mt-1 rounded-[--radius-control] border px-2 py-1.5 text-meta ${
        bad ? "border-red/20 bg-red-soft/60 text-red" : "border-teal/20 bg-teal-soft/60 text-teal-strong"
      }`}
    >
      <p className="font-semibold">{finding.title}</p>
      <p>{finding.result}</p>
      {expanded && finding.details.length > 0 && (
        <ul className="mt-1 list-disc pl-4 text-ink-2">
          {finding.details.map((detail) => (
            <li key={detail}>{detail}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SpecSwitch({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onToggle}
      title="Show generated semantic_view.yaml"
      className="flex items-center gap-2 text-meta text-ink-3 hover:text-ink"
    >
      <span
        className={`relative h-[18px] w-[32px] rounded-full border transition-colors ${
          on ? "border-line-ink bg-cta" : "border-line bg-raised"
        }`}
      >
        <span
          className={`absolute top-[2px] size-[12px] rounded-full transition-all ${
            on ? "left-[16px] bg-cta-ink" : "left-[2px] bg-ink-4"
          }`}
        />
      </span>
      <span className={on ? "text-ink" : undefined}>YAML output</span>
    </button>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full border px-2.5 py-[3px] text-meta transition-colors ${
        active
          ? "border-line-ink bg-cta text-cta-ink"
          : "border-line bg-surface text-ink-2 hover:border-line-strong"
      }`}
    >
      {children}
    </button>
  );
}


const ACTION_PRIMARY =
  "rounded-[--radius-control] bg-cta px-2 py-1 text-meta font-medium text-cta-ink hover:bg-cta-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-4";

const ACTION_SECONDARY =
  "rounded-[--radius-control] border border-line bg-surface px-2 py-1 text-meta text-ink hover:bg-raised";
