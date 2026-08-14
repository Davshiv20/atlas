import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import type { SourceDraft } from "@/components/SourceForm";
import type {
  EngineConfig,
  Fact,
  Job,
  Question,
  ReviewRequest,
  SchemaOutput,
  SemanticViewResponse,
  SnowflakeCredentials,
  ConnectionHealth,
  SourceStatus,
  WorkspaceSummary,
} from "@/api/types";

/**
 * The engine is proxied at /api in development (see vite.config.ts), so the
 * browser stays same-origin and there is no CORS configuration to get wrong.
 * In the built image there is no proxy, and the engine strips the prefix
 * itself (`ApiPrefixMiddleware`) — so this one base is correct in both.
 */
export const API_BASE = "/api";

export const api = createApi({
  reducerPath: "engine",
  baseQuery: fetchBaseQuery({ baseUrl: API_BASE }),
  tagTypes: ["Workspaces", "Output", "Claims", "Questions", "Job", "Sources"],
  endpoints: (build) => ({
    config: build.query<EngineConfig, void>({
      query: () => "/config",
    }),

    sources: build.query<SourceStatus[], void>({
      query: () => "/sources",
      transformResponse: (r: { sources: SourceStatus[] }) => r.sources,
      providesTags: ["Sources"],
    }),

    createSource: build.mutation<SourceStatus, SourceDraft>({
      query: (body) => ({ url: "/sources", method: "POST", body }),
      invalidatesTags: ["Sources"],
    }),

    deleteSource: build.mutation<void, string>({
      query: (id) => ({ url: `/sources/${encodeURIComponent(id)}`, method: "DELETE" }),
      invalidatesTags: ["Sources"],
    }),

    setCredentials: build.mutation<ConnectionHealth, { id: string; url: string }>({
      query: ({ id, url }) => ({
        url: `/sources/${encodeURIComponent(id)}/credentials`,
        method: "PUT",
        body: { url },
      }),
      invalidatesTags: ["Sources"],
    }),

    setSnowflakeCredentials: build.mutation<
      ConnectionHealth,
      { id: string; credentials: SnowflakeCredentials }
    >({
      query: ({ id, credentials }) => ({
        url: `/sources/${encodeURIComponent(id)}/credentials/snowflake`,
        method: "PUT",
        body: credentials,
      }),
      invalidatesTags: ["Sources"],
    }),

    forgetCredentials: build.mutation<void, string>({
      query: (id) => ({
        url: `/sources/${encodeURIComponent(id)}/credentials`,
        method: "DELETE",
      }),
      invalidatesTags: ["Sources"],
    }),

    testSource: build.mutation<ConnectionHealth, string>({
      query: (id) => ({ url: `/sources/${encodeURIComponent(id)}/test`, method: "POST" }),
      // The listing carries health, so a check must refresh the cards too.
      invalidatesTags: ["Sources"],
    }),

    workspaces: build.query<WorkspaceSummary[], void>({
      query: () => "/workspaces",
      transformResponse: (r: { workspaces: WorkspaceSummary[] }) => r.workspaces,
      providesTags: ["Workspaces"],
    }),

    createWorkspace: build.mutation<WorkspaceSummary, { id: string; source_id: string }>({
      query: (body) => ({ url: "/workspaces", method: "POST", body }),
      invalidatesTags: ["Workspaces"],
    }),

    deleteWorkspace: build.mutation<void, string>({
      query: (id) => ({ url: `/workspaces/${encodeURIComponent(id)}`, method: "DELETE" }),
      invalidatesTags: ["Workspaces", "Output", "Claims", "Questions", "Job"],
    }),

    output: build.query<SchemaOutput, string>({
      query: (workspace) => `/workspaces/${workspace}/output`,
      providesTags: ["Output"],
    }),

    claims: build.query<Fact[], string>({
      query: (workspace) => `/workspaces/${workspace}/claims`,
      transformResponse: (r: { claims: Fact[] }) => r.claims,
      providesTags: ["Claims"],
    }),

    semanticView: build.query<
      SemanticViewResponse,
      { workspace: string; table?: string }
    >({
      query: ({ workspace, table }) =>
        `/workspaces/${workspace}/semantic-view${table ? `?table=${encodeURIComponent(table)}` : ""}`,
      // Reviewing a claim changes what is emitted, so the view goes stale with
      // the claims rather than on its own schedule.
      providesTags: ["Output"],
    }),

    questions: build.query<Question[], string>({
      query: (workspace) => `/workspaces/${workspace}/questions`,
      transformResponse: (r: { questions: Question[] }) => r.questions,
      providesTags: ["Questions"],
    }),

    // Answering writes a claim as well as settling the question, so the
    // catalogue and the claim list both go stale.
    answerQuestion: build.mutation<
      { question: Question; claim: Fact },
      { workspace: string; id: string; answer: string; reviewer: string }
    >({
      query: ({ workspace, id, answer, reviewer }) => ({
        url: `/workspaces/${workspace}/questions/${id}/answer`,
        method: "POST",
        body: { answer, reviewer },
      }),
      invalidatesTags: ["Questions", "Claims", "Output"],
    }),

    dismissQuestion: build.mutation<
      { question: Question },
      { workspace: string; id: string; answer: string; reviewer: string }
    >({
      query: ({ workspace, id, answer, reviewer }) => ({
        url: `/workspaces/${workspace}/questions/${id}/dismiss`,
        method: "POST",
        body: { answer, reviewer },
      }),
      // Nothing is established, so no claim changes.
      invalidatesTags: ["Questions"],
    }),



    review: build.mutation<
      Fact,
      { workspace: string; claimId: string; body: ReviewRequest }
    >({
      query: ({ workspace, claimId, body }) => ({
        // encodeURIComponent is load-bearing: a plural-aspect id contains two
        // '#' characters, which are fragment markers if left raw.
        url: `/workspaces/${workspace}/claims/${encodeURIComponent(claimId)}/review`,
        method: "POST",
        body,
      }),
      // The output document is derived from claims, so both go stale together.
      // Refetching output is what turns an approval teal in the column list.
      invalidatesTags: ["Claims", "Output"],
    }),

    extract: build.mutation<Job, { workspace: string; resetSemantics?: boolean }>({
      query: ({ workspace, resetSemantics }) => ({
        url: `/workspaces/${workspace}/extract`,
        method: "POST",
        body: { reset_semantics: resetSemantics ?? false },
      }),
      invalidatesTags: ["Workspaces", "Output"],
    }),

    analyze: build.mutation<
      Job,
      // `limit: null` means every remaining table; `regenerate` re-does ones
      // that already have claims, which is off by default.
      { workspace: string; limit: number | null; regenerate?: boolean; tables?: string[] }
    >({
      query: ({ workspace, limit, regenerate, tables }) => ({
        url: `/workspaces/${workspace}/analyze`,
        method: "POST",
        body: { limit, regenerate: regenerate ?? false, tables: tables ?? null },
      }),
    }),

    job: build.query<Job, string>({
      query: (jobId) => `/jobs/${jobId}`,
      providesTags: ["Job"],
    }),

    // Every job the engine knows about for this workspace, newest first. The
    // console needs this to find a run it did not start itself — a reload, a
    // second tab, or a run kicked off from the CLI.
    jobs: build.query<Job[], string>({
      query: (workspace) => `/jobs?workspace=${encodeURIComponent(workspace)}`,
      transformResponse: (response: { jobs: Job[] }) => response.jobs,
      providesTags: ["Job"],
    }),
  }),
});

