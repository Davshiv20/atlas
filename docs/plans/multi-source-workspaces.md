# Implementation record: Safe workspaces for multiple data sources

Status: **implemented and validated in August 2026**. The risk analysis and steps below are retained as the design record; descriptions of the previous system refer to the state before this work landed.

## Goal

Make PostgreSQL, Snowflake, and future data sources coexist safely inside one trusted Atlas installation. Each source must retain its own snapshot, claims, evidence, questions, reviews, jobs, and connection lifecycle without overwriting or mixing another source's state.

This is **not** a multi-tenant plan. Atlas will continue to run as one trusted installation for one team. Authentication, mutually untrusted customer organizations, tenant-aware storage, RBAC, external secret managers, shared job infrastructure, and multi-instance deployment are out of scope.

## Pre-implementation understanding

### What already worked

- PostgreSQL and Snowflake can coexist as different entries in `sources.yaml`.
- The console conventionally extracts a source into a workspace with the same name as the source.
- Each workspace writes under `ATLAS_OUTPUT_DIR/<workspace>/`.
- A snapshot records `source_id`, and analysis resolves the matching adapter from that source's connection URL.
- There is no global active database in the engine. Adding Snowflake does not replace PostgreSQL when source and workspace names remain distinct.

### Risks this work addressed

- Source/workspace binding is only a UI convention. API and CLI connection overrides can extract or analyze a different datastore through an existing workspace.
- Replacing `snapshot.yaml` does not atomically invalidate or replace facts, evidence, questions, output, and human reviews created from the previous snapshot.
- Extraction can race analysis. An old analysis can continue writing semantic state after a new snapshot is published.
- Workspace names, source IDs, and job workspace strings are independent values with no explicit manifest tying them together.
- Deleting a source leaves its workspace data behind but breaks future live operations without clearly describing the cached/offline state.
- The console's **Open** action assumes `workspace === source.id`, even when no snapshot exists.

## Scope

### In Scope

- Explicit workspace creation and source binding.
- One immutable source binding per workspace.
- Structured workspace summaries instead of bare workspace-name strings.
- Removal of per-run source URL and namespace overrides for managed workspaces.
- Safe snapshot refresh behavior and semantic-state invalidation.
- Per-workspace job exclusion and generation checks.
- Clear source deletion, credential removal, cached snapshot, and offline behavior.
- Console management for multiple PostgreSQL and Snowflake workspaces.
- Backward-compatible migration for existing file-backed workspaces.
- Tests proving state cannot cross between sources.

### Out of Scope

- Multiple tenants or mutually untrusted organizations.
- Authentication, authorization, RBAC, invitations, or audit infrastructure.
- Moving metadata from YAML/SQLite to PostgreSQL.
- External secret managers.
- Multi-instance workers or shared queues.
- Cross-database joins, federated SQL, or one semantic graph spanning several sources.
- Changing the PostgreSQL or Snowflake adapter contracts.

## Implemented model

```text
Source
  ├── adapter + namespace
  ├── credential reference
  └── connection health

Workspace
  ├── immutable source_id
  ├── active snapshot generation
  ├── snapshot
  ├── claims + evidence + questions + reviews
  └── jobs
```

A source owns connection configuration. A workspace owns analysis state. The first implementation enforces one workspace per source and one immutable source per workspace. Supporting several objective-specific workspaces for one source can be designed later if needed.

### Workspace manifest

Each workspace directory gets a versioned `workspace.yaml` manifest:

```yaml
schema_version: 1
id: trellis
source_id: trellis-source
incarnation_id: 4fc8e67d7bd34dfab45cd9da280e1a23
created_at: 2026-08-11T00:00:00Z
snapshot_generation: 3
```

The manifest is the authority for source binding. `snapshot.source_id` is repeated provenance and must match it.

### Required invariants

