# Product: Agent Semantic Context Layer

## 1. Product Summary

### One-line description

A living semantic context layer that learns unfamiliar databases, compresses large schemas into a small set of meaningful business concepts, lets humans validate only high-impact uncertainty, and serves trusted task-specific context to AI agents.

### Core promise

Teams building AI agents over customer-owned or unfamiliar databases should not have to manually understand hundreds of tables or expose raw schemas directly to agents.

The product connects to a data source, builds a physical model, generates semantic hypotheses from schema metadata, descriptions, relationships, and privacy-safe sample values, automatically validates what it can, escalates only consequential ambiguity to humans, and publishes an agent-ready semantic layer.

The product is not a one-shot semantic-view generator. It is a continuous system:

```text
Connect → infer → compress → validate → serve → observe → improve
```

## 2. Problem

AI agents that work over enterprise data are often given one of two bad interfaces:

1. Raw database schemas and unrestricted SQL access.
2. Hand-built semantic layers that take significant domain and engineering effort to create and maintain.

Both approaches fail when the team building the AI product does not own the underlying database.

Typical conditions:

- The database belongs to a customer or another internal team.
- There may be hundreds of tables and thousands of columns.
- Naming is inconsistent or legacy-heavy.
- Table and column descriptions may be missing or stale.
- Important business logic is implicit rather than encoded in the schema.
- Multiple fields may represent similar concepts.
- The AI team cannot spend weeks reverse-engineering the domain.
- Human reviewers cannot realistically validate every table or column.
- The schema can change after the semantic layer is created.

The raw database contains facts, but not necessarily the business meaning an AI agent needs in order to act accurately.

## 3. Product Thesis

Humans should not validate schemas field-by-field.

The system should automatically reduce a large database into a much smaller set of consequential semantic decisions.

Example:

```text
5,000 columns
   ↓
800 tables
   ↓
automatic clustering + profiling
   ↓
40 relevant tables for one agent workflow
   ↓
8 domain entities
   ↓
12 business concepts
   ↓
15 high-impact unresolved questions
```

The product succeeds when it can turn an unfamiliar database into a small, reviewable semantic surface that an AI agent can safely consume.

The key principle is:

> Humans validate business pivots, not database metadata.

## 4. Target User

### Primary user

AI engineers and product teams building agents over databases they do not own.

Examples:

- Vertical AI companies integrating with customer operational systems.
- AI consultancies building customer-specific agents.
- Enterprise AI teams integrating with legacy internal databases.
- SaaS vendors supporting customer-hosted or bring-your-own-data environments.

### Initial persona

An AI engineer receives read-only access to a customer database and needs to make an agent accurately answer questions about a bounded business workflow.

Examples:

- collections agent
- customer-success agent
- finance copilot
- operations agent
- claims agent
- logistics assistant

## 5. Job To Be Done

### Primary JTBD

Given an unfamiliar database and an agent objective, help me determine:

- which tables matter;
- what they likely mean;
- how they relate;
- which fields are authoritative;
- which semantics are safe to infer;
- which assumptions need human confirmation;
- what context an AI agent should receive for a specific task.

### Example agent objective

```text
The agent should answer questions about customers, invoices, payments,
and outstanding balances.
```

The system should not model the entire database equally. It should use this objective to find and model the relevant semantic subgraph.

## 6. Inputs

The initial system should work primarily from database metadata plus lightweight profiling.

### 6.1 Structural metadata

Collect:

- databases / schemas / namespaces
- tables and views
- columns and data types
- primary keys
- foreign keys
- unique constraints
- nullability
- check constraints
- indexes
- default values
- database comments
- view definitions where available

### 6.2 Existing descriptions

Collect descriptions from:

- database comments
- dbt metadata
- ORM metadata
- API docs
- catalog exports
- manually supplied notes
- optional domain documentation

Descriptions should retain their source and should not automatically be treated as authoritative.

### 6.3 Privacy-safe sample values

Collect representative values and local statistics such as:

- common values
- approximate distinct count
- null percentage
- uniqueness
- min / max
- timestamp range
- row count
- enum candidates
- join coverage
- masked samples

