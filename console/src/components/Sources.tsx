import { useState } from "react";

import type { SnowflakeAuthMethod, SourceStatus, WorkspaceSummary } from "@/api/types";
import { Modal } from "@/components/Modal";
import { SourceForm } from "@/components/SourceForm";
import {
  useCreateSourceMutation,
  useCreateWorkspaceMutation,
  useDeleteSourceMutation,
  useDeleteWorkspaceMutation,
  useExtractMutation,
  useForgetCredentialsMutation,
  useSetCredentialsMutation,
  useSetSnowflakeCredentialsMutation,
  useSourcesQuery,
  useTestSourceMutation,
  useWorkspacesQuery,
} from "@/store/api";
import { describeError } from "@/api/errors";
import { clearWorkspace, selectWorkspace, setView } from "@/store/uiSlice";
import { useAppDispatch, useAppSelector } from "@/store";

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
  const { data: workspaces } = useWorkspacesQuery();
  const [create, { isLoading: creating, error: createError }] = useCreateSourceMutation();
  const [saveSnowflake, { isLoading: savingSnowflake }] = useSetSnowflakeCredentialsMutation();
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
                workspaces={(workspaces ?? []).filter((workspace) => workspace.source_id === source.id)}
                onConfigure={() => setConfiguring(source)}
                onOpen={(workspaceId) => {
                  // Selecting is not opening. Without the view change the
                  // screen stayed on Connections, so the button appeared to do
                  // nothing while the workspace had in fact switched underneath
                  // it — and whatever was open before still looked current.
                  dispatch(selectWorkspace(workspaceId));
                  dispatch(setView("workspace"));
                }}
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
            busy={creating || savingSnowflake}
            error={createError ? describeError(createError, "Could not save the connection.") : null}
            submitLabel="Add connection"
            onSubmit={async (draft) => {
              const { snowflake_credentials: credentials, ...sourceDraft } = draft;
              let source: SourceStatus;
              try {
                source = await create(sourceDraft).unwrap();
              } catch {
                return;
              }
              if (credentials) {
                try {
                  const health = await saveSnowflake({
                    id: source.id,
                    credentials,
                  }).unwrap();
                  if (health.state !== "connected") {
                    setAdding(false);
                    setConfiguring({
                      ...source,
                      configured: true,
                      managed: true,
                      health,
                    });
                    return;
                  }
                } catch (error) {
                  setAdding(false);
                  setConfiguring({
                    ...source,
                    health: {
                      state: "failed",
                      detail: describeError(error, "Could not save the Snowflake credential."),
                    },
                  });
                  return;
                }
              }
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
  workspaces,
  onConfigure,
  onOpen,
}: {
  source: SourceStatus;
  workspaces: WorkspaceSummary[];
  onConfigure: () => void;
  onOpen: (workspaceId: string) => void;
}) {
  const [createWorkspace, { isLoading: creating, error: createError }] =
    useCreateWorkspaceMutation();
  const step = nextStep(source, workspaces);

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
      {workspaces.length > 0 && (
        <p className="mt-2 text-meta text-ink-3">
          {workspaces.length} workspace{workspaces.length === 1 ? "" : "s"}
          {step.kind === "open" ? ` · latest generation ${step.workspace.snapshot_generation}` : ""}
        </p>
      )}

      {/* Say what is missing. A disabled control with no reason beside it is
          the same dead end whether the cause is a missing credential, a
          missing workspace or a missing snapshot — and the reader cannot tell
          which, so they cannot act. */}
      <p className="mt-2 text-meta text-ink-3">{step.why}</p>

      {createError && (
        <p className="mt-2 rounded-[--radius-control] border border-red/25 bg-red-soft px-2 py-1 text-meta text-red">
          {describeError(createError, "Could not create the workspace.")}
        </p>
      )}

      <div className="mt-4 flex gap-2 border-t border-line pt-3">
        {step.kind === "open" ? (
          <button
            type="button"
            onClick={() => onOpen(step.workspace.id)}
            className="rounded-[--radius-control] bg-cta px-2.5 py-1 text-meta font-medium text-cta-ink hover:bg-cta-hover"
          >
            Open workspace
          </button>
        ) : step.kind === "create" ? (
          <button
            type="button"
            disabled={creating}
            onClick={async () => {
              const created = await createWorkspace({
                id: source.id,
                source_id: source.id,
              }).unwrap().catch(() => null);
              // Straight into Configure: the next thing needed is a schema
              // read, and that is where it lives.
              if (created) onConfigure();
            }}
            className="rounded-[--radius-control] bg-cta px-2.5 py-1 text-meta font-medium text-cta-ink hover:bg-cta-hover disabled:bg-raised disabled:text-ink-4"
          >
            {creating ? "Creating…" : "Create workspace"}
          </button>
        ) : (
          <button
            type="button"
            onClick={onConfigure}
            className="rounded-[--radius-control] bg-cta px-2.5 py-1 text-meta font-medium text-cta-ink hover:bg-cta-hover"
          >
            {step.kind === "connect" ? "Add credentials" : "Read schema"}
          </button>
        )}
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

type Step =
  | { kind: "connect"; why: string }
  | { kind: "create"; why: string }
  | { kind: "extract"; why: string }
  | { kind: "open"; why: string; workspace: WorkspaceSummary };

/**
 * The one thing this connection needs next.
 *
 * A source is a way in; a workspace is a captured snapshot of it. "Open
 * workspace" opens the second, so it is inert until the first has produced
 * one — which is correct, and was previously indistinguishable from broken
 * because the card offered that button and nothing else at every stage.
 */
function nextStep(source: SourceStatus, workspaces: WorkspaceSummary[]): Step {
  const openable = workspaces.find((workspace) => workspace.snapshot_available);
  if (openable) {
    return { kind: "open", why: "Ready to review.", workspace: openable };
  }
  if (workspaces.length === 0) {
    return source.health.state === "connected"
      ? { kind: "create", why: "Connected. No workspace yet — create one to capture this schema." }
      : {
          kind: "connect",
          why: "No workspace yet. Add credentials and test the connection first.",
        };
  }
  if (source.health.state !== "connected") {
    return {
      kind: "connect",
      why: "Workspace exists but nothing has been read yet, and the connection is not verified.",
    };
  }
  return { kind: "extract", why: "Workspace is empty. Read the schema to capture a snapshot." };
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
/**
 * What happens, in the order it happens.
 *
 * Four controls sit below this: two of them open a connection to the customer's
 * database, one spends warehouse time reading it, and one throws away work.
 * None of that was stated anywhere, so the only way to learn what a button did
 * was to press it.
 */
function Stages({ source, hasSnapshot }: { source: SourceStatus; hasSnapshot: boolean }) {
  const connected = source.health.state === "connected";
  const stages: { done: boolean; title: string; detail: string }[] = [
    {
      done: source.configured,
      title: "Credentials",
      detail:
        "Stored on the engine host and never returned to this screen. Atlas needs " +
        "SELECT and nothing else — the in-process guards are a second line of " +
        "defence, not the first.",
    },
    {
      done: connected,
      title: "Test connection",
      detail:
        "Opens one connection and asks the server its version. Reads no table " +
        "data. Being configured is not the same as being reachable, which is why " +
        "this is separate.",
    },
    {
      done: hasSnapshot,
      title: "Read schema",
      detail:
        "Reads structure, then profiles every column: row counts, null rates, " +
        "distinct counts and sample values. This is the step that costs real " +
        "query time, and on a warehouse it costs money. Nothing is written to " +
        "your database.",
    },
    {
      done: false,
      title: "Generate semantic view",
      detail:
        "Runs later, from the workspace rather than here. Roughly a minute per " +
        "table, spends model budget, and executes typed checks against your " +
        "database to ground what it proposes.",
    },
  ];

  return (
    <ol className="mb-4 flex flex-col gap-2.5 rounded-[--radius-control] border border-line bg-raised px-3 py-2.5">
      {stages.map((stage) => (
        <li key={stage.title} className="flex gap-2.5">
          <span
            aria-hidden
            className={`mt-[5px] size-[7px] shrink-0 rounded-full ${
              stage.done ? "bg-teal" : "bg-line-strong"
            }`}
          />
          <span className="min-w-0">
            <span className="text-meta font-semibold text-ink">{stage.title}</span>
            <span className="mt-0.5 block text-meta leading-[16px] text-ink-3">
              {stage.detail}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}


function ConfigureModal({ source, onClose }: { source: SourceStatus; onClose: () => void }) {
  const dispatch = useAppDispatch();
  const selectedWorkspace = useAppSelector((state) => state.ui.workspace);
  const { data: workspaces } = useWorkspacesQuery();
  const existingWorkspace = (workspaces ?? []).find((workspace) => workspace.source_id === source.id);
  const workspaceId = existingWorkspace?.id ?? source.id;
  const [test, { data: result, isLoading: testing }] = useTestSourceMutation();
  const [createWorkspace] = useCreateWorkspaceMutation();
  const [extract, { isLoading: extracting }] = useExtractMutation();
  const [deleteWorkspace, { isLoading: deletingWorkspace }] = useDeleteWorkspaceMutation();
  const [remove, { isLoading: deletingSource }] = useDeleteSourceMutation();
  const [extractError, setExtractError] = useState<string | null>(null);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);

  return (
    <Modal
      title={source.id}
      description={`${source.adapter} · ${source.namespace}`}
      onClose={onClose}
    >
      {source.health.state === "failed" && source.health.detail && (
        <p className="mb-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {source.health.detail}
        </p>
      )}

      {/* What each control does, before it is pressed. Two of the four read the
          database and one discards work, and none of them said so. */}
      <Stages source={source} hasSnapshot={Boolean(existingWorkspace?.snapshot_available)} />

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
            const resetSemantics = Boolean(existingWorkspace?.snapshot_available);
            if (
              resetSemantics &&
              !window.confirm(
                `Refresh ${workspaceId}? This resets claims, evidence, questions, reviews, and derived output for this workspace only.`,
              )
            ) {
              return;
            }
            try {
              if (!existingWorkspace) {
                await createWorkspace({ id: workspaceId, source_id: source.id }).unwrap();
              }
              await extract({ workspace: workspaceId, resetSemantics }).unwrap();
            } catch (error) {
              setExtractError(describeError(error, "Could not read the schema."));
              return;
            }
            dispatch(selectWorkspace(workspaceId));
            onClose();
          }}
          className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink hover:bg-cta-hover disabled:bg-raised disabled:text-ink-4"
        >
          {extracting ? "Reading schema…" : "Read schema"}
        </button>

        {existingWorkspace && (
          <button
            type="button"
            disabled={deletingWorkspace}
            onClick={async () => {
              if (
                !window.confirm(
                  `Delete workspace ${existingWorkspace.id}? Its snapshot, claims, evidence, questions, reviews, and jobs will be removed. The source connection remains.`,
                )
              ) {
                return;
              }
              setLifecycleError(null);
              try {
                await deleteWorkspace(existingWorkspace.id).unwrap();
              } catch (error) {
                setLifecycleError(describeError(error, "Could not delete the workspace."));
                return;
              }
              if (selectedWorkspace === existingWorkspace.id) dispatch(clearWorkspace());
              onClose();
            }}
            className="ml-auto rounded-[--radius-control] px-2.5 py-1.5 text-body text-red hover:bg-red-soft disabled:text-ink-4"
          >
            {deletingWorkspace ? "Deleting workspace…" : "Delete workspace"}
          </button>
        )}

        <button
          type="button"
          disabled={Boolean(existingWorkspace) || deletingSource}
          title={existingWorkspace ? "Delete the workspace before removing its source." : undefined}
          onClick={async () => {
            if (!window.confirm(`Remove source ${source.id}?`)) return;
            setLifecycleError(null);
            try {
              await remove(source.id).unwrap();
            } catch (error) {
              setLifecycleError(describeError(error, "Could not remove the source."));
              return;
            }
            onClose();
          }}
          className={`${existingWorkspace ? "" : "ml-auto"} rounded-[--radius-control] px-2.5 py-1.5 text-body text-red hover:bg-red-soft disabled:text-ink-4`}
        >
          {deletingSource ? "Removing source…" : "Remove source"}
        </button>
      </div>

      {(extractError || lifecycleError) && (
        <p className="mt-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {extractError ?? lifecycleError}
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
  return source.adapter === "snowflake" ? (
    <SnowflakeCredentialField source={source} />
  ) : (
    <UrlCredentialField source={source} />
  );
}

function SnowflakeCredentialField({ source }: { source: SourceStatus }) {
  const [save, { isLoading: saving, data: result }] = useSetSnowflakeCredentialsMutation();
  const [forget] = useForgetCredentialsMutation();
  const [editing, setEditing] = useState(!source.configured);
  const [accountIdentifier, setAccountIdentifier] = useState("");
  const [username, setUsername] = useState("");
  const [authMethod, setAuthMethod] = useState<SnowflakeAuthMethod>("mfa_totp");
  const [privateKeyFile, setPrivateKeyFile] = useState("");
  const [privateKeyPwd, setPrivateKeyPwd] = useState("");
  const [password, setPassword] = useState("");
  const [passcode, setPasscode] = useState("");
  const [warehouse, setWarehouse] = useState("");
  const [role, setRole] = useState("ATLAS_READER");
  const [saveError, setSaveError] = useState<string | null>(null);

  if (!editing) {
    return (
      <div className="rounded-[--radius-control] border border-line bg-raised p-3">
        <div className="flex items-center gap-2">
          <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">
            Snowflake credentials
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
          const health = await save({
            id: source.id,
            credentials: {
              account_identifier: accountIdentifier,
              username,
              auth_method: authMethod,
              ...(authMethod !== "external_browser" && authMethod !== "key_pair"
                ? { password }
                : {}),
              ...(authMethod === "mfa_totp" ? { passcode } : {}),
              ...(authMethod === "key_pair"
                ? {
                    private_key_file: privateKeyFile,
                    ...(privateKeyPwd ? { private_key_file_pwd: privateKeyPwd } : {}),
                  }
                : {}),
              warehouse,
              role,
            },
          }).unwrap();
          if (health.state === "connected") {
            setPassword("");
            setPasscode("");
            setEditing(false);
          }
        } catch (error) {
          setSaveError(describeError(error, "Could not save the Snowflake credential."));
        }
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <CredentialInput label="Account identifier" value={accountIdentifier} onChange={setAccountIdentifier} placeholder="myorg-myaccount" />
        <CredentialInput label="Warehouse" value={warehouse} onChange={setWarehouse} placeholder="POC_WH" />
        <CredentialInput label="Username" value={username} onChange={setUsername} placeholder="SHIVAM" autoComplete="username" />
        <label className="flex flex-col gap-1">
          <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">Sign-in method</span>
          <select
            value={authMethod}
            onChange={(event) => setAuthMethod(event.target.value as SnowflakeAuthMethod)}
            className="w-full rounded-[--radius-control] border border-line bg-canvas px-2.5 py-2 text-body text-ink focus:border-line-ink focus:outline-none"
          >
            <option value="key_pair">Key pair · runs unattended</option>
            <option value="mfa_totp">Password + authenticator code</option>
            <option value="mfa_push">Password + MFA push</option>
            <option value="password">Programmatic token or password</option>
            <option value="external_browser">Corporate browser SSO (SAML)</option>
          </select>
        </label>
        {authMethod !== "external_browser" && authMethod !== "key_pair" && (
          <CredentialInput label={authMethod === "password" ? "Password or token" : "Password"} value={password} onChange={setPassword} placeholder="Entered securely" type="password" autoComplete="current-password" />
        )}
        {authMethod === "mfa_totp" && (
          <CredentialInput
            label="Authenticator code · current 6-digit code"
            value={passcode}
            onChange={(value) => setPasscode(value.replace(/\D/g, "").slice(0, 6))}
            placeholder="123456"
            autoComplete="one-time-code"
            inputMode="numeric"
            maxLength={6}
          />
        )}
        {authMethod === "key_pair" && (
          <>
            <CredentialInput
              label="Private key file · path on the engine host"
              value={privateKeyFile}
              onChange={setPrivateKeyFile}
              placeholder="/etc/atlas/snowflake_key.p8"
            />
            <CredentialInput
              label="Key passphrase · optional"
              value={privateKeyPwd}
              onChange={setPrivateKeyPwd}
              placeholder="Leave empty if unencrypted"
              type="password"
            />
          </>
        )}
        <CredentialInput label="Role" value={role} onChange={setRole} placeholder="ATLAS_READER" />
      </div>
      <p className="mt-2 text-meta text-ink-3">
        {authMethod === "key_pair"
          ? "The key stays on the engine host and is read at connect time — nothing is uploaded. The only method that survives a background job, because it needs no one present."
          : authMethod === "mfa_totp"
          ? "The code is used once and never saved. Extraction and analysis run later with no one present, so they depend on Snowflake's MFA token cache — which the account must allow, and which expires."
          : authMethod === "mfa_push"
            ? "Saving sends an MFA approval request. Approve it while Atlas waits for Snowflake."
            : authMethod === "external_browser"
            ? "Use this only when the account has Okta, Entra, or another SAML provider configured."
            : "Atlas builds and encodes the connection URL on the engine. The credential is never returned to this screen."}
      </p>
      <div className="mt-2 flex items-center gap-2">
        <button
          type="submit"
          disabled={
            saving ||
            !accountIdentifier ||
            !username ||
            !warehouse ||
            !role ||
            (authMethod !== "external_browser" && authMethod !== "key_pair" && !password) ||
            (authMethod === "key_pair" && !privateKeyFile) ||
            (authMethod === "mfa_totp" && !/^\d{6}$/.test(passcode))
          }
          className="rounded-[--radius-control] bg-cta px-3 py-1.5 text-body font-medium text-cta-ink hover:bg-cta-hover disabled:bg-raised disabled:text-ink-4"
        >
          {saving
            ? authMethod === "mfa_push" || authMethod === "mfa_totp"
              ? "Waiting for approval…"
              : authMethod === "external_browser"
                ? "Waiting for browser…"
                : "Connecting…"
            : "Save and test"}
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
      </div>
      {(saveError || (result && result.state !== "connected")) && (
        <p className="mt-2 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {saveError ?? result?.detail}
        </p>
      )}
    </form>
  );
}

function CredentialInput({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  autoComplete,
  inputMode,
  maxLength,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  type?: string;
  autoComplete?: string;
  inputMode?: React.HTMLAttributes<HTMLInputElement>["inputMode"];
  maxLength?: number;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-meta font-semibold uppercase tracking-wide text-ink-3">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        inputMode={inputMode}
        maxLength={maxLength}
        required
        className="w-full rounded-[--radius-control] border border-line bg-canvas px-2.5 py-2 text-body text-ink placeholder:text-ink-4 focus:border-line-ink focus:outline-none"
      />
    </label>
  );
}

function UrlCredentialField({ source }: { source: SourceStatus }) {
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
