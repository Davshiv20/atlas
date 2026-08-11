import { useState } from "react";

import type { SnowflakeAuthMethod, SnowflakeCredentials } from "@/api/types";

/**
 * The connection form. Shared by setup and the sources screen so there is one
 * implementation of what a source is.
 *
 * PostgreSQL can reference an operator-managed URL. Snowflake accepts normal
 * credential fields and lets the engine construct and encode the URL; users
 * should never have to learn connection-string escaping rules.
 */
export interface SourceDraft {
  id: string;
  adapter: string;
  url_env: string;
  namespace: string;
  label?: string;
  snowflake_credentials?: SnowflakeCredentials;
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
  const [accountIdentifier, setAccountIdentifier] = useState("");
  const [username, setUsername] = useState("");
  const [authMethod, setAuthMethod] = useState<SnowflakeAuthMethod>("mfa_totp");
  const [password, setPassword] = useState("");
  const [passcode, setPasscode] = useState("");
  const [warehouse, setWarehouse] = useState("");
  const [role, setRole] = useState("ATLAS_READER");

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
          ...(snowflake
            ? {
                snowflake_credentials: {
                  account_identifier: accountIdentifier,
                  username,
                  auth_method: authMethod,
                  ...(authMethod !== "external_browser" ? { password } : {}),
                  ...(authMethod === "mfa_totp" ? { passcode } : {}),
                  warehouse,
                  role,
                },
              }
            : {}),
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

      {snowflake && (
        <SnowflakeGuide
          onApply={({
            id: detectedId,
            namespace: detectedNamespace,
            accountIdentifier: detectedAccount,
          }) => {
            if (!id) setId(detectedId);
            setNamespace(detectedNamespace);
            setAccountIdentifier(detectedAccount);
          }}
        />
      )}

      {snowflake && (
        <div className="mt-4 grid gap-4 rounded-[--radius-panel] border border-line bg-raised p-3 sm:grid-cols-2">
          <Field label="Account identifier" hint="organization-account">
            <input
              value={accountIdentifier}
              onChange={(event) => setAccountIdentifier(event.target.value)}
              placeholder="myorg-myaccount"
              required
              className={INPUT}
            />
          </Field>
          <Field label="Warehouse">
            <input
              value={warehouse}
              onChange={(event) => setWarehouse(event.target.value)}
              placeholder="POC_WH"
              required
              className={INPUT}
            />
          </Field>
          <Field label="Username">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="SHIVAM"
              autoComplete="username"
              required
              className={INPUT}
            />
          </Field>
          <Field label="Sign-in method">
            <select
              value={authMethod}
              onChange={(event) => setAuthMethod(event.target.value as SnowflakeAuthMethod)}
              className={INPUT}
            >
              <option value="mfa_totp">Password + authenticator code</option>
              <option value="mfa_push">Password + MFA push</option>
              <option value="password">Programmatic token or password</option>
              <option value="external_browser">Corporate browser SSO (SAML)</option>
            </select>
          </Field>
          {authMethod !== "external_browser" && (
            <Field label={authMethod === "password" ? "Password or token" : "Password"}>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="Entered securely"
                autoComplete="current-password"
                required
                className={INPUT}
              />
            </Field>
          )}
          {authMethod === "mfa_totp" && (
            <Field label="Authenticator code" hint="current 6-digit code">
              <input
                value={passcode}
                onChange={(event) =>
                  setPasscode(event.target.value.replace(/\D/g, "").slice(0, 6))
                }
                placeholder="123456"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                pattern="[0-9]{6}"
                required
                className={`${INPUT} font-mono`}
              />
            </Field>
          )}
          <Field label="Role">
            <input
              value={role}
              onChange={(event) => setRole(event.target.value)}
              placeholder="ATLAS_READER"
              required
              className={INPUT}
            />
          </Field>
          <p className="self-end pb-1 text-meta text-ink-3">
            {authMethod === "mfa_totp"
              ? "The code is used once for this connection and is never saved. Atlas requests Snowflake's MFA token cache for later local connections."
              : authMethod === "mfa_push"
                ? "Saving sends an MFA approval request. Approve it while Atlas waits for Snowflake."
                : authMethod === "external_browser"
                ? "For accounts configured with Okta, Entra, or another SAML provider. Snowflake opens that provider in your browser."
                : "Atlas encodes the credential on the engine. The saved secret is never returned to the browser."}
          </p>
        </div>
      )}