Sensitive fields should be detected and masked before any external model sees them.

### 6.4 Agent objective

A required input that describes what the consuming agent needs to do.

This is used to prune irrelevant areas of the schema and prioritize review.

## 7. Canonical Internal Model

Every source adapter should normalize its input into a source-independent physical representation.

```typescript
interface DataSourceSnapshot {
  namespaces: Namespace[];
  objects: DataObject[];
  fields: Field[];
  relationships: Relationship[];
  constraints: Constraint[];
  profiles: FieldProfile[];
}
```

Examples:

```text
Postgres
schema → table → column

Snowflake
database → schema → table/view → column

REST API
service → endpoint → response object → field
```

Everything after ingestion should operate on the canonical model rather than source-specific metadata.

## 8. Product Model: Three Layers

### 8.1 Physical Source Model

What actually exists.

Example:

```text
billing.invoice_hdr
billing.payment_txn
crm.customer_master
```

Contains observed facts only:

- names
- types
- constraints
- values
- cardinality
- physical relationships

### 8.2 Semantic Model

What the business structures probably mean.

Example:

```text
Customer
Invoice
Payment
Outstanding Balance
```

Contains:

- entities
- semantic field names
- grain
- relationship meanings
- candidate business concepts
- agent usage guidance
- validation state

### 8.3 Task Context View

The minimal relevant projection for a specific agent question.

Example question:

> Which enterprise customers have unpaid invoices older than 60 days?

The returned context may contain only:

- Customer
- Invoice
- PaymentAllocation
- relevant relationships
- overdue-date guidance
- outstanding-balance definition
- unresolved ambiguity around partially paid invoices

This task-specific context view is what the agent actually consumes.

## 9. Semantic Claims

The central unit of the product should be a semantic claim.

A semantic claim is an explicit interpretation derived from evidence.

```json
{
  "claim_type": "table_grain",
  "subject": "billing.invoice_hdr",
  "proposal": "One row represents one customer invoice.",
  "confidence": 0.91,
  "evidence": [
    "invoice_id is unique",
    "invoice_id is the primary key",
    "table contains issue_date, due_date and total_amount"
  ],
  "status": "inferred"
}
```

### Claim types

- table purpose
- semantic entity
- table grain
- field meaning
- canonical identifier
- relationship meaning
- enum mapping
- lifecycle interpretation
- metric/business-rule definition
- sensitive-data classification
- agent usage guidance
- authoritative-field selection

Claims should be atomic, reviewable, versionable, and attributable.

## 10. Trust States

Avoid a simple approved / unapproved model.

### Observed

Deterministically true from the source.

Examples:

- PK / FK
- data type
- nullability
- cardinality
- observed values
- uniqueness

No human review required.

### Inferred

A semantic interpretation with supporting evidence.

Example:

> `invoice.currency_code` probably represents transaction currency.

Usable in low-risk contexts when confidence is high.

### Validated

A business-relevant interpretation confirmed by a reviewer or strong authoritative evidence.

Example:

> `invoice.balance_due` is the authoritative outstanding-balance field.

### Unresolved

The system knows it does not know.

Example:

> It is unclear whether paused subscriptions count as active.

### Stale

A previously validated claim may no longer hold because the source changed.

### Conflicted

Two pieces of evidence or two approved interpretations disagree.

## 11. Semantic Compression

The core technical challenge is not description generation. It is semantic compression.

The system should reduce a large physical schema into a much smaller semantic surface for a given agent objective.

Pipeline:

```text
Large schema
   ↓
structural analysis
   ↓
domain clustering
   ↓
agent-task relevance retrieval
   ↓
relevant source subgraph
   ↓
entity + grain inference
   ↓
semantic hypotheses
   ↓
contradiction testing
   ↓
ranked unresolved decisions
```

The system should optimize for the smallest number of human decisions required to make the agent workflow reliable.

## 12. Automated Validation

The system should actively test its own semantic hypotheses before escalating them to a human.

Example hypothesis:

> `invoice.paid_at` represents the date the invoice became fully paid.

