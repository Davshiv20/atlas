# console

The review workbench: the interface where a human validates generated claims.
Structure only — no components yet, and **no framework chosen**.

## Why the framework is not chosen here

It is a real decision with real consequences for this app, and it belongs to
whoever will maintain it. The shape of the work, to inform the choice:

- Mostly a **queue UI** — a prioritized list, keyboard-driven approve / reject /
  edit, evidence shown beside each claim. Throughput for the reviewer is the
  product metric, so keyboard handling and optimistic updates matter more than
  routing or SEO.
- **Long-running jobs** — analysis is minutes per table. Needs polling or SSE
  against `GET /jobs/{id}`, and a UI that survives a refresh mid-run.
- **No public surface** — internal tool behind auth. SSR and SEO are irrelevant.

That profile points at a client-rendered SPA (Vite + React or Svelte) over a
meta-framework. Not a decision made here.

## Layout

```
src/
├── api/          generated client + types from the engine's OpenAPI schema
├── routes/       one per surface: workspaces, catalogue, review queue, jobs
└── components/   shared UI
```

## Types come from the engine

Do not hand-write request or response types. The engine's OpenAPI schema is the
contract:

```bash
make types    # from the repo root
```

Then generate TypeScript from `src/api/openapi.json` with `openapi-typescript`
(or the equivalent for the chosen stack). Hand-written types drift silently; the
first symptom is a reviewer approving a claim that never saved.

## The surfaces that matter

1. **Review queue** — least-confident claims first, evidence and any
   contradicting result shown inline. This is the product.
2. **Catalogue** — browse `GET /workspaces/{name}/output`.
3. **Questions** — answer what no query can settle.
4. **Jobs** — progress for extract and analyze runs.

One API behaviour to design around: verifying an ungrounded claim returns
**409**, not 200. That is deliberate — it is the rule that stops a confident
guess being promoted by a distracted reviewer. Surface the message; do not
retry.
