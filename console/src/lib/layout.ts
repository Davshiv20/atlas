import type { Claim, SchemaOutput, TableOutput } from "@/api/types";

/**
 * Layered layout for the schema map.
 *
 * Hand-rolled rather than pulled from a graph library: a schema is a shallow,
 * hub-shaped graph — most edges point at a handful of central tables — and a
 * general force-directed engine spends its effort on cases this data does not
 * have, while producing a different picture on every render. Determinism
 * matters more than optimality here: a reviewer who moves away and comes back
 * should find the same map.
 *
 * Three passes: rank by how far a table sits from the things nothing points
 * at, order within each rank to pull connected tables together, then place.
 * If this stops holding up past a few hundred tables, replace it with elk or
 * dagre — the shape of the output is what the renderer depends on, not how it
 * was arrived at.
 */

export interface Node {
  table: TableOutput;
  x: number;
  y: number;
  width: number;
  height: number;
  rank: number;
}

export interface Edge {
  /** Stable across renders, so a selected edge survives a refetch. */
  id: string;
  from: string;
  to: string;
  /** The column on the source side, so an edge can be read without a click. */
  via: string;
  /** What it points at on the far side. Needed to state the join in full.  */
  referredColumns: string[];
  enforced: boolean;
  /**
   * The claim describing this relationship, where one exists.
   *
   * Carried onto the edge because the map is where a join is legible: the
   * queue can only render it as a sentence, and a sentence about two tables is
   * the one claim shape a reader cannot check by reading.
   */
  claim: Claim | undefined;
  points: { x: number; y: number }[];
}

export interface Graph {
  nodes: Node[];
  edges: Edge[];
  width: number;
  height: number;
}

const NODE_WIDTH = 208;
const NODE_HEIGHT = 74;
const RANK_GAP = 132;
const NODE_GAP = 28;
const PADDING = 48;

export function layout(output: SchemaOutput): Graph {
  const tables = output.tables;
  const byName = new Map(tables.map((t) => [t.name, t]));

  const links = tables.flatMap((table) =>
    table.joins
      .filter((join) => join.referred_table && byName.has(join.referred_table))
      .map((join) => ({
        id: `${table.name}.${join.columns.join("_")}->${join.referred_table}`,
        from: table.name,
        to: join.referred_table!,
        via: join.columns[0] ?? "",
        referredColumns: join.referred_columns,
        enforced: join.enforced,
        claim: join.description,
      })),
  );

  const ranks = rankTables(tables, links);
  const columns = groupByRank(tables, ranks);
  order(columns, links);

  const nodes: Node[] = [];
  const tallest = Math.max(...columns.map((c) => c.length), 1);
  const canvasHeight = tallest * (NODE_HEIGHT + NODE_GAP) - NODE_GAP + PADDING * 2;

  columns.forEach((column, rank) => {
    // Each column is centred against the tallest, so the map reads as a shape
    // rather than as everything hanging from the top edge.
    const columnHeight = column.length * (NODE_HEIGHT + NODE_GAP) - NODE_GAP;
    const top = (canvasHeight - columnHeight) / 2;
    column.forEach((table, index) => {
      nodes.push({
        table,
        x: PADDING + rank * (NODE_WIDTH + RANK_GAP),
        y: top + index * (NODE_HEIGHT + NODE_GAP),
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        rank,
      });
    });
  });

  const positions = new Map(nodes.map((n) => [n.table.name, n]));
  const edges: Edge[] = links
    .map((link) => {
      const from = positions.get(link.from);
      const to = positions.get(link.to);
      if (!from || !to) return null;
      return { ...link, points: route(from, to) };
    })
    .filter((edge): edge is Edge => edge !== null);

  return {
    nodes,
    edges,
    width: columns.length * (NODE_WIDTH + RANK_GAP) - RANK_GAP + PADDING * 2,
    height: canvasHeight,
  };
}

