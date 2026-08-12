import { useMemo, useState } from "react";

import type { ClaimStatus, SchemaOutput, TrustFactors } from "@/api/types";
import { describeError } from "@/api/errors";
import { SemanticViewPane } from "@/components/SemanticViewPane";
import { Button, Key } from "@/components/ui/Button";
import {
  buildQueue,
  countFor,
  isGap,
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
  // Nothing analysed is a different screen from nothing outstanding.
  const analysed = output.tables.filter((candidate) => candidate.analyzed).length;
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
            ) : analysed === 0 ? (
              "not analysed yet"
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
        ) : analysed === 0 ? (
          /* Not the same as an empty queue, and it must not be dressed as one.
             A green "nothing to review" over a schema nobody has looked at
             reads as finished work, when in fact no claim has been made about
             any of these tables. */
          <div className="m-6 rounded-[--radius-panel] border border-line bg-surface px-5 py-8 text-center">
            <p className="text-body text-ink">
              {output.table_count} table{output.table_count === 1 ? "" : "s"} captured. Nothing has
              been analysed yet, so there is no meaning to review.
            </p>
            <p className="mt-1.5 text-meta text-ink-3">
              Run <span className="font-semibold text-ink-2">Generate semantic view</span> to
              propose claims. Roughly a minute per table, and it reads your database.
            </p>
          </div>
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
          {/* Separate from "needs review" on purpose: these are fields the
              engine never wrote about. Grouping them with review work is what
              made an approved workspace still look unfinished. */}
          <Chip active={filter === "not-generated"} onClick={() => onFilter("not-generated")}>
            Not generated {countFor(output, "not-generated")}
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
        {/* Always listed, whatever the filter says. These tables exist — their
            columns, types and samples are already captured — and hiding them
            until analysis runs made a full extract look like a failed one. */}
        <Section
          label={`Not analysed · ${queue.notAnalysed.length}`}
          entries={shown(queue.notAnalysed)}
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
                <span
                  title={`${entry.highlighted} awaiting your decision`}
                  className="rounded-full bg-amber-soft px-1.5 text-badge font-semibold tabular-nums text-amber-strong"
                >
                  {entry.highlighted}
                </span>
              )}
              {/* Outlined, not filled: a gap is an absence, and giving it the
                  same solid chip as review work makes the two read alike.
                  Suppressed entirely on an unanalysed table, where every field
                  is a gap and the section heading already says so. */}
              {entry.gaps > 0 && entry.table.analyzed && (
                <span
                  title={`${entry.gaps} fields with no generated claim`}
                  className="rounded-full border border-dashed border-line-strong px-1.5 text-badge font-semibold tabular-nums text-ink-3"
                >
                  {entry.gaps}
                </span>
              )}
              <span className="shrink-0 text-meta tabular-nums text-ink-4">
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
  const open = rows.filter(needsReview).length;
  const gaps = rows.filter(isGap).length;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      {/* The count line is the whole summary. The old header spent three lines
          of prose explaining what the colours meant, which is what a legend is
          for — and needing one at all was the signal that the marks were not
          carrying their own meaning. */}
      <header className="sticky top-0 z-20 flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-line bg-canvas/95 px-6 py-3 backdrop-blur">
        <h2 className="ident text-table text-ink">{table.qualified_name}</h2>
        <span className="text-meta tabular-nums text-ink-3">
          {table.row_count.toLocaleString()} rows · {rows.length} fields
        </span>
        <span className="ml-auto flex items-center gap-3">
          <Tally n={open} tone="review" label={open === 1 ? "to decide" : "to decide"} />
          <Tally n={gaps} tone="gap" label="not generated" />
        </span>
      </header>

      <div className="overflow-x-auto">
        <div className="min-w-[880px]">
          <div className={`${SHEET_GRID} sticky top-[49px] z-10 border-b border-line bg-surface py-2 pl-4 pr-6 text-meta font-semibold uppercase tracking-wide text-ink-4`}>
            <span>Field</span>
            <span>Suggested meaning</span>
            <span>Samples</span>
            <span>State</span>
            <span className="text-right">Action</span>
          </div>
          {rows.map((row) => (
            <ReviewRow key={row.id} row={row} workspace={workspace} />
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * Five columns, down from six.
 *
 * Trust and Review were separate cells saying one thing between them, and the
 * six-column minimum forced the sheet to 1060px — which, beside a 268px ledger,
 * scrolled sideways on any ordinary laptop. A review table you have to drag
 * horizontally is not a table you read.
 */
const SHEET_GRID =
  "grid grid-cols-[minmax(140px,170px)_minmax(220px,1fr)_minmax(150px,0.62fr)_128px_112px] items-start gap-x-4";

/** A count that disappears at zero, so "nothing outstanding" looks like nothing. */
function Tally({ n, tone, label }: { n: number; tone: "review" | "gap"; label: string }) {
  if (n === 0) return null;
  return (
    <span className="flex items-baseline gap-1.5 text-meta">
      <span
        aria-hidden
        className={
          tone === "review"
            ? "inline-block h-2 w-[3px] rounded-full bg-amber"
            : "inline-block h-2 w-[3px] rounded-full bg-line-strong"
        }
      />
      <span className="font-semibold tabular-nums text-ink-2">{n}</span>
      <span className="text-ink-3">{label}</span>
    </span>
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

  // Risk is carried by a 3px edge marker, not by flooding the row. Washing the
  // whole row meant a table with twenty flagged fields rendered as a solid
  // amber panel: the colour stopped locating anything because it was
  // everywhere, and the sheet became unreadable at exactly the moment there
  // was most to read.
  const marker =
    row.risk === "red"
      ? "before:bg-red"
      : row.risk === "yellow"
        ? "before:bg-amber"
        : row.risk === "ungenerated"
          ? "before:bg-line-strong"
          : "before:bg-transparent";

  return (
    <article
      className={`group relative border-b border-line/70 bg-canvas last:border-b-0 hover:bg-surface/60 before:absolute before:inset-y-0 before:left-0 before:w-[3px] ${marker}`}
    >
      <div className={`${SHEET_GRID} py-2.5 pl-4 pr-6`}>
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

        {/* One cell, not two. Trust and Review were adjacent columns describing
            a single state, and the risk chip printed the internal enum — a
            reviewer read the literal word "yellow", which names a colour rather
            than a condition and is meaningless to anyone who cannot see it. */}
        <div className="min-w-0">
          <StateMark row={row} />
          {claim && (
            <p className="mt-1 text-meta tabular-nums text-ink-3">
              {trustPercent(claim.confidence)} trust
            </p>
          )}
          <p className="mt-0.5 text-meta leading-[15px] text-ink-4">{row.reason}</p>
        </div>

        {/* Three buttons on every row put up to 120 controls on one screen. The
            decision stays visible; the two that revise it appear when the row
            is hovered or focused. They stay in the DOM and in the tab order —
            focus-within is what keeps this usable from the keyboard. */}
        <div className="flex flex-col items-end gap-1">
          {editing ? (
            <>
              <Button
                size="sm"
                variant="primary"
                disabled={isLoading || !draft.trim()}
                onClick={() => void submit("verified", draft.trim())}
              >
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </>
          ) : claim ? (
            <>
              {needsReview(row) ? (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={isLoading || !canApprove}
                  title={canApprove ? undefined : "Nothing has tested this claim yet"}
                  onClick={() => void submit("verified")}
                >
                  Confirm
                </Button>
              ) : null}
              <span className="flex gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={isLoading}
                  onClick={() => void submit("rejected")}
                >
                  Reject
                </Button>
              </span>
            </>
          ) : (
            // Deliberately not a disabled button. There is no action to offer —
            // the gap closes by regenerating the table, not by deciding here.
            <span className="text-right text-meta leading-[15px] text-ink-4">
              Regenerate<br />to fill
            </span>
          )}
        </div>
      </div>

      {/* Only where there is something to disclose. Rendering it on every row
          put a triangle and an uppercase label beside forty fields, most of
          which had nothing behind them. */}
      {(row.findings.length > 0 || row.lineage.length > 0) && (
      <details className="border-t border-line/50 pb-1.5 pl-4 pr-6">
        <summary className="cursor-pointer py-1 text-meta text-ink-4 transition-colors hover:text-ink-2">
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
      )}
    </article>
  );
}

/**
 * What this row's state is, in words a reviewer can act on.
 *
 * A settled claim says so plainly rather than being decorated: the reward for
 * finishing a row should be that it goes quiet. Only the two states that want
 * something carry a filled chip.
 */
function StateMark({ row }: { row: ReviewLine }) {
  if (row.risk === "red") {
    return (
      <span className="inline-flex rounded-full bg-red-soft px-1.5 py-[1px] text-badge font-semibold text-red">
        Conflict
      </span>
    );
  }
  if (row.risk === "yellow") {
    return (
      <span className="inline-flex rounded-full bg-amber-soft px-1.5 py-[1px] text-badge font-semibold text-amber-strong">
        Needs you
      </span>
    );
  }
  // Dashed and unfilled — the shape says "nothing is here", which is the
  // literal truth and the thing that was indistinguishable from review work.
  if (row.risk === "ungenerated") {
    return (
      <span className="inline-flex rounded-full border border-dashed border-line-strong px-1.5 py-[1px] text-badge font-semibold text-ink-3">
        Not generated
      </span>
    );
  }
  const settled = row.claim?.status;
  return (
    <span className="inline-flex items-center gap-1.5 text-badge font-semibold text-ink-3">
      <span aria-hidden className="inline-block size-[5px] rounded-full bg-teal" />
      {settled === "verified" ? "Confirmed" : settled === "auto_accepted" ? "Accepted" : "Settled"}
    </span>
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
