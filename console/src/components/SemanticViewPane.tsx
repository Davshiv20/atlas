import { useState } from "react";

import { useSemanticViewQuery } from "@/store/api";

/**
 * The emitted view, beside the map that produced it.
 *
 * This is the artifact — what an agent is actually handed — and until now it
 * existed only as a file on the engine host. Showing it next to the schema
 * makes the relationship between reviewing a claim and changing the output
 * visible, which is the one thing the review workbench could never demonstrate
 * about itself.
 *
 * Dark on purpose: it is generated output rather than an editable surface, and
 * the reversal says so without a label.
 */
export function SemanticViewPane({
  workspace,
  table,
  onReview,
  onClose,
  bordered = true,
}: {
  workspace: string;
  table: string | null;
  onReview: () => void;
  /** Given where the pane shares the screen with something it can uncover by
   *  leaving. Absent where the pane *is* the screen and closing it would
   *  leave nothing behind. */
  onClose?: () => void;
  /** False when it fills a pane of its own rather than sitting beside one. */
  bordered?: boolean;
}) {
  const { data, isFetching } = useSemanticViewQuery(
    { workspace, table: table ?? undefined },
    { skip: !workspace },
  );
  const [copied, setCopied] = useState(false);

  const view = data?.view.tables.find((t) => t.name === table);
  const pending = view?.pending ?? 0;

  const copy = async () => {
    if (!data) return;
    await navigator.clipboard.writeText(data.yaml);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <aside
      className={`flex min-h-0 flex-1 flex-col bg-[#1c1c1a] ${bordered ? "border-l border-line" : ""}`}
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-white/10 px-4 py-3">
        <span className="ident text-[#e8e8e2]">semantic_view.yaml</span>
        {table && <span className="ident text-[#8b8b83]">{table}</span>}
        <button
          type="button"
          onClick={copy}
          disabled={!data}
          className="ml-auto rounded-[--radius-control] border border-white/15 px-2 py-1 text-meta text-[#c9c9c1] hover:border-white/30 hover:text-white disabled:opacity-40"
        >
          {copied ? "Copied" : "Copy"}
        </button>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            title="Close the semantic view"
            aria-label="Close the semantic view"
            className="-mr-1 rounded-[--radius-control] px-1.5 py-1 text-[#8b8b83] hover:text-white"
          >
            {/* Drawn rather than a glyph: × renders at a different weight and
                baseline in every fallback font, and beside a hairline header
                that shows. */}
            <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden>
              <path
                d="M1 1l9 9M10 1l-9 9"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
          </button>
        )}
      </header>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        {!table ? (
          <p className="text-body text-[#8b8b83]">
            Select a table to see what an agent would be given for it.
          </p>
        ) : isFetching && !data ? (
          <p className="text-body text-[#8b8b83]">Building…</p>
        ) : data?.yaml ? (
          <Highlighted yaml={data.yaml} />
        ) : (
          <p className="text-body text-[#8b8b83]">
            Nothing to emit for this table — it has not been analysed.
          </p>
        )}
      </div>

      {table && (
        <footer className="flex shrink-0 items-center gap-3 border-t border-white/10 px-4 py-3">
          <span className="text-meta text-[#8b8b83]">
            {pending > 0
              ? `${pending} pending ${pending === 1 ? "claim blocks" : "claims block"} emit`
              : view
                ? "Ready to emit"
                : "Not analysed"}
          </span>
          {pending > 0 && (
            <button
              type="button"
              onClick={onReview}
              className="ml-auto rounded-[--radius-control] bg-[#f6f6f3] px-3 py-1.5 text-body font-medium text-[#1c1c1a] hover:bg-white"
            >
              Review them
            </button>
          )}
        </footer>
      )}
    </aside>
  );
}

/**
 * Enough highlighting to read structure, and no more.
 *
 * A tokenizer for a document we generate ourselves is effort spent on a
 * problem we do not have: the shapes are known, so the lines are matched
 * rather than parsed. Comments carry the review state, so they are the one
 * thing that must never be mistaken for a value.
 */
function Highlighted({ yaml }: { yaml: string }) {
  return (
    <pre className="ident whitespace-pre text-[12.5px] leading-[20px]">
      {yaml.split("\n").map((line, index) => (
        <div key={index}>{colour(line)}</div>
      ))}
    </pre>
  );
}

function colour(line: string): React.ReactNode {
  const comment = line.indexOf("#");
  if (comment >= 0) {
    return (
      <>
        {colour(line.slice(0, comment))}
        <span className="text-[#7c7c73]">{line.slice(comment)}</span>
      </>
    );
  }

  const match = /^(\s*-?\s*)([\w_]+)(:)(.*)$/.exec(line);
  if (!match) return <span className="text-[#c9c9c1]">{line}</span>;

  const [, indent, key, colon, rest] = match;
  return (
    <>
      <span className="text-[#c9c9c1]">{indent}</span>
      <span className="text-[#9aa7ff]">{key}</span>
      <span className="text-[#7c7c73]">{colon}</span>
      <span className={value(rest ?? "")}>{rest}</span>
    </>
  );
}

function value(rest: string): string {
  const text = rest.trim();
  if (text === "true" || text === "false" || /^\d+$/.test(text)) return "text-[#e0a458]";
  return "text-[#a8cf9a]";
}
