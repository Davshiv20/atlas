import { useMemo } from "react";

import type { SchemaOutput } from "@/api/types";
import { layout, type Edge, type Node } from "@/lib/layout";
import { reviewCounts, reviewState } from "@/lib/review";
import { JoinReview } from "@/components/JoinReview";
import { SemanticViewPane } from "@/components/SemanticViewPane";
import { selectEdge, selectTable, setMapPaneOpen, setView } from "@/store/uiSlice";
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
  const selectedEdge = useAppSelector((s) => s.ui.edge);
  const paneOpen = useAppSelector((s) => s.ui.mapPaneOpen);
  const graph = useMemo(() => layout(output), [output]);

  const edge = graph.edges.find((candidate) => candidate.id === selectedEdge);
  // Both ends of a selected relationship light up. The claim is about the pair,
  // so highlighting one of them would be a picture of half the question.
  const lit = edge ? [edge.from, edge.to] : selected ? [selected] : [];

  const ready = output.tables.filter((t) => reviewState(t) === "validated").length;

  return (
    // The panel takes a column rather than floating over one. A schema map is
    // read by following lines between tables, and an overlay hides exactly the
    // part you are tracing — so closing it gives the canvas back instead of
    // moving the obstruction somewhere else.
    <div className="flex min-h-0 overflow-hidden">
      {/* Three layers on purpose. The cards are positioned against the canvas
          so they scroll with the edges they connect to — pinned to the scroll
          container instead, they would sit still while the lines moved. The
          legend stays with the viewport, which is the one thing that should
          not scroll away. */}
      <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
        <div className="h-full w-full overflow-auto bg-canvas [background-image:radial-gradient(var(--color-line)_1px,transparent_1px)] [background-size:24px_24px]">
          <div className="relative" style={{ width: graph.width, height: graph.height }}>
            <svg
              width={graph.width}
              height={graph.height}
              className="absolute inset-0 block"
              role="img"
              aria-label="Schema relationship map"
            >
              {/* Unenforced edges are painted last, so they sit on top.
                  Two tables joined twice are routed down the same corridor and
                  their click targets overlap — aiming at the dashed line
                  selected the solid one running beside it. Where a click is
                  contested, the reviewable claim should win it: an enforced
                  key has nothing to decide. */}
              {[...graph.edges]
                .sort((a, b) => Number(b.enforced) - Number(a.enforced))
                .map((candidate) => (
                  <Connector
                    key={candidate.id}
                    edge={candidate}
                    lit={lit}
                    selected={candidate.id === selectedEdge}
                    onSelect={() => dispatch(selectEdge(candidate.id))}
                  />
                ))}
            </svg>

            {graph.nodes.map((node) => (
              <TableCard
                key={node.table.name}
                node={node}
                selected={node.table.name === selected}
                lit={lit.includes(node.table.name)}
                onSelect={() => dispatch(selectTable(node.table.name))}
              />
            ))}
          </div>
        </div>

        <Legend ready={ready} total={output.tables.length} />

        <ReopenPane
          table={selected}
          hidden={paneOpen}
          onOpen={() => dispatch(setMapPaneOpen(true))}
        />
      </div>

      {/* Two elements, and both are load-bearing. The outer one animates from
          460 to 0 and clips; the inner one stays 460 wide the whole way, so the
          YAML slides out under the edge instead of reflowing narrower and
          narrower as it goes. Rewrapping text mid-animation is what makes a
          panel look like it is being crushed rather than put away.

          Width rather than a grid track: `grid-template-columns` interpolates
          in current Chrome and not everywhere else, and a transition that
          silently degrades to a snap in one browser is the one thing this
          change exists to remove. */}
      <div
        aria-hidden={!paneOpen}
        className={`min-h-0 shrink-0 overflow-hidden transition-[width] duration-200 ease-out motion-reduce:transition-none ${
          paneOpen ? "w-[460px]" : "w-0"
        }`}
      >
        <div className="flex h-full w-[460px]">
          {edge ? (
            <JoinReview
              edge={edge}
              workspace={workspace}
              onClose={() => dispatch(selectEdge(null))}
              onSettled={() => dispatch(selectEdge(null))}
            />
          ) : (
            <SemanticViewPane
              workspace={workspace}
              table={selected}
              onReview={() => dispatch(setView("workspace"))}
              onClose={() => dispatch(setMapPaneOpen(false))}
            />
          )}
        </div>
      </div>
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
 * Sits opposite the legend, on the edge it would emerge from, and crosses that
 * edge as it arrives: it slides in from where the panel just left rather than
 * appearing on top of it half-closed.
 *
 * Kept mounted while the panel is open rather than removed, because a control
 * that is added to the DOM at the end of an animation cannot be part of it.
 */
function ReopenPane({
  table,
  hidden,
  onOpen,
}: {
  table: string | null;
  hidden: boolean;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      tabIndex={hidden ? -1 : undefined}
      aria-hidden={hidden}
      // `translate`, not `transform`: Tailwind v4 compiles `translate-x-*` to
      // the standalone `translate` property, so a transition naming `transform`
      // matches nothing and the slide snaps. Confirmed in the browser —
      // computed `transform` was `none` while `translate` was `12px`.
      className={`absolute bottom-4 right-4 z-10 inline-flex max-w-[280px] items-center gap-2 rounded-[--radius-panel] border border-line bg-surface px-3 py-2 text-left transition-[opacity,translate] duration-200 ease-out hover:border-line-strong motion-reduce:transition-none ${
        hidden ? "pointer-events-none translate-x-3 opacity-0" : "translate-x-0 opacity-100"
      }`}
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
  lit,
  onSelect,
}: {
  node: Node;
  selected: boolean;
  /** An end of the relationship under review. Marked, but not as a selection —
   *  the reviewer chose the line, not this. */
  lit: boolean;
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
          : lit
            ? "border-x-line-ink border-b-line-ink"
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

/**
 * A relationship, and a place to click on it.
 *
 * Two paths for one line. The visible one is a hairline, which is unclickable
 * by any honest measure — a 1px diagonal target is a test of aim, not an
 * affordance. The transparent one underneath is 14px wide and takes the
 * pointer, so the line can be selected without being drawn thick enough to
 * clutter a map of forty of them.
 */
function Connector({
  edge,
  lit,
  selected,
  onSelect,
}: {
  edge: Edge;
  lit: string[];
  selected: boolean;
  onSelect: () => void;
}) {
  const touched = selected || lit.includes(edge.from) || lit.includes(edge.to);
  const path = edge.points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");

  return (
    <g className="cursor-pointer">
      <path
        d={path}
        fill="none"
        stroke={selected ? "var(--color-violet)" : touched ? "var(--color-line-ink)" : "var(--color-line-strong)"}
        strokeWidth={selected ? 2 : touched ? 1.5 : 1}
        // Dashed where the database does not enforce it: a verified join and a
        // guaranteed one are not the same promise, and the map is the one place
        // that difference is visible at a glance.
        strokeDasharray={edge.enforced ? undefined : "4 3"}
      />
      <path
        d={path}
        fill="none"
        stroke="transparent"
        strokeWidth={14}
        onClick={onSelect}
        role="button"
        tabIndex={0}
        aria-label={`Review the relationship ${edge.from}.${edge.via} to ${edge.to}`}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onSelect();
          }
        }}
      >
        <title>{`${edge.from}.${edge.via} → ${edge.to}${edge.enforced ? " (enforced)" : ""}`}</title>
      </path>
    </g>
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
