import { useState } from "react";

/**
 * The connection form. Shared by setup and the sources screen so there is one
 * implementation of what a source is.
 *
 * It collects the *name* of the environment variable holding the connection
 * string, never the string. The engine has no authentication yet, so a form
 * that accepted credentials would be a credential-theft and SSRF surface.
 */
export interface SourceDraft {
  id: string;
  adapter: string;
  url_env: string;
  namespace: string;
  label?: string;
}

export function SourceForm({
  busy,
  error,
  submitLabel = "Save source",
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  error: string | null;
  submitLabel?: string;
  onSubmit: (source: SourceDraft) => void;
  onCancel?: () => void;
}) {
  const [id, setId] = useState("");
  const [adapter, setAdapter] = useState("postgresql");
  const [namespace, setNamespace] = useState("public");
  const [label, setLabel] = useState("");
  const [urlEnv, setUrlEnv] = useState("");

  // Suggested, not imposed: a predictable name is one less thing to mistype
  // across the form and the .env file, but sharing one variable is legitimate.
  const suggested = id ? `${id.toUpperCase().replace(/[^A-Z0-9]/g, "_")}_DATABASE_URL` : "";
  const effectiveEnv = urlEnv || suggested;
  const snowflake = adapter === "snowflake";

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({
          id,
          adapter,
          url_env: effectiveEnv,
          namespace,
          ...(label ? { label } : {}),
        });
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name" hint="lowercase; identifies this source">
          <input
            value={id}
            onChange={(e) => setId(e.target.value.toLowerCase())}
            placeholder="elara"
            required
            autoFocus
            className={INPUT}
          />
        </Field>

        <Field label="Engine">
          <select value={adapter} onChange={(e) => setAdapter(e.target.value)} className={INPUT}>
            <option value="postgresql">PostgreSQL</option>
            <option value="snowflake">Snowflake</option>
          </select>
        </Field>

        <Field label="Schema" hint={snowflake ? "DATABASE.SCHEMA" : "the schema to read"}>
          <input
            value={namespace}
            onChange={(e) => setNamespace(e.target.value)}
            placeholder={snowflake ? "ANALYTICS.PUBLIC" : "public"}
            required
            className={INPUT}
          />
        </Field>

        <Field label="Label" hint="optional">
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Elara dev"
            className={INPUT}
          />
        </Field>
      </div>

      <div className="mt-4 rounded-[--radius-control] border border-line bg-raised p-3">
        <Field label="Environment variable" hint="where the connection string lives">
          <input
            value={effectiveEnv}
            onChange={(e) => setUrlEnv(e.target.value.toUpperCase())}
            placeholder="ELARA_DATABASE_URL"
            required
            className={`${INPUT} font-mono`}
          />
        </Field>
        <p className="mt-2 text-meta text-ink-3">
          The credential itself never passes through this form. You will put it in{" "}
          <span className="ident">engine/.env</span> next.
        </p>
      </div>

      {error && (
        <p className="mt-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {error}
        </p>
      )}

      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={busy || !id || !effectiveEnv}
          className="rounded-[--radius-control] bg-cta px-4 py-2 text-body font-medium text-cta-ink transition-colors hover:bg-cta-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-4"
        >
          {busy ? "Saving…" : submitLabel}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-[--radius-control] px-3 py-2 text-body text-ink-2 hover:text-ink"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}

const INPUT =
  "w-full rounded-[--radius-control] border border-line bg-canvas px-2.5 py-1.5 text-body text-ink placeholder:text-ink-4 focus:border-line-ink focus:outline-none";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">
        {label}
        {hint && <span className="ml-1.5 font-normal normal-case tracking-normal">{hint}</span>}
      </span>
      {children}
    </label>
  );
}
