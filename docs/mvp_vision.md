Here’s a concrete shipping plan for “error-free SQL generation” as the MVP feature — so VAST can draft correct, schema-aware statements for any database without hardcoding:

Phase 1 — Strengthen the DB Brain (1–2 days)

Catalog layer (done): already have catalog_pg.py with table/column introspection.

Extend: add indexes, FKs, comments, enums, and column stats (null %, distinct values).

Cache: JSON snapshot in .vast/catalog.json keyed by schema fingerprint (hash of pg_class + pg_attribute rows).

API: serve this via /catalog/snapshot.

✅ Outcome: a single, authoritative, fast-to-fetch DB brain.

Phase 2 — Ground the LLM (1–2 days)

Retriever: from the DB brain, select relevant tables/columns for each prompt (embedding + FK traversal).

Prompt builder: inject those facts into the system prompt (tables: …, columns: …, FKs: …) before plan generation.

Identity facts: include {project_name, project_ref, database} so the model knows the DB’s friendly name.

✅ Outcome: model always sees the schema subset it needs.

Phase 3 — Pre-execution Guard (2–3 days)

Validator: parse generated SQL with sqlglot or run EXPLAIN (VERBOSE, COSTS OFF) and extract identifiers.

Check: compare identifiers against the DB brain.

If mismatch: regenerate with explicit hints (“Column ‘sale_pricee’ not found; did you mean ‘sale_price’?”).

Block execution if still invalid — return error to user with suggestions.

✅ Outcome: no “table/column does not exist” runtime errors.

Phase 4 — Conversation Integration (1 day)

Conversation loop:

NL → plan SQL.

Guard validates identifiers.

If OK → execute.

Results piped back into chat message.

Draft mode (for writes): instead of executing, return SQL + params + notes to user.

✅ Outcome: user can say “Add a Theory shirt” and get back a safe, valid transactional SQL draft tailored to their schema.

Phase 5 — Enterprise polish (optional, ~1 week)

Observability: plan/exec logs, timings, audit of all SQL.

Graceful degrade: if catalog query fails, still answer with identity + table count.

UX: default LIMIT 10, parse “top N,” badge health = db_ok && llm_ok.

Rollout checklist

 Extend catalog_pg.py to cover indexes/FKs/comments/stats.

 Implement schema cache + /catalog/snapshot.

 Add SQL validator in service.safe_execute.

 Add retry-with-hints loop in agent.plan_sql.

 Add draft write path for DML/DDL.

 Tests: test_identifier_guard_blocks, test_sql_regen_with_hints.

 Demo script: “create review table + FK + index + seed row” end-to-end.

⚡ With Phases 1–4, you’ll have the MVP: VAST never proposes invalid identifiers, and it can draft correct INSERT/UPDATE/DDL tailored to any schema.