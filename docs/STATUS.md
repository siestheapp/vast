# VAST — Project Status

## TL;DR
- VAST is connected to the **freestyle Supabase Postgres** (read-only) and can answer natural-language questions by planning safe SQL and executing them.
- Safety rails are in place for identifier validation, system-schema reads, and schema-scoped introspection.
- Test suite passes for Supabase-compatible subsets; Pagila-only tests are excluded pending demo migration.
- Next: retarget `demo:writes` to freestyle tables, wire a proper writer role, and add enterprise safety/observability.

## What’s Working (Today)
- **Supabase connectivity (RO):** `user=vast_ro`, `db=postgres`, scoped to `public`.
- **NL → SQL (read-only):** 
  - Top-N aggregations (e.g., styles per brand).
  - Data dictionary lookups (columns/types via information_schema).
  - View-based queries (e.g., latest price per variant).
  - Null/anti-join patterns (e.g., variants missing images).
- **Diagnostics & schema handling:**
  - `VAST_SCHEMA_INCLUDE=public` to avoid Supabase internal schemas (`realtime`, etc.).
  - System-schema reads allowed for SELECT/EXPLAIN; writes still guarded.
  - Fresh schema cache builds; deterministic fingerprint preserved.
- **CLI UX:**
  - Accepts `DATABASE_URL_RO` (no strict `DATABASE_URL` dependency).
  - Helpful env error messages with copy-paste `export …` examples.
  - New `health` command prints env/DB info, refreshes schema, and runs a smoke read.
- **Tests:**
  - Superset passing when excluding Pagila-only & write-integration tests.
  - New config and CLI diagnostics tests green.

## Recent Changes (Key PRs/Files)
- **Identifier guard:** allow safe reads from PostgreSQL system schemas while keeping write enforcement.
  - `src/vast/identifier_guard.py` (+ SYSTEM_SCHEMAS, _is_system_relation; strict-mode reads allowed)
  - `tests/test_identifier_strict.py`
- **Config & env loading:** `.env` support; RO/RW split; legacy `DATABASE_URL` fallback.
  - `src/vast/config.py` (pydantic Settings; `get_ro_url`, `get_rw_url`)
  - `tests/test_config_write_url.py`
- **CLI gates & UX:** accept `DATABASE_URL_RO`; RW required for write paths; example `export` lines in errors.
  - `cli.py` (env guard updates; import helpers; added `health`)
  - `tests/test_cli_diagnostics.py`
- **Introspection scope & resilience:** honor `VAST_SCHEMA_INCLUDE`; skip schemas without USAGE; graceful column reflection.
  - `src/vast/introspect.py` (has_schema_usage, filtered `information_schema` queries, deterministic fingerprint)
- **Test hygiene:** subset runs green on Supabase (excluded Pagila-only & write-integration tests).

## How to Run (Dev)
```bash
# one-time
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# .env at repo root (values are examples; use your real ones)
OPENAI_API_KEY=sk-...
DATABASE_URL_RO=postgresql://vast_ro:***@db.<supabase>.co:5432/postgres?sslmode=require
DATABASE_URL_RW=postgresql://vast_ro:***@db.<supabase>.co:5432/postgres?sslmode=require  # safe RO mirror for now
VAST_DIAGNOSTICS=1
VAST_SCHEMA_INCLUDE=public
```