Possible automatic checks:

- Are there records with `paid_at != NULL` but non-paid status?
- Does `paid_at` correspond to the final payment-allocation timestamp?
- Are refunded invoices still populated?
- Are there partially paid invoices with `paid_at` set?

The result can raise or lower confidence.

The goal is not just to generate semantics, but to attempt to falsify them.

## 13. Human Review Model

Humans should review only high-impact ambiguity.

### Review priority

A claim's priority can be based on:

```text
uncertainty
× downstream impact
× task relevance
× agent usage frequency
× consequence of being wrong
```

### Review budget

The product should eventually support a review budget such as:

```text
Review budget: 30 minutes
```

The system then selects the highest-value unresolved decisions that can be reviewed within that time.

### Better review questions

Avoid:

> Please validate these 120 columns.

Prefer:

> Which field should be treated as the authoritative invoice status?

or:

> Do partially paid invoices count as unpaid for collections workflows?

One answer should resolve or constrain many downstream claims.

### Representative-case review

Instead of asking for abstract definitions, show concrete examples.

```text
Subscription A → system classifies ACTIVE
Subscription B → system classifies ACTIVE
Subscription C → system classifies INACTIVE

Do these classifications match the business meaning of “active subscription”?
```

This makes semantic validation faster and more reliable.

## 14. Review Workbench

The primary product UI should be a semantic review workbench, not an ER diagram.

### Main surfaces

#### Relevant domain map

Shows only entities and source objects relevant to the agent objective.

#### Claim review queue

Ranked by impact and uncertainty.

Each item should show:

- proposal
- confidence
- observed evidence
- inferred evidence
- contradictions
- sample values
- source descriptions
- affected concepts
- affected agent tasks/tools
- approve / edit / reject / unresolved

#### Entity view

Shows:

- semantic description
- physical source mappings
- grain
- identifiers
- relationships
- trust status
- unresolved assumptions

#### Agent preview

Shows how an agent would interpret a sample question using the current semantic layer.

## 15. Agent Context Compiler

The product should not dump the full semantic layer into an LLM context window.

It should compile the smallest sufficient context for each question.

### Input

```text
user question
+
agent identity
+
agent objective
+
permissions
+
validated semantic graph
```

### Compiler

```text
intent extraction
   ↓
entity retrieval
   ↓
relationship closure
   ↓
business-rule retrieval
   ↓
trust filtering
   ↓
permission filtering
   ↓
token-budget optimization
```

### Output

A minimal, trusted context package containing only what the agent needs.

Example:

```json
{
  "entities": ["Customer", "Invoice", "PaymentAllocation"],
  "relationships": [
    "Invoice.customer_id -> Customer.id",
    "PaymentAllocation.invoice_id -> Invoice.id"
  ],
  "guidance": [
    "Use due_date, not created_at, to calculate overdue age.",
    "Subtract allocated payments when calculating outstanding balance."
  ],
  "warnings": [
    "The treatment of partially paid invoices is not yet validated."
  ]
}
```

## 16. Agent-Facing Interfaces

Exports are useful, but the product should become a runtime service rather than only producing files.

### Initial interfaces

- `search_schema(query)`
- `describe_entity(entity)`
- `describe_field(entity, field)`
- `find_relationship_path(source, target)`
- `get_context_for_question(question)`

### Later interfaces

- MCP
- REST API
- Python SDK
- TypeScript SDK
- JSON export
- YAML export
- Markdown documentation

### Principle

Agents should query the semantic layer rather than receive unrestricted access to the raw database schema.

## 17. Source Adapters

Source-specific ingestion should be modular.

### Initial adapter

Postgres.

### Later adapters

- Snowflake
- BigQuery
- MySQL
- SQL Server
- Databricks
- Salesforce
- REST APIs
- object storage / tabular exports
- document stores where the model can reasonably represent structure

Adapters only need to produce the canonical `DataSourceSnapshot`. Semantic generation should remain source-independent.

## 18. Privacy and Security

Privacy-safe profiling should be built into the architecture.

### Default behavior

