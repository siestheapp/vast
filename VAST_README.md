# Vast / Vast1 – AI Database Architect & Operator

_Last updated: 2025-09-20 03:59 _

## 0) TL;DR

Vast is a product vision for an **AI database operator** that behaves like a cautious, senior DBA/CTO: it remembers your schema and business rules, plans safe queries and migrations, and operates your database continuously. **Vast1** is the standalone MVP we’re building now. It works against **existing databases**, persists schema context, generates **safe SQL** from natural language, and executes with **strong guardrails** (read-only by default; two‑key writes).

This document captures the **purpose, scope, architecture, setup, and progress** so far, and the **next steps** toward a pilot within ~3 months. Drop this file into Cursor chat for instant project context.

---

## 1) Product Definitions

### Vast (the company/product)
- **Vision:** An “AI DBA/CTO” that remembers your database, follows business rules, and evolves the schema and data pipelines safely over time.
- **Why now:** LLMs + agent patterns enable non-experts to design and operate complex databases, but tooling today is stateless and risky. Persisting schema memory + rules + audit‑safe actions unlocks real value.
- **Target user (initial):** Non‑technical founders and small teams with fast‑evolving relational data, plus technical teams wanting an assistant that reliably handles day‑to‑day DB operations.
- **Differentiation:**
  - **Persistent schema memory** (not just stateless chat).
  - **Business‑rule adherence** (encode constraints/intent as first‑class context).
  - **Audit‑safe execution** (dry‑run, approvals, reversible migrations).
  - **Vendor‑agnostic posture** (start Postgres; design for portability).

### Vast1 (the MVP agent)
- **Goal:** Demonstrate a focused subset: **schema introspection + persistent memory + safe SQL generation & execution** for an existing Postgres DB.
- **Status:** Working CLI that connects to Postgres (Pagila sample), lists schema, generates SQL with GPT, executes with guardrails, caches schema snapshot, supports named params, and provides “two‑key turn” writes.
- **Scope (MVP):**
  - Read‑only by default (SELECT).
  - Optional **INSERT/UPDATE** with `--write` + `--force-write` (DRY RUN by default).
  - Named parameters via `--params` JSON.
  - Schema cache with fingerprint auto‑refresh.
  - Minimal operator UX via CLI; no full UI yet.

---

## 2) What We’ve Done (Chronological)

**Project bootstrap (Python + SQLAlchemy + Typer CLI)**
- Created repo `vast` with `src/vast/` modules and `cli.py` CLI.
- Installed minimal deps: `psycopg[binary]`, `sqlalchemy`, `python-dotenv`, `openai`, `rich`, `typer`.
- Files created:
  - `src/vast/config.py` – loads `.env` (`DATABASE_URL`, `OPENAI_API_KEY`, `VAST_ENV`).
  - `src/vast/db.py` – engine factory + **safe_execute** with guardrails (blocks DDL/DELETE, detects writes).
  - `src/vast/introspect.py` – **schema introspection** (moved to SQLAlchemy Inspector for robustness) + **schema_fingerprint**.
  - `src/vast/agent.py` – LLM planner using **OpenAI SDK v1** (`chat.completions.create`, model `gpt-4o-mini`), prompt rules, schema cache, robust response handling.
  - `cli.py` – commands: `env`, `schema`, `columns`, `run`, `ask`.

**Local Postgres (Docker) with Pagila sample DB**
- Ran Postgres container on **host port 5433** to avoid conflicts.
- Created DB `pagila`; loaded `pagila-schema.sql` and `pagila-data.sql`.
- Created **least‑privilege** DB role `vast_ro` with `SELECT/INSERT/UPDATE` on tables (explicit sequence grants optional for writes).
- Verified connectivity via `psql` and `python cli.py schema`.

**Guardrails + Safety**
- **Read‑only default**: all DDL/DELETE blocked. Writes require `--write` and **also** `--force-write` to truly execute (otherwise **DRY RUN** preview).
- **Named parameters**: `--params '{"key":"value"}'` with prompt nudging to avoid inlined literals.
- **Single statement policy**: normalize model output to **one** statement; strip code fences.
- **Schema cache**: `.vast/schema_cache.json` with a **fingerprint** (hash of schema/columns). Auto‑refresh if DB changes, or `--refresh-schema` flag.
- **LLM SDK migration**: Using **OpenAI Python SDK v1+** (no legacy `openai.ChatCompletion.create`). Robust “empty response” handling.

**Working demo examples**
- `python cli.py ask "top 10 films by rental count"` → correct JOIN/GROUP/LIMIT.
- `python cli.py ask "list films released after :yr order by release_year, title limit :lim" --params '{"yr":2006,"lim":5}'` → SELECT with params.
- `python cli.py ask "insert a category with name :name and return it" --write --params '{"name":"TESTCAT"}'` → **DRY RUN** preview (no execute).
- Optional: sequence grants enable `--force-write` to actually insert.