export const {
  useConfigQuery,
  useSourcesQuery,
  useCreateSourceMutation,
  useDeleteSourceMutation,
  useSetCredentialsMutation,
  useSetSnowflakeCredentialsMutation,
  useForgetCredentialsMutation,
  useTestSourceMutation,
  useWorkspacesQuery,
  useCreateWorkspaceMutation,
  useDeleteWorkspaceMutation,
  useOutputQuery,
  useClaimsQuery,
  useQuestionsQuery,
  useSemanticViewQuery,
  useAnswerQuestionMutation,
  useDismissQuestionMutation,
  useReviewMutation,
  useAnalyzeMutation,
  useExtractMutation,
  useJobQuery,
  useJobsQuery,
} = api;

/**
 * Where the semantic view can be fetched or downloaded.
 *
 * Built here rather than in the component so the base path is named once. The
 * export is reached by URL rather than through RTK Query because the point of
 * it is to be a link: a browser download and an agent's `curl` want the same
 * address, and routing it through a cache would give the file a copy of the
 * view rather than the view.
 */
export function exportUrl(
  workspace: string,
  options: { format?: "yaml" | "json"; include?: "ready" | "all"; table?: string } = {},
): string {
  const query = new URLSearchParams({
    format: options.format ?? "yaml",
    include: options.include ?? "ready",
  });
  if (options.table) query.set("table", options.table);
  return `${API_BASE}/workspaces/${encodeURIComponent(workspace)}/export?${query}`;
}