1. A workspace's `source_id` cannot change.
2. Extraction and analysis resolve adapter, URL, and namespace only from the bound source.
3. Managed workspace routes do not accept direct URLs, alternate source IDs, or namespace overrides.
4. Snapshot source and generation must match the workspace manifest before semantic state is read or written.
5. Only one extraction or analysis job may mutate a workspace at a time.
6. A job captures the source ID, immutable workspace incarnation, and snapshot generation it started from; stale writers cannot publish into a refreshed or deleted/recreated workspace.
7. Refresh publishes a validated snapshot atomically.
8. Semantic state from an older generation is never shown as current.
9. Removing credentials leaves cached workspace output readable and marks live operations unavailable.
10. Removing a referenced source is blocked until its workspaces are explicitly removed or archived.
11. PostgreSQL and Snowflake failures remain isolated to their own sources/workspaces.

## Implementation steps

### 1. Add explicit workspace manifests

- Add a `WorkspaceManifest` model with schema version, workspace ID, immutable source ID, creation time, and snapshot generation.
- Add manifest read/write helpers to `Workspace`.
- Add `POST /workspaces` to create a bound workspace.
- Change `GET /workspaces` to return structured summaries containing workspace ID, source ID, adapter, namespace, snapshot availability, snapshot time, and source health/configuration state.
- Migrate existing workspaces lazily:
  - if `snapshot.source_id` exists and the source still exists, create the manifest;
  - if provenance is missing or ambiguous, refuse to guess and return an operator-actionable error.

### 2. Enforce immutable source binding

- Remove `source_id`, `database_url`, and `schema_name` from managed extraction requests.
- Resolve the source from `workspace.manifest.source_id`.
- Remove `database_url` from managed analysis requests.
- Disable CLI `--url` and `--schema` override paths for manifest-managed workspaces. Keep legacy import behavior separate and explicit if still required.
- Validate manifest source ID against snapshot source ID before analysis, compile, output, semantic-view, question, claim, and review operations.
- Return `409 Conflict` when a caller attempts to rebind or use mismatched provenance.

### 3. Prevent concurrent and stale workspace writes

- Add per-workspace exclusive job submission in `JobRegistry` for extraction and analysis.
- Use an interprocess file lock shared by API jobs, synchronous reviews/compile operations, and CLI mutations.
- Atomically check for an active workspace job and insert the next job under one registry lock.
- Reject a second mutation with `409` and identify the active job safely.
- Capture workspace snapshot generation when each job starts.
- Before every semantic publish, verify that the generation is unchanged.
- Stage snapshot writes and atomically replace the active snapshot only after extraction/profile succeeds.
- Add race tests for extraction versus extraction and extraction versus analysis.

### 4. Define safe refresh behavior

- Initial extraction publishes generation 1.
- Refresh increments the generation only after the staged snapshot is valid.
- Until full drift handling exists, refresh requires an explicit `reset_semantics=true` when semantic state already exists.
- A confirmed reset stages a complete new generation without prior facts, evidence, questions, or output; the manifest pointer advances only after staging succeeds, and the previous generation remains recoverable.
- Never silently preserve reviewed claims against a replacement snapshot.
- Return a clear conflict when refresh would discard semantic work without confirmation.

### 5. Define source and workspace lifecycle

- Keep credential removal separate from workspace deletion.
- A workspace with a snapshot remains readable when its source is offline or credentials are missing.
- Live extraction/analysis buttons are disabled when the source is unavailable.
- Block source deletion while a workspace manifest references it and return the referencing workspace IDs.
- Add explicit workspace deletion with confirmation; it removes only that workspace's derived state and jobs, never another source.
- Keep source health and snapshot availability as separate status fields.

### 6. Update the console

- Separate connection cards from workspace selection.
- A connected source can create a workspace or open an existing bound workspace.
- Stop dispatching `selectWorkspace(source.id)` as an implicit binding.
- Use structured workspace summaries and stable workspace IDs throughout RTK Query and Redux selection.
- Show adapter, namespace, source label, snapshot freshness, and cached/offline state in the workspace picker.
- Make **Read schema** create/extract the selected workspace explicitly.
- Make refresh destructive behavior explicit and confirmed.
- Ensure switching between PostgreSQL and Snowflake clears table selection and displays only the selected workspace's data/jobs.

### 7. Preserve file-backed deployment constraints

- Keep `sources.yaml`, `.secrets.env`, workspace YAML, and `jobs.db` for this scope.
- Continue requiring one Atlas instance and one trusted operator.
- Keep source credentials read-only and out of API responses/logs.
- Document backup/restore for `/data` and the new workspace manifests.

