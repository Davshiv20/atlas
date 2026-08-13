import { useMemo } from "react";

import type { SchemaOutput } from "@/api/types";
import { layout, type Edge, type Node } from "@/lib/layout";
import { reviewCounts, reviewState } from "@/lib/review";
import { SemanticViewPane } from "@/components/SemanticViewPane";
import { selectTable, setMapPaneOpen, setView } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

/**
 * The schema as a picture.
 *
 * The table list answers "what exists" and nothing else — it cannot show that
 * `users` is what half the schema points at, or that the untouched tables sit
 * together at one edge. Structure is the thing a developer meeting an
 * unfamiliar database needs first, and a flat list of twenty-three names is
 * the one shape that hides it.
 *
 * Shading is by review state so the map doubles as progress: where the work is
 * left is a property of position, not something to be read off a column of
 * ratios.
 */
export function SchemaMap({ output, workspace }: { output: SchemaOutput; workspace: string }) {
  const dispatch = useAppDispatch();
  const selected = useAppSelector((s) => s.ui.table);
  const paneOpen = useAppSelector((s) => s.ui.mapPaneOpen);
  const graph = useMemo(() => layout(output), [output]);

  const ready = output.tables.filter((t) => reviewState(t) === "validated").length;

  return (
    // The panel takes a column rather than floating over one. A schema map is
    // read by following lines between tables, and an overlay hides exactly the
    // part you are tracing — so closing it gives the canvas back instead of
    // moving the obstruction somewhere else.
    <div
      className={`grid min-h-0 overflow-hidden ${
        paneOpen ? "grid-cols-[minmax(0,1fr)_460px]" : "grid-cols-[minmax(0,1fr)]"
      }`}
    >
      {/* Three layers on purpose. The cards are positioned against the canvas
          so they scroll with the edges they connect to — pinned to the scroll
          container instead, they would sit still while the lines moved. The
          legend stays with the viewport, which is the one thing that should
          not scroll away. */}
      <div className="relative min-h-0 min-w-0 overflow-hidden">
        <div className="h-full w-full overflow-auto bg-canvas [background-image:radial-gradient(var(--color-line)_1px,transparent_1px)] [background-size:24px_24px]">
          <div className="relative" style={{ width: graph.width, height: graph.height }}>
            <svg
              width={graph.width}
              height={graph.height}
              className="absolute inset-0 block"
              role="img"
              aria-label="Schema relationship map"
            >
              {graph.edges.map((edge) => (
                <Connector
                  key={`${edge.from}-${edge.via}-${edge.to}`}
                  edge={edge}
                  selected={selected}
                />
              ))}
            </svg>

            {graph.nodes.map((node) => (
              <TableCard
                key={node.table.name}
                node={node}
                selected={node.table.name === selected}
                onSelect={() => dispatch(selectTable(node.table.name))}
              />
            ))}
          </div>
        </div>

        <Legend ready={ready} total={output.tables.length} />

        {!paneOpen && (
          <ReopenPane
            table={selected}
            onOpen={() => dispatch(setMapPaneOpen(true))}
          />
        )}
      </div>

      {paneOpen && (
        <SemanticViewPane
          workspace={workspace}
          table={selected}
          onReview={() => dispatch(setView("workspace"))}
          onClose={() => dispatch(setMapPaneOpen(false))}
        />
      )}
    </div>
  );
}

/**
 * How the closed panel says it is still there.
 *
 * It carries the selected table's name, so selecting one while the panel is
 * shut is not a dead end: the map answers, quietly, that there is something to
 * open. Auto-opening instead would override a decision the reviewer just made
 * — they closed it to see the graph.
 *
 * Sits opposite the legend, on the edge it would emerge from.
 */
function ReopenPane({ table, onOpen }: { table: string | null; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="absolute bottom-4 right-4 z-10 inline-flex max-w-[280px] items-center gap-2 rounded-[--radius-panel] border border-line bg-surface px-3 py-2 text-left hover:border-line-strong"
    >
      <span className="ident shrink-0 text-ink-2">semantic_view.yaml</span>
      {table ? (
        <span className="ident min-w-0 flex-1 truncate text-ink-3">{table}</span>
      ) : (
        <span className="text-meta text-ink-3">closed</span>
      )}
    </button>
  );
}

const EDGE_STATE = {
  validated: "border-t-teal",
  partial: "border-t-amber",
  "needs-review": "border-t-amber",
  "not-generated": "border-t-line-strong",
} as const;

function TableCard({
  node,
  selected,
  onSelect,
}: {
  node: Node;
  selected: boolean;
  onSelect: () => void;
}) {
  const table = node.table;
  const state = reviewState(table);
  const { verified, total } = reviewCounts(table);

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      style={{ left: node.x, top: node.y, width: node.width, minHeight: node.height }}
      className={`absolute flex flex-col items-start gap-0.5 rounded-[--radius-panel] border border-t-[3px] bg-surface px-3 py-2 text-left transition-colors ${
        EDGE_STATE[state]
      } ${
        selected
          ? "border-x-line-ink border-b-line-ink ring-2 ring-focus"
          : "border-x-line border-b-line hover:border-x-line-strong hover:border-b-line-strong"
      }`}
    >
      <span className="flex w-full items-baseline gap-2">
        <span className="ident min-w-0 flex-1 truncate text-ink">{table.name}</span>
        {total > 0 && (
          <span
            className={`shrink-0 rounded-full px-1.5 text-badge font-semibold ${
              verified === total ? "bg-teal-soft text-teal-strong" : "bg-amber-soft text-amber-strong"
            }`}
          >
            {verified}/{total}
          </span>
        )}
      </span>

      <span className="text-meta text-ink-3">
        {table.columns.length} cols
        {table.analyzed ? ` · ${state === "validated" ? "complete" : `${verified} reviewed`}` : " · not generated"}
      </span>

      {selected && table.grain && (
        <span className="mt-0.5 line-clamp-2 text-meta text-ink-2">{table.grain.text}</span>
      )}
    </button>
  );
}

function Connector({ edge, selected }: { edge: Edge; selected: string | null }) {
  const touched = selected === edge.from || selected === edge.to;
  const path = edge.points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");

  return (
    <path
      d={path}
      fill="none"
      stroke={touched ? "var(--color-line-ink)" : "var(--color-line-strong)"}
      strokeWidth={touched ? 1.5 : 1}
      // Dashed where the database does not enforce it: a verified join and a
      // guaranteed one are not the same promise, and the map is the one place
      // that difference is visible at a glance.
      strokeDasharray={edge.enforced ? undefined : "4 3"}
    >
      <title>{`${edge.from}.${edge.via} → ${edge.to}`}</title>
    </path>
  );
}

function Legend({ ready, total }: { ready: number; total: number }) {
  return (
    <div className="absolute bottom-4 left-4 z-10 inline-flex items-center gap-3 rounded-[--radius-panel] border border-line bg-surface px-3 py-2">
      <Swatch className="bg-teal" label="validated" />
      <Swatch className="bg-amber" label="partial" />
      <Swatch className="bg-line-strong" label="untouched" />
      <span className="text-meta text-ink-3">
        {ready} of {total} tables ready
      </span>
    </div>
  );
}

function Swatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`h-[3px] w-4 rounded-full ${className}`} aria-hidden />
      <span className="text-meta text-ink-2">{label}</span>
    </span>
  );
}