**Debugging/infra issues resolved**
- Docker daemon not running → started Docker Desktop.
- Port conflicts on 5432 → mapped container to **5433** (`-p 5433:5432`).
- Wrong `DATABASE_URL` value (`DATABASE_URL=DATABASE_URL=...`) → fixed.
- Switched `introspect.py` to SQLAlchemy Inspector; fixed malformed SQL.
- Upgraded agent to handle empty/None LLM replies with explicit error messages.

---

## 3) Architecture (MVP)

```
User (CLI)  ──>  Vast1 Agent (planner)  ──>  Safe SQL Executor  ──>  Postgres
                  ↑            │                     │
                  │            │                     └─ Guardrails: RO default,
                  │            │                        two‑key writes, block DDL/DELETE
          Schema Cache         │
     (.vast/schema_cache.json) │
                  │            └─ LLM (OpenAI, gpt‑4o‑mini) with schema context + param hints
                  └─ Schema Introspection (Inspector)  → Fingerprint refresh
```

**Key components**
- **Agent (planner):** Converts NL → SQL using a strict system prompt; includes schema snapshot; enforces single statement & parameterization.
- **Schema cache:** Lightweight persisted context (immediate recall, small token footprint). Auto‑refresh on DB change.
- **Executor:** Blocks dangerous ops; separates **allow_writes** from **force_write** (DRY RUN by default).
- **Params:** Named parameters passed safely to SQLAlchemy; no string interpolation.
- **DB auth:** Least‑privilege DB role; optional sequence grants to allow auto‑increment inserts.

---

## 4) Repository Layout

```
vast/
├─ .env.example
├─ .vast/
│  └─ schema_cache.json         # generated
├─ cli.py                       # Typer CLI
├─ requirements.txt
└─ src/vast/
   ├─ __init__.py
   ├─ agent.py                  # LLM planner + schema cache
   ├─ config.py                 # env settings
   ├─ db.py                     # engine + safe_execute
   └─ introspect.py             # inspector + fingerprint
```

---

## 5) Setup & Usage

### 5.1 Environment
```
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install "psycopg[binary]" sqlalchemy python-dotenv openai rich typer
pip freeze > requirements.txt
```

Create `.env`:
```
DATABASE_URL=postgresql+psycopg://vast_ro:vast_ro_pwd@localhost:5433/pagila
OPENAI_API_KEY=sk-...
VAST_ENV=dev
```

### 5.2 Postgres (Docker, port 5433)
```
docker run --name vast-pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 -d postgres:16
docker exec -it vast-pg psql -U postgres -c "CREATE DATABASE pagila;"
curl -LO https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-schema.sql
curl -LO https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-data.sql
docker exec -i vast-pg psql -U postgres -d pagila < pagila-schema.sql
docker exec -i vast-pg psql -U postgres -d pagila < pagila-data.sql
docker exec -it vast-pg psql -U postgres -d pagila -c "
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'vast_ro') THEN
      CREATE ROLE vast_ro LOGIN PASSWORD 'vast_ro_pwd';
   END IF;
END
$$;
GRANT CONNECT ON DATABASE pagila TO vast_ro;
GRANT USAGE ON SCHEMA public TO vast_ro;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO vast_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO vast_ro;
"
# Optional: allow auto-increment inserts across sequences
docker exec -it vast-pg psql -U postgres -d pagila -c "
DO $$
DECLARE s RECORD;
BEGIN
  FOR s IN SELECT sequence_schema, sequence_name
           FROM information_schema.sequences WHERE sequence_schema = 'public'
  LOOP
    EXECUTE format('GRANT USAGE, SELECT, UPDATE ON SEQUENCE %I.%I TO vast_ro',
                   s.sequence_schema, s.sequence_name);
  END LOOP;
END
$$;
"
```

### 5.3 CLI
```
# sanity
python cli.py env
python cli.py schema
python cli.py columns public film

# run raw SQL (read-only)
python cli.py run "SELECT title FROM public.film LIMIT 3"

# NL → SQL (read-only default)
python cli.py ask "top 10 films by rental count"

# With named parameters
python cli.py ask "list films released after :yr order by release_year, title limit :lim" --params '{"yr":2006,"lim":5}'

# Write flow: proposal (DRY RUN)
python cli.py ask "insert a category with name :name and return it" --write --params '{"name":"TESTCAT"}'

# Write flow: actually execute (requires sequence grants)
python cli.py ask "insert a category with name :name and return it" --write --force-write --params '{"name":"TESTCAT2"}'

# Refresh schema cache when needed
python cli.py ask "..." --refresh-schema
```

---

## 6) Prompt Rules (Current)

