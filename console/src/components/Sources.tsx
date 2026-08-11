import { useState } from "react";

import type { SourceStatus } from "@/api/types";
import { Modal } from "@/components/Modal";
import { SourceForm } from "@/components/SourceForm";
import {
  useCreateSourceMutation,
  useDeleteSourceMutation,
  useExtractMutation,
  useForgetCredentialsMutation,
  useSetCredentialsMutation,
  useSourcesQuery,
  useTestSourceMutation,
} from "@/store/api";
import { describeError } from "@/api/errors";
import { selectWorkspace } from "@/store/uiSlice";
import { useAppDispatch } from "@/store";

/**
 * Connections, in the shape a database client uses: a centred grid of what is
 * plugged in, and a tile that opens a dialog to plug in something else.
 *
 * The previous full-page form left an almost-empty screen carrying one line of
 * text, because a five-field setup form does not fill a page. A connection is
 * something you configure and dismiss — so it belongs in a popup, and the page
 * shows what is connected.
 */
export function Sources() {
  const dispatch = useAppDispatch();
  const { data: sources, isLoading } = useSourcesQuery();
  const [create, { isLoading: creating, error: createError }] = useCreateSourceMutation();
  const [adding, setAdding] = useState(false);
  const [configuring, setConfiguring] = useState<SourceStatus | null>(null);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[860px] px-6 py-12">
        <header className="mb-8">
          <h1 className="display text-display text-ink">Connections</h1>
          <p className="mt-1.5 max-w-[58ch] text-body text-ink-2">
            Each connection names a database and the environment variable holding its
            credentials. The credential itself never reaches this screen.
          </p>
        </header>

        {isLoading ? (
          <p className="text-body text-ink-3">Loading…</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {(sources ?? []).map((source) => (
              <ConnectionCard
                key={source.id}
                source={source}
                onConfigure={() => setConfiguring(source)}
                onOpen={() => dispatch(selectWorkspace(source.id))}
              />
            ))}
            <NewConnectionTile onClick={() => setAdding(true)} />
          </div>
        )}
      </div>

      {adding && (
        <Modal
          title="New connection"
          description="Atlas reads this schema. Nothing is written to your database."
          onClose={() => setAdding(false)}
        >
          <SourceForm
            busy={creating}
            error={createError ? describeError(createError, "Could not save the connection.") : null}
            submitLabel="Add connection"
            onSubmit={async (draft) => {
              await create(draft).unwrap().catch(() => undefined);
              setAdding(false);
            }}
            onCancel={() => setAdding(false)}
          />
        </Modal>
      )}

      {configuring && (
        <ConfigureModal source={configuring} onClose={() => setConfiguring(null)} />
      )}
    </div>
  );
}

function ConnectionCard({
  source,
  onConfigure,
  onOpen,
}: {
  source: SourceStatus;
  onConfigure: () => void;
  onOpen: () => void;
}) {
  return (
    <article className="flex flex-col rounded-[--radius-panel] border border-line bg-surface p-4">
      <div className="flex items-start gap-3">
        <EngineMark adapter={source.adapter} connected={source.health.state === "connected"} />
        <div className="min-w-0 flex-1">
          <h2 className="ident truncate text-body font-medium text-ink">{source.id}</h2>
          <p className="truncate text-meta text-ink-3">
            {source.label ?? source.adapter} · {source.namespace}
          </p>
        </div>
      </div>

      <HealthLine source={source} />

      <div className="mt-4 flex gap-2 border-t border-line pt-3">
        <button
          type="button"
          onClick={onOpen}
          disabled={source.health.state !== "connected"}
          className="rounded-[--radius-control] bg-cta px-2.5 py-1 text-meta font-medium text-cta-ink hover:bg-cta-hover disabled:bg-raised disabled:text-ink-4"
        >
          Open
        </button>
        <button
          type="button"
          onClick={onConfigure}
          className="rounded-[--radius-control] border border-line px-2.5 py-1 text-meta text-ink-2 hover:bg-raised hover:text-ink"
        >
          Configure
        </button>
      </div>
    </article>
  );
}

