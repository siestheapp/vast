# VAST Vision Playbook

## Why VAST Exists
- Become the most reliable AI teammate for founders who need database expertise on demand.
- Deliver durable institutional memory about schemas, rules, and prior decisions—not just one-off SQL translations.
- Operate with the caution and judgment of a senior DBA/CTO while automating repetitive data work.

## Differentiation Pillars
1. **Persistent Knowledge** – Comprehensive, versioned understanding of the connected database (schema, constraints, business rules, migrations, performance history).
2. **Safety & Governance** – Guardrails, approvals, and rollback tooling that make changes auditable and low-risk compared to ad-hoc SQL agents.
3. **Operator Mindset** – Long-term planning, standardization of patterns, and proactive maintenance beyond reactive question answering.
4. **Observable Reliability** – Instrumentation that proves VAST’s success rate, highlights regressions, and exposes its reasoning.
5. **Enterprise Console UX** – A dedicated desktop experience that surfaces status, decisions, and actions like a control tower—not a chat widget.

## Why VAST Beats DIY "Cursor + LLM" Builds
- **Compounding memory**: packaged knowledge base with auto-refreshed schema docs, tracked business rules, and replayable success/failure cases—work most teams will not harden themselves.
- **Governed operations**: approvals, rollback plans, diff previews, environment policies, and audit trails built in; far beyond prompt-driven SQL helpers.
- **Reliability guarantees**: deterministic validators, benchmark suites, telemetry, and SLAs so founders trust every run, not just the happy path demos.
- **Proactive partnership**: VAST thinks like a DBA/CTO—planning migrations, enforcing standards, surfacing maintenance tasks—where DIY projects usually stop at translating queries.
- **Brand & support**: documentation, support playbooks, and accountability that organizations expect from a critical infrastructure product.

## Where We Are Today (Sep 2025)
- CLI + FastAPI backend with schema cache, guardrails (`allow_writes` + two-key `force_write`).
- Desktop shell + web UI for chat, SQL execution, schema browsing, artifact management.
- Ops helpers for dump/restore, autonomous migration hooks, session persistence under `.vast/`.
- GPT-4o-mini powered planning with retry loop.

## Gaps Blocking “Expert DBA” Status
- **Knowledge depth** limited to flat schema summary; no semantic retrieval or lessons learned.
- **Validation** primarily runtime; missing static linting, constraint-aware checks, or regression suite.
- **Governance** lacks approvals, change windows, policy awareness, and environment separation.
- **Observability** light telemetry, no dashboards for success/error rates or drift monitoring.
- **Scale readiness** missing workflow for multi-env migrations, index tuning, performance feedback.

## Strategic Initiatives

### 1. Knowledge Engine (Persistent Memory)
- Publish full schema/constraint docs, naming patterns, business rules to `.vast/knowledge`.
- Embed artifacts (tables, relationships, past fixes) into a local vector store with metadata.
- Retrieval pipeline feeds planner with authoritative snippets and prior decisions.
- Auto-refresh embeddings on `schema_fingerprint` change; version snapshots for diffing.

### 2. Planner & Validator Overhaul
- Entity detection → targeted retrieval + strict prompt instructions (“use only provided definitions”).
- Preflight checks: SQLFluff lint, SQLAlchemy compilation, `EXPLAIN`/`ROLLBACK` dry runs.
- Constraint-aware retries driven by catalog diagnostics; capture fixes as new exemplars.

### 3. Operational Trust & Governance
- Approval tiers, change windows, and notification hooks before writes/migrations.
- Automatic rollbacks, change diffs, backup verification, and audit exports.
- Policy matrix by environment (dev/staging/prod) controlling allowed operations.

### 4. Observability & Telemetry
- Record success/error taxonomy, latency, token usage, retry counts.
- Activity feed + dashboards in the console; alerts for anomalous behavior.
- Regression smoke suite that replays tough prompts before releases.

### 5. Console UX Evolution
- Expand desktop app into a control surface with navigation, review queue, and timeline.
- Visualize schema lineage, migration history, and artifact inventory.
- Incorporate dark mode, role-based views, and context-driven tips.

### 6. Scaling & Ecosystem
- Support multi-database workspaces, multi-tenant configuration, and secrets management.
- Integrations for CI/CD (migrations), incident responders (PagerDuty), and analytics stacks.
- Eventually ship managed cloud + enterprise deployment options.

### 7. Repository-Aware Operations
- Introduce a controlled “code intel” service so VAST can read whitelisted repo artifacts (migrations/, scripts/) without raw filesystem access.
- Layer safe writers (e.g., generate migration files, open change requests) with human approvals before they reach production branches.
- Tie code insights back into the knowledge engine so authored migrations and past fixes become reusable context.

## Milestone Roadmap
1. **Knowledge + Validator Alpha (Q4 2025)**
   - Vector-backed retrieval in planner, deterministic answer for “who are you / what columns exist.”
   - Datetime/Decimal safe serialization, lint + dry-run gate before execution.
2. **Operations Readiness (Q1 2026)**
   - Approval flows, automated dumps, change diffs, audit log UI.
   - Telemetry dashboard for success rates and error taxonomy.
3. **Trusted Teammate Beta (Q2 2026)**
   - Regression suite, policy-aware environments, proactive maintenance suggestions.
   - Console enhancements (activity timeline, review queue, insights pane).
4. **Enterprise Launch (H2 2026)**
   - Multi-env workspaces, RBAC, integrations, managed hosting.
   - SLA-backed observability and support playbooks.

## Success Metrics
- >95% of generated SQL executes without manual edits on benchmark suites.
- Zero unapproved production-impacting changes across design partners.
- Time-to-answer for complex schema questions < 10 seconds end-to-end.
- Founders report VAST as “indispensable teammate” in qualitative interviews.

## Immediate Next Actions
- Implement knowledge base prototype + retrieval injection.
- Add serialization fix for datetimes/DecIMALS and beef up validator pipeline.
- Update system prompt with runtime facts (model, environment, guardrails).
- Plan telemetry schema for capturing every interaction + outcome.

*Keep this document as the north star for every feature choice and design decision.*