- Generate ONLY safe SQL.
  - Default: **SELECT** only.
  - With `allow_writes=True`: allow **INSERT/UPDATE** with WHERE; **NEVER DELETE**.
- **No DDL**: disallow CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE.
- **Use only existing tables/columns** from provided schema snapshot.
- **Named parameters** (e.g., `:name`, `:id`, `:limit`); do **not** inline literals.
- **Single statement** only; strip fences/backticks; max ~1 statement.

---

## 7) Roadmap (Next 2–6 Weeks)

### A) Core polish (MVP hardening)
- **Inline‑literal guard**: If `--params` provided but output contains quoted literals for those keys, auto‑reject and regenerate.
- **Error‑aware retry**: On SQL error, round‑trip error back to LLM (1–2 retries) before surfacing to user.
- **Better schema context**: Table‑selection step (agent first chooses relevant tables, then load detailed column metadata only for those).
- **Unit tests**: pytest smoke tests for planner, executor (DRY RUN), and a happy‑path INSERT.

### B) Memory & rules
- **Business rules store** (per‑DB): plaintext/JSON rules (e.g., “never hard‑delete; price must be >=0”) retrieved into prompt.
- **Conversation memory**: per‑session history with summarization to keep context under token limits.

### C) Writes & migrations (careful expansion)
- **Two‑phase writes**: Planner proposes plan + SQL; executor executes only after human ACK.
- **Safe migrations (read‑only at first)**: Generate migration scripts (SQL files) with comments/diffs; never run automatically.
- **Staging runner**: Always run writes on **staging** first; compare rowcounts/hashes; then promote to prod with ACK.

### D) Observability & ops
- **Audit log**: Append-only log of prompts, SQL, params, execution status, rowcounts, errors.
- **Telemetry**: Basic metrics (success rate, retries, avg latency, token usage).

### E) Integration path
- **Freestyle DB sandbox**: Connect Vast1 to a cloned Freestyle DB to validate domain‑specific tasks (catalog ingestion, variants, etc.).
- **Plugin surface**: Simple HTTP API for `ask/run` for easy UI embedding later.

---

## 8) Pilot Plan (Target: within ~3 months)

**Candidate pilots**
- Internal (Freestyle clone): realistic schema and data; build case study.
- 1–2 external founders with messy or evolving Postgres (catalogs, ops, CRM‑like).

**Pilot scope**
- Read‑heavy assistance and “assisted writes” with human approval.
- No destructive ops; all changes logged; sequence grants as needed.
- Weekly check‑ins; collect prompts, failures, and requested features.

**Success criteria**
- ≥70% NL→SQL success without manual editing on first or second try.
- Measurable time savings for founder (e.g., 5–10 routine queries automated).
- Zero data loss/incidents; clear path to feature requests (e.g., rules, migrations).

---

## 9) Risks & Mitigations

- **LLM hallucinations / wrong SQL** → Guardrails, single‑stmt policy, retries with DB error feedback, read‑only default, least‑privilege role.
- **Schema drift** → Fingerprint + cache refresh; table‑selection strategy to keep context small and current.
- **Write safety** → Two‑key writes, DRY RUN previews, staging‑first execution, sequence privileges explicit.
- **Cost/latency** → Cache schema; favor small context; consider smaller/faster models for simple reads.
- **Vendor dependence** → Abstract LLM client; keep prompts model‑agnostic; option to swap in local/open models later.

---

## 10) Stretch Goals (Post‑MVP)

- **Business‑rule engine** with formal policy language (YAML/JSON) → generate constraints/triggers/functions as proposals.
- **Migration assistant** that plans multi‑step, reversible migrations with data backfills and rollout plans.
- **Perf tuning advisor**: EXPLAIN/ANALYZE assist; index recommendations; slow query detection.
- **Multi‑DB** support (MySQL, SQLite) via adapters; start with Postgres feature parity.
- **Light UI**: web chat + schema/map view + approval queue for writes/migrations.

---

## 11) Example Prompts

- “**Top 10 films by rental count**.”  
- “**List films released after :yr order by release_year, title limit :lim**” with `--params '{"yr":2006,"lim":5}'`  
- “**Insert a category with name :name and return it**” with `--write` (DRY RUN) and `--force-write` to really execute.

---

## 12) Glossary

- **Two‑key write**: Require both `--write` and `--force-write` to execute writes; otherwise simulate.
- **Schema fingerprint**: Hash of schema/columns to detect drift and trigger cache refresh.
- **Param hints**: Passing `--params` JSON encourages named parameters and blocks literal inlining.

---

## 13) Owner’s Notes (why this doc exists)

- Cursor/Chat context often resets; this README provides durable intent and the exact **operating principles** of Vast1.
- Paste this into any AI coding session to keep responses aligned with safety, memory, and agent design goals.
- Update this file as the system evolves (new flags, models, rules, etc.).