- read-only source access
- no writes to customer databases
- local profiling where possible
- sensitive-field detection
- masking of identifiers
- redaction of email / phone / address samples
- no free-text sampling by default
- summarized distributions instead of raw rows
- configurable per-column sampling policy

Example:

```json
{
  "column": "email",
  "semantic_type": "email_address",
  "sample_shape": ["s***@company.com"],
  "distinct_count": 15231,
  "null_percentage": 3.4
}
```

## 19. Schema Drift

A generated semantic layer becomes stale if the source changes.

The product should snapshot sources and compare them over time.

Detect:

- new / removed tables
- renamed columns
- type changes
- new enum values
- relationship changes
- null-rate changes
- cardinality changes
- significant distribution shifts

Example:

```text
New value discovered:
billing.invoice_hdr.status = 7

Affected semantic concepts:
- paid_invoice
- overdue_invoice
- outstanding_balance

Affected agent capabilities:
- get_customer_balance
- list_overdue_invoices
```

Affected semantic claims should become `STALE` or be revalidated automatically.

## 20. Learning From Agent Usage

The layer should improve based on real agent interactions.

Capture signals such as:

- agent could not find a concept
- multiple plausible fields were returned
- tool or SQL generation failed
- agent requested clarification
- human corrected an answer
- task required context not represented in the semantic graph

Example:

> Agent asks: "What is Acme's current contract value?"

Possible meanings found:

- `annual_contract_value`
- `total_contract_value`
- `current_billing_value`

The system should escalate the ambiguity rather than silently guessing.

A reviewer can validate:

> For active-customer questions, “current contract value” means `annual_contract_value`.

That correction becomes reusable organizational semantic memory.

## 21. Long-Term Knowledge Stack

Over time, the product should combine:

```text
generic semantic reasoning
+
domain-specific priors
+
organization-specific validated knowledge
+
current source evidence
+
agent usage and failures
```

This accumulated semantic memory is more valuable than one-time generated descriptions.

## 22. MVP

### Goal

Prove that a generated and lightly validated semantic layer improves agent accuracy compared with raw schema access.

### Initial scope

- Postgres only
- schema dump or read-only connection
- selected privacy-safe profiling
- required agent objective
- table/column descriptions
- relevance ranking
- entity and relationship proposals
- table-grain inference
- semantic claims
- confidence and evidence
- contradiction detection
- ranked human-review queue
- approve / edit / reject / unresolved
- generated semantic JSON
- `get_context_for_question()`

### Explicitly not in MVP

- every database connector
- full text-to-SQL product
- complex data lineage
- write operations
- universal ontology
- graph database
- large RBAC system
- autonomous approval of business definitions
- broad unstructured-document ingestion
- full BI semantic metrics platform

## 23. MVP User Flow

1. Create project.
2. Define agent objective.
3. Upload schema or connect Postgres.
4. Generate source snapshot.
5. Profile relevant fields.
6. Rank relevant tables.
7. Generate semantic claims.
8. Run automatic contradiction checks.
9. Show high-impact review queue.
10. Human resolves key questions.
11. Publish semantic graph.
12. Ask sample agent questions.
13. Compile minimal context.
14. Compare agent behavior against raw-schema baseline.

## 24. Evaluation

The core product metric should be agent correctness, not description quality.

Create a benchmark of realistic domain questions.

Compare:

### Condition A

Agent receives raw schema only.

### Condition B

Agent receives raw schema + samples/descriptions.

### Condition C

Agent receives the generated semantic model.

### Condition D

Agent receives the validated semantic model + task-specific context compiler.

Measure:

- correct table selection
- correct field selection
- correct join path
- correct business-rule application
- executable SQL / tool plan
- final-answer correctness
- unsupported assumptions
- appropriate clarification behavior
- context-token usage
- human review time

### Key success metric

> Agent accuracy gain per minute of human review.

This directly captures the product's thesis.

## 25. Success Criteria for Early Product

A first version should be considered promising if it can take a large unfamiliar schema and, for one bounded agent workflow:

