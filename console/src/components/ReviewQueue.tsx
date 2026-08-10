import { useEffect, useMemo, useState } from "react";

import type { SchemaOutput } from "@/api/types";
import { ClaimCard } from "@/components/ClaimCard";
import { SemanticViewPane } from "@/components/SemanticViewPane";
import { buildQueue, countFor, settledOf, type Filter, type TableProgress } from "@/lib/queue";
import { setSearch } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * The review screen.
 *
 * One decision on screen at a time, with its evidence directly underneath it.
 * The old three-pane layout put the claim in the middle and the evidence for
 * that same claim in a panel to the right, so reading one decision meant
 * moving between two columns; and the left rail listed tables, which is not
 * what a reviewer is being asked about.
 *
 * The rail is now a progress ledger — in progress above, done below — and
 * `J`/`K` move through the queue without touching it. Progress is the
 * navigation.
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
  const [position, setPosition] = useState(0);
  const [showSpec, setShowSpec] = useState(false);

  const queue = useMemo(() => buildQueue(output, filter), [output, filter]);
  const current = queue.items[Math.min(position, queue.items.length - 1)];

  // Approving removes an item, so the index has to be pulled back rather than
  // left pointing past the end.
  useEffect(() => {
    if (position >= queue.items.length && queue.items.length > 0) {
      setPosition(queue.items.length - 1);
    }
  }, [queue.items.length, position]);

  const move = (step: number) =>
    setPosition((at) => Math.min(Math.max(at + step, 0), Math.max(queue.items.length - 1, 0)));

  useQueueKeys({ move, onSpec: () => setShowSpec((v) => !v), active: Boolean(current) });

  const table = current?.table ?? queue.inProgress[0]?.table.name ?? null;
  const settled = useMemo(() => {
    const entry = output.tables.find((t) => t.name === table);
    return entry ? settledOf(entry) : [];
  }, [output.tables, table]);

  return (
    <main className="grid min-h-0 grid-cols-[268px_minmax(0,1fr)] overflow-hidden">
      <Ledger
        queue={queue}
        filter={filter}
        onFilter={setFilter}
        output={output}
        search={search}
        onSearch={(value) => dispatch(setSearch(value))}
        active={table}
      />

      <section className="flex min-h-0 min-w-0 flex-col overflow-hidden">
        <div className="flex shrink-0 items-center gap-3 border-b border-line px-6 py-3">
          <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">Queue</span>
          <span className="text-body text-ink-2">
            {queue.items.length === 0
              ? "nothing outstanding"
              : `claim ${Math.min(position + 1, queue.items.length)} of ${queue.items.length}`}
            {table && <span className="ident ml-2 text-ink">{table}</span>}
          </span>

          <span className="ml-auto flex items-center gap-3">
            <SpecSwitch on={showSpec} onToggle={() => setShowSpec((v) => !v)} />
            <span className="flex items-center gap-1.5 text-meta text-ink-3">
              move <Key>J</Key> <Key>K</Key>
            </span>
          </span>
        </div>

        {showSpec ? (
          <SemanticViewPane
            workspace={workspace}
            table={table}
            onReview={() => setShowSpec(false)}
            bordered={false}
          />
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
            {settled.map((item) => (
              <p key={item.id} className="mb-2 flex items-baseline gap-3 rounded-[--radius-control] bg-raised px-3 py-2">
                <span className="text-meta font-semibold uppercase tracking-wide text-teal-strong">
                  done
                </span>
                <span className="ident text-ink-2">{item.label}</span>
                <span className="min-w-0 flex-1 truncate text-body text-ink-2">
                  {item.claim.text}
                </span>
              </p>
            ))}

            {current ? (
              <ClaimCard
                key={current.id}
                item={current}
                workspace={workspace}
                onSettled={() => setPosition((at) => Math.min(at, queue.items.length - 2))}
              />
            ) : (
              <p className="rounded-[--radius-panel] border border-teal/25 bg-teal-soft px-4 py-8 text-center text-body text-teal-strong">
                Everything consequential has been judged. Switch the filter to
                <span className="font-semibold"> All</span> to see the rest.
              </p>
            )}

            <NextUp queue={queue} position={position} onJump={setPosition} />
          </div>
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
}: {
  queue: ReturnType<typeof buildQueue>;
  filter: Filter;
  onFilter: (filter: Filter) => void;
  output: SchemaOutput;
  search: string;
  onSearch: (value: string) => void;
  active: string | null;
}) {
  const needle = search.trim().toLowerCase();
  const shown = (entries: TableProgress[]) =>
    needle ? entries.filter((e) => e.table.name.toLowerCase().includes(needle)) : entries;

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
          <Chip active={filter === "low-confidence"} onClick={() => onFilter("low-confidence")}>
            Low conf {countFor(output, "low-confidence")}
          </Chip>
          <Chip active={filter === "all"} onClick={() => onFilter("all")}>
            All
          </Chip>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-2">
        <Section label="In progress" entries={shown(queue.inProgress)} active={active} />
        <Section
          label={`Done · ${queue.done.length}`}
          entries={shown(queue.done)}
          active={active}
        />
      </div>
    </nav>
  );
}