## API Direction

```text
POST /workspaces
  { "id": "trellis-review", "source_id": "trellis-source" }

GET /workspaces
  {
    "workspaces": [
      {
        "id": "trellis-review",
        "source_id": "trellis-source",
        "adapter": "snowflake",
        "namespace": "POC_DB.TRELLIS_SOURCE",
        "snapshot_generation": 1,
        "snapshot_available": true,
        "source_available": true
      }
    ]
  }

POST /workspaces/{workspace_id}/extract
  { "profile": true, "reset_semantics": false }

POST /workspaces/{workspace_id}/analyze
  { "limit": null, "tables": null, "regenerate": false }
```

The engine, not the request body, determines the connection from the workspace's bound source.

## Files / Areas Likely Involved

- `engine/src/atlas/workspace.py`
- `engine/src/atlas/sources.py`
- `engine/src/atlas/snapshot.py`
- `engine/src/atlas/jobs.py`
- `engine/src/atlas/api.py`
- `engine/src/atlas/cli.py`
- `engine/tests/test_api.py`
- `engine/tests/test_sources.py`
- new focused workspace-management tests
- `console/src/api/types.ts`
- `console/src/store/api.ts`
- `console/src/store/uiSlice.ts`
- `console/src/App.tsx`
- `console/src/components/Sources.tsx`
- `console/src/components/WorkspacePicker.tsx`
- README and engine/console documentation

## Edge Cases

- PostgreSQL and Snowflake sources have similar display labels.
- A legacy workspace has no `source_id`.
- A workspace references a source that was manually removed from `sources.yaml`.
- Credentials are forgotten while cached output remains.
- A refresh fails after staging but before publication.
- Extraction and analysis are requested simultaneously.
- The engine restarts during a staged refresh.
- A stale analysis worker attempts to write after generation changed.
- Refresh is requested after human reviews exist.
- A source is deleted while a job is active.
- Workspace deletion is requested while a job is active.
- A workspace name contains traversal characters.

## Testing Plan

### Unit tests

- Manifest validation and atomic writes.
- Immutable source binding.
- Legacy manifest migration from snapshot provenance.
- Missing/ambiguous provenance refusal.
- Snapshot generation checks.
- Exclusive workspace job submission.

### API integration tests

- PostgreSQL and Snowflake workspaces coexist with independent snapshots and outputs.
- A workspace bound to PostgreSQL cannot extract or analyze Snowflake.
- Direct URL/source/namespace overrides are rejected or absent from the contract.
- Re-extraction cannot leave old semantic state appearing current.
- Refresh with semantic state requires explicit reset confirmation.
- Source deletion is blocked while referenced.
- Credential removal preserves cached reads but blocks live operations.
- Workspace deletion affects only the selected workspace.
- A second active mutation receives `409`.

### Console checks

- Create one PostgreSQL workspace and one Snowflake workspace.
- Extract both and switch repeatedly between them.
- Verify tables, questions, claims, reviews, and jobs never cross.
- Disable Snowflake credentials and confirm PostgreSQL remains operational.
- Confirm Snowflake cached output remains clearly available offline.

### Regression checks

- Existing PostgreSQL and Snowflake adapter tests remain green.
- Full engine tests and lint pass.
- Console typecheck and production build pass.
- OpenAPI schema is regenerated.
- `git diff --check` passes.

## Rollout / Safety Notes

- Migrate existing workspaces before enabling the new console flow.
- Back up `/data` before manifest migration.
- Keep a schema version in every manifest for future migrations.
- Do not auto-bind a legacy workspace when provenance is ambiguous.
- Keep one-instance deployment enforcement unchanged.
- Roll back by restoring the `/data` backup and previous image; manifests are additive until refresh/reset is used.

## Final Checklist

- [x] Existing PostgreSQL + Snowflake behavior understood
- [x] Multi-tenant scope explicitly excluded
- [x] Workspace/source ownership identified
- [x] Contamination paths identified
- [x] Concurrency and refresh behavior defined
- [x] Edge cases covered
- [x] Tests planned
- [x] Workspace manifest and API implemented
- [x] Console workspace management implemented
- [x] Full validation green