/** The mark carries the engine, so a mixed list is scannable at a glance. */
function EngineMark({ adapter, connected }: { adapter: string; connected: boolean }) {
  return (
    <span
      aria-hidden
      title={adapter}
      className={`flex size-9 shrink-0 items-center justify-center rounded-[--radius-control] border text-badge font-semibold uppercase ${
        connected ? "border-line-ink bg-cta text-cta-ink" : "border-line bg-raised text-ink-4"
      }`}
    >
      {adapter === "snowflake" ? "SF" : "PG"}
    </span>
  );
}

function NewConnectionTile({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-[128px] flex-col items-center justify-center gap-1.5 rounded-[--radius-panel] border border-dashed border-line-strong bg-canvas p-4 text-ink-3 transition-colors hover:border-line-ink hover:text-ink"
    >
      <span className="text-title leading-none">+</span>
      <span className="text-body font-medium">New connection</span>
      <span className="text-meta text-ink-4">PostgreSQL or Snowflake</span>
    </button>
  );
}

/** Everything after a connection exists: check it, read it, remove it. */
function ConfigureModal({ source, onClose }: { source: SourceStatus; onClose: () => void }) {
  const dispatch = useAppDispatch();
  const [test, { data: result, isLoading: testing }] = useTestSourceMutation();
  const [extract, { isLoading: extracting }] = useExtractMutation();
  const [remove] = useDeleteSourceMutation();
  const [extractError, setExtractError] = useState<string | null>(null);

  return (
    <Modal
      title={source.id}
      description={`${source.adapter} · ${source.namespace}`}
      onClose={onClose}
    >
      <CredentialField source={source} />

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => test(source.id)}
          disabled={testing}
          className="rounded-[--radius-control] border border-line px-3 py-1.5 text-body text-ink hover:bg-raised disabled:text-ink-4"
        >
          {testing ? "Checking…" : "Test connection"}
        </button>

        <button
          type="button"
          disabled={source.health.state !== "connected" || extracting}
          onClick={async () => {
            setExtractError(null);
            try {
              await extract({ workspace: source.id, sourceId: source.id }).unwrap();
            } catch (error) {
              setExtractError(describeError(error, "Could not read the schema."));
              return;
            }
            dispatch(selectWorkspace(source.id));
            onClose();
          }}
          className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink hover:bg-cta-hover disabled:bg-raised disabled:text-ink-4"
        >
          {extracting ? "Reading schema…" : "Read schema"}
        </button>

        <button
          type="button"
          onClick={() => {
            remove(source.id);
            onClose();
          }}
          className="ml-auto rounded-[--radius-control] px-2.5 py-1.5 text-body text-red hover:bg-red-soft"
        >
          Remove
        </button>
      </div>

      {extractError && (
        <p className="mt-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {extractError}
        </p>
      )}

      {result && (
        <p
          className={`mt-3 rounded-[--radius-control] border px-3 py-2 text-body ${
            result.state === "connected"
              ? "border-teal/30 bg-teal-soft text-teal-strong"
              : "border-amber/30 bg-amber-soft text-amber-strong"
          }`}
        >
          {result.state === "connected" ? "Connected — " : ""}
          {result.detail}
        </p>
      )}
    </Modal>
  );
}

/**
 * Connection state, in words.
 *
 * Three states, never two: `configured` is not `connected`, and conflating them
 * is how a wrong password reads as success.
 */
