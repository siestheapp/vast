# VAST Product Roadmap

## 1. Where VAST Is Now
- **Conversation pipeline**: Natural language → planner → SQL → executor with guardrails (single statement, param binding, dry-run).
- **Facts runtime**: Early short-circuit for a few metadata questions (DB identity, table count).
- **Knowledge store**: Persists schema snapshots and rules for cross-session memory.
- **Safety**: Two-key writes exposed in UI; guardrails detect dangerous SQL.
- **Operator console**: UI for chat, schema view, artifacts, audit log.
- **Gaps**:
  - Fails simple DBA questions like “how large is it” because there’s no `db_size` fact.
  - No structured **ops planner** for schema evolution & backfills.
  - Guardrails are brittle (hard-coded detection).
  - Knowledge store is underused (not integrated with ops or validation).
  - Approvals & audit exist but not full staging→promotion workflow.

---

## 2. End-State Vision
VAST should function as a **persistent DBA/CTO agent**, with these properties:

- **Knows the schema**: Full discovery, change history, rules, constraints, indexes.
- **Understands ops requests**: Detects intents like “add column”, “backfill data”, “rename table”, “enforce rule”, “migrate type”.
- **Plans like an engineer**: Generates multi-step ops plans (DDL, staging, validation, promotion, rollback) with risk annotations.
- **Executes safely**: Staging first, deterministic validators, two-key writes, audit-logged rollbacks.
- **Answers metadata instantly**: Size, counts, indexes, versions, privileges (facts runtime).
- **Has memory**: Persists decisions, rules, schema snapshots, backfill logic.
- **Converses naturally**: But always grounds answers in actual schema + ops plans, not speculation.

---

## 3. Gap Analysis
| Capability            | Current         | Needed                               |
|-----------------------|-----------------|--------------------------------------|
| Schema introspection  | Partial (facts) | Full entity/column/fk graph; diffing |
| Facts layer           | Identity, count | + size, privileges, indexes, stats   |
| Ops intent detection  | None            | Verbs: add/backfill/migrate/enforce  |
| Plan generator        | None            | Templates for DDL + staging + rollback|
| Validation            | None            | Percent checks, whitelist, null %    |
| Execution model       | Ad hoc          | Staging → validate → approve → prod  |
| Approvals             | Two-key UI only | Server-enforced, role-based, queues  |
| Audit                 | Basic logs      | Append-only with provenance & diffs  |
| Knowledge store       | Exists, unused  | Tie into schema diffs & ops rules    |
| Guardrails            | SQL type checks | Hardened (regex, sqlglot, sim check) |

---

## 4. Roadmap

### Phase 1 — Strengthen Foundation (next 30 days)
- Expand **facts registry**: db_size, index list, role/privileges, row counts (fast metadata).
- Harden guardrails: enforce single statement, param binding, no multi-DDL.
- Tie facts + guardrails into **audit log** with provenance.
- Build **schema discovery/fingerprint** (tables, fks, indexes) with caching.

### Phase 2 — Ops Planner (30–60 days)
- Add **intent detectors** for ops verbs.
- Create **plan generator** with SQL templates:
  - add column/table,
  - create staging table,
  - backfill from source,
  - validators,
  - promotion,
  - rollback snapshot/revert.
- Add **Plan object** format (JSON/YAML) + UI preview.
- Run plans in **staging only**; produce validation artifacts.
- Approval flow: `[Run in staging] → [Validate] → [Request approval] → [Promote]`.

### Phase 3 — Full DBA/CTO Loop (60–90 days)
- Add **entity discovery** to auto-propose candidate tables/cols for ops.
- Expand validators: distribution checks, null %, referential integrity.
- Add **rollback orchestration** (snapshots, revert scripts).
- Tie **knowledge store** to schema evolution + rules (e.g., “all PKs use UUID”).
- Add **role-based access controls**: operator, approver, auditor.
- Provide **metrics dashboard**: cost, query volume, blocked dangerous ops.

---

## 5. Success Criteria
- **Latency**: facts answers < 200ms; ops plan generation < 2s.
- **Coverage**: ≥ 80% of ops requests route to planner, not LLM.
- **Safety**: 100% of writes require staging + approval + audit.
- **Adoption**: Operator can replace DBA for schema/backfill ops confidently.

