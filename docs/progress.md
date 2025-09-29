VAST MVP Progress & Next Steps
🎯 Overall Goal

The MVP for VAST is to be a trustworthy AI SQL agent / database architect that can:

Reliably understand a database schema (tables, columns, relationships).

Answer basic deterministic queries (counts, lists, latest N per group) without relying on the LLM.

Fall back to the LLM only when queries cannot be resolved via templates, but with guardrails (identifier checks, safe limits, clarifications).

This is the foundation for trust. If VAST can’t answer “how many users?” or “list brand slugs” correctly and fast, safety features or advanced UX won’t matter.

✅ Current State
Schema Catalog

Catalog building works end-to-end: columns, keys, indexes, samples, aliases.

Cached under .vast/schema_cards/ and accessible via cli.py catalog show.

Aliases seeded (singular/plural + column-driven) — helps matching natural language.

Resolver & Templates

Deterministic resolver added:

Detects simple intents like count and list.

Short-circuits to SQL templates instead of going through the LLM.

Ambiguity guard in place: when scores are too close, VAST asks for clarification rather than guessing.

Timing & Performance

Engine caching and pooled connection reduce DB handshake time.

Debug output now shows full timing breakdown (catalog_ms, plan_ms, engine_ms, exec_ms, llm_ms, total_ms).

Confirmed: basic “how many brands” runs in ~150 ms.

Guardrails

New planner cross-checks identifiers against allowed tables.

Ensures queries stay schema-safe and always apply a LIMIT.

If schema match is too weak, VAST returns a clarification.

Issues Encountered

Supabase DB sometimes terminates or caps connections.

Version mismatches (sqlglot) caused compatibility errors, since fixed by upgrade.

The “latest N per group” template (product_url→style→brand) is not fully wired: resolver doesn’t detect this intent yet, so queries still fall through to the LLM.

📌 Immediate Term Status

Basic deterministic queries (count, list) are working, correct, and fast.

Catalog is healthy and introspection reliable.

LLM fallback with guardrails exists, but multi-table joins/templates not yet robust.

Latest work attempted to add “latest N per group” deterministic path, but it is incomplete: detect_latest_per_group is not defined in resolver, so intent is never triggered.

🚀 MVP Target (Near-Term Deliverable)

VAST should:

Resolve counts, lists, and latest-N-per-group queries deterministically and safely.

Handle ambiguous or unknown queries gracefully with clarification, not bad SQL.

Provide debug timing + allowed_tables consistently so developers/users can trust execution.

This forms the baseline MVP milestone: “DB-aware, safe, and fast for 80% of common analytic queries.”

🔜 Next Steps

Finish the “latest per group” resolver path

Add detect_latest_per_group to resolver.py.

Wire into resolve_entities() so intent is returned.

Ensure agent.py short-circuits to a deterministic join (product_url → style → brand).

Verify with:

python cli.py ask "latest 5 product urls per brand" --debug


Validate deterministic templates

Test count, list, and latest-per-group queries.

Confirm llm_ms=0 for these.

Confirm correct joins: product_url.style_id → style.id → brand.id.

Add coverage tests

Unit test detect_latest_per_group regex.

End-to-end test on catalog + resolver + agent for latest N queries.

Harden DB connectivity

Decide whether to pin a local Postgres for dev/tests (avoid Supabase session limits).

Keep Supabase as staging/prod target.

📍 Summary

We’ve made strong progress: schema introspection, caching, and deterministic resolver for counts/lists are all working and fast. The missing piece for MVP is multi-table deterministic joins (latest per group). Once that’s wired, VAST will cover the majority of everyday analytic queries with confidence and speed — the foundation needed to move on to trust, safety, and advanced workflows.

👉 Concrete Next Step: Implement detect_latest_per_group in resolver.py and hook it into agent.py so “latest N per group” queries bypass the LLM. Then test with cli.py ask "latest 5 product urls per brand" --debug.