function HealthLine({ source }: { source: SourceStatus }) {
  const { state, detail, checked_at } = source.health;

  const dot =
    state === "connected" ? "bg-teal" : state === "failed" ? "bg-red" : "bg-ink-4";
  const label =
    state === "connected"
      ? "Connected"
      : state === "failed"
        ? source.configured
          ? "Cannot connect"
          : "Credentials not set"
        : "Not checked";

  return (
    <div className="mt-3">
      <p className="flex items-center gap-1.5 text-meta">
        <span aria-hidden className={`size-[6px] rounded-full ${dot}`} />
        <span className={state === "connected" ? "text-teal-strong" : "text-ink-2"}>
          {label}
        </span>
        {checked_at && (
          <time className="text-ink-4" dateTime={checked_at}>
            · checked {relative(checked_at)}
          </time>
        )}
      </p>
      {detail && (
        <p className="mt-1 line-clamp-2 text-meta text-ink-3" title={detail}>
          {detail}
        </p>
      )}
    </div>
  );
}

function relative(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/**
 * The connection string, editable in place.
 *
 * Saving writes it to the engine's secret store *and* into the running
 * process's environment, then re-probes — so the answer to "does this work"
 * arrives without editing a file or restarting anything.
 *
 * The stored value is never sent back to the browser: once saved, the field
 * shows that a credential exists, not what it is.
 */
function CredentialField({ source }: { source: SourceStatus }) {
  const [save, { isLoading: saving, data: result }] = useSetCredentialsMutation();
  const [forget] = useForgetCredentialsMutation();
  const [url, setUrl] = useState("");
  const [editing, setEditing] = useState(!source.configured);
  const [saveError, setSaveError] = useState<string | null>(null);

  const placeholder =
    source.adapter === "snowflake"
      ? "snowflake://user:pass@account/DB/SCHEMA?warehouse=WH&role=ATLAS_READER"
      : "postgresql+psycopg://user:pass@host:5432/db";

  if (!editing) {
    return (
      <div className="rounded-[--radius-control] border border-line bg-raised p-3">
        <div className="flex items-center gap-2">
          <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">
            Connection string
          </span>
          <span className="ident text-meta text-ink-3">
            {source.managed ? "stored by Atlas" : `from $${source.url_env}`}
          </span>
          <span className="ml-auto flex gap-2">
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="rounded-[--radius-control] border border-line px-2 py-0.5 text-meta text-ink-2 hover:bg-surface hover:text-ink"
            >
              Replace
            </button>
            {source.managed && (
              <button
                type="button"
                onClick={() => forget(source.id)}
                className="rounded-[--radius-control] px-2 py-0.5 text-meta text-red hover:bg-red-soft"
              >
                Forget
              </button>
            )}
          </span>
        </div>
      </div>
    );
  }

  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        setSaveError(null);
        try {
          const health = await save({ id: source.id, url }).unwrap();
          if (health.state === "connected") {
            setUrl("");
            setEditing(false);
          }
        } catch (error) {
          setSaveError(describeError(error, "Could not store the connection string."));
        }
      }}
    >
      <label className="flex flex-col gap-1">
        <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">
          Connection string
        </span>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={placeholder}
          autoFocus
          spellCheck={false}
          className="ident w-full rounded-[--radius-control] border border-line bg-canvas px-2.5 py-2 text-ink placeholder:text-ink-4 focus:border-line-ink focus:outline-none"
        />
      </label>

      <div className="mt-2 flex items-center gap-2">
        <button
          type="submit"
          disabled={saving || !url.trim()}
          className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink hover:bg-cta-hover disabled:bg-raised disabled:text-ink-4"
        >
          {saving ? "Connecting…" : "Save and test"}
        </button>
        {source.configured && (
          <button
            type="button"
            onClick={() => setEditing(false)}
            className="rounded-[--radius-control] px-2.5 py-1.5 text-body text-ink-2 hover:text-ink"
          >
            Cancel
          </button>
        )}
        <span className="ml-auto text-meta text-ink-4">
          Stored on the engine host, not in the browser
        </span>
      </div>

      {(saveError || (result && result.state !== "connected")) && (
        <p className="mt-2 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {saveError ?? result?.detail}
        </p>
      )}
    </form>
  );
}