/**
 * How far each table sits from a root.
 *
 * Roots are the tables nothing references — the leaves of the dependency
 * graph, which is where a reader starts. Hubs like `users` end up deepest,
 * which puts the most-referenced tables on one side instead of in the middle
 * of everything.
 */
function rankTables(
  tables: TableOutput[],
  links: { from: string; to: string }[],
): Map<string, number> {
  const targets = new Map<string, string[]>();
  for (const link of links) {
    if (link.from === link.to) continue; // self-reference ranks nothing
    targets.set(link.from, [...(targets.get(link.from) ?? []), link.to]);
  }

  const ranks = new Map<string, number>();
  const visiting = new Set<string>();

  const depth = (name: string): number => {
    const known = ranks.get(name);
    if (known !== undefined) return known;
    // A cycle has no well-defined depth; breaking it at the revisit keeps the
    // layout deterministic instead of recursing forever.
    if (visiting.has(name)) return 0;

    visiting.add(name);
    const children = targets.get(name) ?? [];
    const rank = children.length === 0 ? 0 : 1 + Math.max(...children.map(depth));
    visiting.delete(name);
    ranks.set(name, rank);
    return rank;
  };

  for (const table of tables) depth(table.name);
  return ranks;
}

function groupByRank(tables: TableOutput[], ranks: Map<string, number>): TableOutput[][] {
  const depth = Math.max(...tables.map((t) => ranks.get(t.name) ?? 0), 0);
  const columns: TableOutput[][] = Array.from({ length: depth + 1 }, () => []);
  for (const table of tables) columns[ranks.get(table.name) ?? 0]!.push(table);
  return columns;
}

/**
 * Pull connected tables level with each other.
 *
 * One barycentre sweep, not the usual iterate-to-convergence: on a hub-shaped
 * graph the second pass changes almost nothing, and a single deterministic
 * sweep is easier to reason about than a loop with a fudged stopping rule.
 */
function order(columns: TableOutput[][], links: { from: string; to: string }[]): void {
  const neighbours = new Map<string, string[]>();
  for (const link of links) {
    neighbours.set(link.from, [...(neighbours.get(link.from) ?? []), link.to]);
    neighbours.set(link.to, [...(neighbours.get(link.to) ?? []), link.from]);
  }

  const index = new Map<string, number>();
  columns.forEach((column) => {
    column.sort((a, b) => a.name.localeCompare(b.name));
    column.forEach((table, position) => index.set(table.name, position));
  });

  for (const column of columns) {
    column.sort((a, b) => barycentre(a, neighbours, index) - barycentre(b, neighbours, index));
    column.forEach((table, position) => index.set(table.name, position));
  }
}

function barycentre(
  table: TableOutput,
  neighbours: Map<string, string[]>,
  index: Map<string, number>,
): number {
  const linked = (neighbours.get(table.name) ?? [])
    .map((name) => index.get(name))
    .filter((position): position is number => position !== undefined);
  if (linked.length === 0) return index.get(table.name) ?? 0;
  return linked.reduce((total, position) => total + position, 0) / linked.length;
}

/**
 * An orthogonal elbow between two nodes.
 *
 * Right angles rather than curves: a schema map is read by following an edge
 * to its end, and straight segments are easier to track across a dense middle
 * than a bundle of similar-looking arcs.
 */
function route(from: Node, to: Node): { x: number; y: number }[] {
  const start = { x: from.x + from.width, y: from.y + from.height / 2 };
  const end = { x: to.x, y: to.y + to.height / 2 };

  if (to.x < from.x) {
    // Pointing backwards: leave from the left edge instead of crossing the node.
    const back = { x: from.x, y: from.y + from.height / 2 };
    const finish = { x: to.x + to.width, y: to.y + to.height / 2 };
    const middle = (back.x + finish.x) / 2;
    return [back, { x: middle, y: back.y }, { x: middle, y: finish.y }, finish];
  }

  if (Math.abs(start.y - end.y) < 1) return [start, end];

  const middle = (start.x + end.x) / 2;
  return [start, { x: middle, y: start.y }, { x: middle, y: end.y }, end];
}
