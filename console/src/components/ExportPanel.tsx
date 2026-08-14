import { useState } from "react";

import { exportUrl } from "@/store/api";
import { useSemanticViewQuery } from "@/store/api";
import { useAppSelector } from "@/store";

/**
 * Getting the emitted view out.
 *
 * Two things from one URL. The download is the obvious half; the link beside it
 * is the half PRODUCT.md §16 argues for — *"the product should become a runtime
 * service rather than only producing files"* — and it is the same resource, so
 * a file and the view an agent is fetching cannot drift apart.
 *
 * The choice this panel exists to put in front of someone is what goes in it.
 * The console shows every captured table with its review state; a file does not
 * get read that way, so the default is what passed review and including the
 * rest is a decision, taken here, and then stated in the file itself.
 */
export function ExportPanel({ disabled }: { disabled: boolean }) {
  const workspace = useAppSelector((s) => s.ui.workspace);
  const [open, setOpen] = useState(false);
  const [includeAll, setIncludeAll] = useState(false);
  const [format, setFormat] = useState<"yaml" | "json">("yaml");
  const [copied, setCopied] = useState(false);

  // The counts the map legend already shows, from the query the map already
  // runs — a second endpoint to count the same thing is a second answer.
  const { data } = useSemanticViewQuery({ workspace: workspace ?? "" }, { skip: !workspace });
  const ready = data?.ready ?? 0;
  const total = data?.tables ?? 0;
  const held = Math.max(total - ready, 0);

  if (!workspace) return null;

  const url = exportUrl(workspace, { format, include: includeAll ? "all" : "ready" });

  const copy = async () => {
    await navigator.clipboard.writeText(new URL(url, window.location.origin).toString());
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        aria-expanded={open}
        className="rounded-[--radius-control] border border-line px-3 py-1.5 text-body text-ink-2 transition-colors hover:border-line-strong hover:text-ink disabled:cursor-not-allowed disabled:text-ink-4"
      >
        Export
      </button>

      {open && (
        <div className="absolute right-0 z-10 mt-2 w-[340px] rounded-[--radius-panel] border border-line bg-surface p-4">
          <p className="text-panel font-semibold text-ink">Export semantic view</p>
          <p className="mt-1 text-meta text-ink-3">
            {ready} of {total} tables passed review
          </p>

          <fieldset className="mt-3">
            <legend className="sr-only">What to include</legend>
            <Choice
              checked={!includeAll}
              onSelect={() => setIncludeAll(false)}
              label={`Only tables that passed review (${ready})`}
              detail="What an agent can act on without a caveat."
            />
            <Choice
              checked={includeAll}
              onSelect={() => setIncludeAll(true)}
              label={held ? `Include unvalidated tables (+${held})` : "Include everything"}
              detail={
                held
                  ? "Their meaning is the model's, unreviewed. The file says so at the top."
                  : "Nothing is outstanding, so this is the same file."
              }
            />
          </fieldset>

          <div className="mt-3 flex items-center gap-2">
            <span className="text-meta text-ink-3">Format</span>
            {(["yaml", "json"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setFormat(option)}
                aria-pressed={format === option}
                className={`ident rounded-[--radius-control] border px-2 py-[3px] text-meta transition-colors ${
                  format === option
                    ? "border-line-ink text-ink"
                    : "border-line text-ink-3 hover:border-line-strong"
                }`}
              >
                {option}
              </button>
            ))}
          </div>

          <div className="mt-4 flex items-center gap-2">
            {/* A plain link, not a blob. The browser already knows what to do
                with Content-Disposition, and there is no object URL to leak. */}
            <a
              href={`${url}&download=1`}
              download
              onClick={() => setOpen(false)}
              className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink transition-colors hover:bg-cta-hover"
            >
              Download
            </a>
            <button
              type="button"
              onClick={copy}
              className="rounded-[--radius-control] border border-line px-3 py-1.5 text-body text-ink-2 transition-colors hover:border-line-strong hover:text-ink"
            >
              {copied ? "Copied" : "Copy link"}
            </button>
          </div>

          <p className="mt-3 border-t border-line pt-3 text-meta text-ink-3">
            The link stays current — it is rebuilt from the record on every request, so
            an agent can keep fetching it rather than holding a copy that goes stale.
          </p>
        </div>
      )}
    </div>
  );
}

function Choice({
  checked,
  onSelect,
  label,
  detail,
}: {
  checked: boolean;
  onSelect: () => void;
  label: string;
  detail: string;
}) {
  return (
    <label className="mt-2 flex items-start gap-2 text-body text-ink-2">
      <input type="radio" checked={checked} onChange={onSelect} className="mt-[4px]" />
      <span>
        {label}
        <span className="mt-0.5 block text-meta text-ink-3">{detail}</span>
      </span>
    </label>
  );
}