function Section({
  label,
  entries,
  active,
}: {
  label: string;
  entries: TableProgress[];
  active: string | null;
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
            <div
              aria-current={entry.table.name === active ? "true" : undefined}
              className={`flex items-center gap-2 border-l-2 px-3 py-1.5 ${
                entry.table.name === active
                  ? "border-l-line-ink bg-raised"
                  : "border-l-transparent"
              }`}
            >
              <span className="ident min-w-0 flex-1 truncate text-ink">{entry.table.name}</span>
              <Bar settled={entry.settled} total={entry.total} />
              <span className="shrink-0 text-meta tabular-nums text-ink-3">
                {entry.settled}/{entry.total}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

/** Progress as a bar, because a ratio has to be read and a length does not. */
function Bar({ settled, total }: { settled: number; total: number }) {
  const share = total === 0 ? 0 : settled / total;
  return (
    <span className="h-[3px] w-8 shrink-0 overflow-hidden rounded-full bg-sunken">
      <span
        className={`block h-full rounded-full ${share === 1 ? "bg-teal" : "bg-amber"}`}
        style={{ width: `${Math.max(share * 100, share > 0 ? 12 : 0)}%` }}
      />
    </span>
  );
}

function NextUp({
  queue,
  position,
  onJump,
}: {
  queue: ReturnType<typeof buildQueue>;
  position: number;
  onJump: (index: number) => void;
}) {
  const upcoming = queue.items.slice(position + 1, position + 4);
  if (upcoming.length === 0) return null;

  return (
    <section className="mt-5">
      <p className="mb-2 text-meta font-semibold uppercase tracking-wide text-ink-3">Next up</p>
      <ul className="flex flex-col gap-1">
        {upcoming.map((item, offset) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onJump(position + 1 + offset)}
              className="flex w-full items-baseline gap-3 rounded-[--radius-control] border border-line bg-surface px-3 py-2 text-left hover:border-line-strong"
            >
              <span className="ident shrink-0 text-ink">{item.label}</span>
              <span className="min-w-0 flex-1 truncate text-body text-ink-2">
                {item.claim.text}
              </span>
              <Confidence value={item.claim.confidence} />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Confidence({ value }: { value: number }) {
  const low = value < 0.7;
  return (
    <span
      className={`shrink-0 rounded-full px-1.5 text-badge font-semibold tabular-nums ${
        low ? "bg-amber-soft text-amber-strong" : "bg-teal-soft text-teal-strong"
      }`}
    >
      {value.toFixed(2)}
    </span>
  );
}

function SpecSwitch({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onToggle}
      title="Show the emitted semantic view for this table (S)"
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
      <span className={on ? "text-ink" : undefined}>YAML</span>
      <Key>S</Key>
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

export function Key({ children }: { children: React.ReactNode }) {
  return (
    <kbd className="rounded border border-line bg-raised px-1 font-mono text-badge text-ink-2">
      {children}
    </kbd>
  );
}

/**
 * Movement and view keys only.
 *
 * Approve, edit and reject live on the card, next to the state they act on —
 * whether an edit is in progress decides whether `E` means "start editing" or
 * is a character being typed.
 */
function useQueueKeys({
  move,
  onSpec,
  active,
}: {
  move: (step: number) => void;
  onSpec: () => void;
  active: boolean;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) {
        if (event.key === "Escape") target.blur();
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.key === "/") {
        event.preventDefault();
        document.getElementById("table-search")?.focus();
        return;
      }
      if (event.key.toLowerCase() === "s") {
        event.preventDefault();
        onSpec();
        return;
      }
      if (!active) return;
      if (event.key === "j" || event.key === "k") {
        event.preventDefault();
        move(event.key === "j" ? 1 : -1);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [move, onSpec, active]);
}