      <details className="mt-4 rounded-[--radius-control] border border-line bg-raised p-3">
        <summary className="cursor-pointer text-meta font-semibold text-ink-2">
          Advanced: secret environment variable
        </summary>
        <div className="mt-3">
          <Field label="Environment variable" hint="optional override">
            <input
              value={effectiveEnv}
              onChange={(e) => setUrlEnv(e.target.value.toUpperCase())}
              placeholder="ELARA_DATABASE_URL"
              required
              className={`${INPUT} font-mono`}
            />
          </Field>
          <p className="mt-2 text-meta text-ink-3">
            Atlas generates this name automatically. Change it only when an operator already
            supplies the connection URL through a specific environment variable.
          </p>
        </div>
      </details>

      {error && (
        <p className="mt-3 rounded-[--radius-control] border border-red/25 bg-red-soft px-3 py-2 text-body text-red">
          {error}
        </p>
      )}

      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={
            busy ||
            !id ||
            !effectiveEnv ||
            (snowflake &&
              (!accountIdentifier ||
                !username ||
                !warehouse ||
                !role ||
                (authMethod !== "external_browser" && !password) ||
                (authMethod === "mfa_totp" && !/^\d{6}$/.test(passcode))))
          }
          className="rounded-[--radius-control] bg-cta px-4 py-2 text-body font-medium text-cta-ink transition-colors hover:bg-cta-hover disabled:cursor-not-allowed disabled:bg-raised disabled:text-ink-4"
        >
          {busy
            ? snowflake && (authMethod === "mfa_push" || authMethod === "mfa_totp")
              ? "Waiting for approval…"
              : snowflake && authMethod === "external_browser"
                ? "Waiting for browser…"
                : "Saving…"
            : submitLabel}
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

interface SnowflakeLocation {
  organization: string;
  account: string;
  database: string;
  schema: string;
}

function SnowflakeGuide({
  onApply,
}: {
  onApply: (value: { id: string; namespace: string; accountIdentifier: string }) => void;
}) {
  const [appUrl, setAppUrl] = useState("");
  const location = parseSnowflakeUrl(appUrl);
  const accountIdentifier = location
    ? `${location.organization}-${location.account}`
    : "ORGANIZATION-ACCOUNT";
  const database = location?.database ?? "DATABASE";
  const schema = location?.schema ?? "SCHEMA";
  const namespace = `${database}.${schema}`;
  const grants = `USE ROLE SECURITYADMIN;
CREATE ROLE IF NOT EXISTS ATLAS_READER;
GRANT USAGE ON WAREHOUSE YOUR_WAREHOUSE TO ROLE ATLAS_READER;
GRANT USAGE ON DATABASE ${database} TO ROLE ATLAS_READER;
GRANT USAGE ON SCHEMA ${namespace} TO ROLE ATLAS_READER;
GRANT SELECT, REFERENCES ON ALL TABLES IN SCHEMA ${namespace} TO ROLE ATLAS_READER;
GRANT SELECT ON ALL VIEWS IN SCHEMA ${namespace} TO ROLE ATLAS_READER;
GRANT SELECT, REFERENCES ON FUTURE TABLES IN SCHEMA ${namespace} TO ROLE ATLAS_READER;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA ${namespace} TO ROLE ATLAS_READER;
GRANT ROLE ATLAS_READER TO USER YOUR_USER;`;

  return (
    <section className="mt-4 rounded-[--radius-panel] border border-line bg-canvas p-3">
      <h3 className="text-body font-semibold text-ink">Snowflake setup guide</h3>
      <p className="mt-1 text-meta text-ink-3">
        Paste the Snowflake page URL you are looking at. Atlas will derive the account,
        database, and schema; no credential is read from this URL.
      </p>

      <label className="mt-3 block">
        <span className="text-meta font-semibold text-ink-3">Snowsight page URL</span>
        <input
          value={appUrl}
          onChange={(event) => setAppUrl(event.target.value.trim())}
          placeholder="https://app.snowflake.com/org/account/#/data/databases/DB/schemas/SCHEMA"
          className={`${INPUT} mt-1 font-mono text-ident`}
        />
      </label>

      {appUrl && !location && (
        <p className="mt-2 text-meta text-red">
          Atlas could not read this URL. Open the schema in Snowflake and copy the full browser URL.
        </p>
      )}

      {location && (
        <div className="mt-3 border-t border-line pt-3">
          <dl className="grid grid-cols-[130px_minmax(0,1fr)] gap-x-3 gap-y-1 text-meta">
            <dt className="text-ink-3">Account identifier</dt>
            <dd className="ident text-ink">{accountIdentifier}</dd>
            <dt className="text-ink-3">Database</dt>
            <dd className="ident text-ink">{database}</dd>
            <dt className="text-ink-3">Schema</dt>
            <dd className="ident text-ink">{schema}</dd>
          </dl>
          <button
            type="button"
            onClick={() =>
              onApply({
                id: schema.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""),
                namespace,
                accountIdentifier,
              })
            }
            className="mt-3 rounded-[--radius-control] border border-line-strong bg-surface px-3 py-1.5 text-meta font-medium text-ink hover:bg-raised"
          >
            Use {namespace} in this form
          </button>
        </div>
      )}

      <details className="mt-3 border-t border-line pt-2">
        <summary className="cursor-pointer text-meta font-semibold text-ink-2">
          1. Ask a Snowflake admin for read-only access
        </summary>
        <p className="mt-2 text-meta text-ink-3">
          In a worksheet, run <code className="ident">SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE();</code>{" "}
          to see your current values. Then ask an administrator to replace{" "}
          <span className="ident">YOUR_WAREHOUSE</span> and <span className="ident">YOUR_USER</span>{" "}
          and run the grants below.
        </p>
        <pre className="ident mt-2 overflow-x-auto rounded-[--radius-control] bg-raised p-2 text-ink-2">
          {grants}
        </pre>
      </details>

      <p className="mt-3 border-t border-line pt-2 text-meta text-ink-3">
        <span className="font-semibold text-ink-2">2. Choose how to sign in below.</span>{" "}
        Use password + MFA push for a Snowflake-managed human login. Corporate browser
        SSO requires a configured SAML provider. Use a programmatic token or key pair for
        a deployed connection.
      </p>
    </section>
  );
}

function parseSnowflakeUrl(value: string): SnowflakeLocation | null {
  try {
    const url = new URL(value);
    if (url.hostname !== "app.snowflake.com") return null;
    const [organization, account] = url.pathname.split("/").filter(Boolean);
    const parts = url.hash.split("/").filter(Boolean);
    const databaseAt = parts.indexOf("databases");
    const schemaAt = parts.indexOf("schemas");
    const database = databaseAt >= 0 ? parts[databaseAt + 1] : undefined;
    const schema = schemaAt >= 0 ? parts[schemaAt + 1] : undefined;
    if (!organization || !account || !database || !schema) return null;
    return {
      organization: decodeURIComponent(organization),
      account: decodeURIComponent(account),
      database: decodeURIComponent(database),
      schema: decodeURIComponent(schema),
    };
  } catch {
    return null;
  }
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