- reduce hundreds of tables to a small relevant subgraph;
- generate useful entity and relationship descriptions;
- automatically validate high-confidence structural semantics;
- surface fewer than approximately 20 consequential human questions;
- produce context that materially improves agent task accuracy;
- detect when it cannot safely infer a business meaning;
- preserve all reviewer decisions for reuse.

A strong demo would be:

> Take an 800-table database and reduce it to 15 meaningful questions that make one agent workflow substantially more reliable.

## 26. Product Differentiation

This product should not compete primarily on:

- schema browsing
- ER diagrams
- LLM-generated documentation
- generic data cataloging
- MCP connectivity
- raw text-to-SQL

Those are features or adjacent categories.

The differentiated loop is:

```text
physical evidence
+
semantic inference
+
automated falsification
+
human validation of only high-impact ambiguity
+
task-specific context compilation
+
agent failure feedback
+
schema drift handling
```

The product is valuable because it turns a large, unfamiliar, evolving database into a small, trusted reasoning surface for AI agents.

## 27. Product Moat

Connectors are not the moat.

Generated descriptions are not the moat.

The durable asset is the accumulated system of:

- source evidence
- semantic claims
- domain priors
- human corrections
- validation history
- representative examples
- agent usage
- failure cases
- drift history
- organization-specific language

Over time, the system learns not just what tables mean, but how the organization expects agents to reason about them.

Example:

> When a collections agent asks “amount owed”: use `invoice.balance_due`, exclude voided invoices, include posted credits, use account currency, and flag unapplied payments separately.

That is agent operational knowledge, not generic metadata.

## 28. Product Principles

1. **Never hide uncertainty.** Unknown should be represented explicitly rather than converted into a confident guess.
2. **Observed facts and semantic inference are different.** The UI and runtime should always distinguish them.
3. **Human attention is scarce.** Review the highest-impact ambiguity, not every object.
4. **Agent objective should shape the model.** Do not model everything equally.
5. **Prefer minimal sufficient context.** Agents should receive the smallest relevant semantic view for the task.
6. **Semantics should be falsifiable.** Generated claims should be tested against the data wherever possible.
7. **The model is living.** Source changes, human corrections, and agent failures should update it over time.
8. **Fail safely.** When the layer cannot answer reliably, it should tell the agent what is missing.

## 29. Phased Roadmap

### Phase 1 — Source understanding

- Postgres ingestion
- canonical physical model
- privacy-safe profiling
- schema explorer
- relevance ranking

### Phase 2 — Semantic generation

- generated descriptions
- table grain
- entities
- relationship meanings
- semantic claims
- evidence/confidence

### Phase 3 — Semantic compression and review

- domain clustering
- task-specific subgraph generation
- contradiction testing
- review-priority scoring
- approve/edit/reject/unresolved

### Phase 4 — Agent serving

- semantic JSON
- `search_schema`
- `describe_entity`
- `find_relationship_path`
- `get_context_for_question`
- task-specific context compiler

### Phase 5 — Continuous maintenance

- source snapshots
- drift detection
- semantic impact analysis
- stale-claim handling

### Phase 6 — Learning loop

- capture agent failures
- ambiguity escalation
- reusable reviewer corrections
- organization-specific semantic memory

### Phase 7 — Expansion

- Snowflake / BigQuery / MySQL
- MCP
- SDKs
- domain templates
- API and SaaS connectors
- governance and permissions

## 30. Positioning

### Short

Generate trusted AI context from unfamiliar databases.

### Expanded

Connect a customer-owned database, automatically compress its schema into relevant domain concepts, validate only the consequential ambiguity, and continuously serve the smallest trusted context AI agents need to reason accurately.

### Alternative

A semantic context compiler for AI agents over data you do not own.

## 31. North Star

The product should optimize for:

> How much trusted agent reasoning can we unlock with the least human semantic work?

The ideal experience is not:

```text
Connect → generate → export
```

It is:

```text
Connect
   ↓
understand
   ↓
compress
   ↓
validate only what matters
   ↓
serve trusted context
   ↓
learn from real usage
   ↓
stay correct as the source evolves
```

That loop is the product.
