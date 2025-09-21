import re, sys
from pathlib import Path

if len(sys.argv) != 2:
    print("usage: python apply_authoritative_db_policy.py <path/to/conversation.py>")
    raise SystemExit(2)

target = Path(sys.argv[1])
if not target.exists():
    raise SystemExit(f"File not found: {target}")

src = target.read_text(encoding="utf-8")
orig = src
changed=False

policy = '''
        Authoritative DB Policy:
        - For any question about the CURRENT database (counts, sizes, existence, schema, relationships, examples), you MUST:
          1) Generate a read-only SQL query against the connected database,
          2) Execute it via VAST (no guessing),
          3) Answer ONLY from the query result. Include a compact Markdown table when helpful.
        - Never use phrases like “typically”, “usually”, “likely”, or generic answers about databases.
        - If you cannot run a query, reply: "I need to run a query to determine this." and include the SQL in ```sql fences.
'''
tone_anchor = re.compile(r'Tone:\s*precise,.*?only\s+necessary\s+detail\.', re.S)
if policy not in src:
    m = tone_anchor.search(src)
    if m:
        src = src[:m.end()] + "\n" + policy + src[m.end():]
        changed=True

fastpath = r'''
        # --- Fast paths that MUST be grounded on the live DB -----------------
        text_lower = user_input.lower()
        if ("biggest tables" in text_lower) or (("largest" in text_lower) and ("tables" in text_lower)):
            data = self._biggest_tables(limit=10)
            md = self._render_biggest_tables_markdown(data)
            assistant_msg = Message(role=MessageRole.ASSISTANT, content=md)
            self.messages.append(assistant_msg)
            self._save_session()
            return md
        # ---------------------------------------------------------------------
'''
if "biggest tables" not in src:
    src, n = re.subn(r"(self\.last_actions\s*=\s*\[\]\s*)", r"\1" + fastpath + "\n", src, count=1)
    if n: changed=True

if "response = self._enforce_grounding" not in src:
    src, n = re.subn(
        r"(assistant_msg\s*=\s*Message\(role=MessageRole\.ASSISTANT,\s*content=response\))",
        r"response = self._enforce_grounding(user_input, response)\n        \1",
        src, count=1
    )
    if n: changed=True

if "_biggest_tables(" not in src:
    helpers = r'''
    # ------------------------ Grounded helpers -------------------------------
    def _biggest_tables(self, limit: int = 10):
        """Return largest tables by total size using pg_total_relation_size"""
        from sqlalchemy import text
        try:
            from .db import get_engine
        except Exception:
            from db import get_engine
        sql = """
        SELECT
          n.nspname AS schema,
          c.relname AS table,
          pg_total_relation_size(c.oid) AS total_bytes,
          pg_relation_size(c.oid)      AS table_bytes,
          COALESCE(pg_stat_get_live_tuples(c.oid), 0) AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT :limit;
        """
        with get_engine(readonly=True).begin() as conn:
            rows = conn.execute(text(sql), {"limit": limit}).mappings().all()
        return rows

    def _render_biggest_tables_markdown(self, rows) -> str:
        def fmt_bytes(b):
            units = ["B","KB","MB","GB","TB","PB"]
            i = 0
            b = float(b or 0)
            while b >= 1024 and i < len(units)-1:
                b /= 1024.0
                i += 1
            return f"{b:.0f} {units[i]}"
        lines = [
            "Here are the largest tables **in the connected database** (by total size):",
            "",
            "| # | schema.table | size | rows |",
            "|---|--------------|------|------|",
        ]
        for i, r in enumerate(rows, 1):
            fq = f"{r['schema']}.{r['table']}"
            lines.append(f"| {i} | `{fq}` | {fmt_bytes(r['total_bytes'])} | {int(r['approx_rows'])} |")
        return "\n".join(lines)

    # ---------------------- Grounding enforcement ---------------------------
    import re as _re
    _HEDGES = _re.compile(r"\b(typically|usually|likely|generally|commonly|often|might)\b", _re.I)

    def _is_db_fact_question(self, s: str) -> bool:
        s = s.lower()
        return any(k in s for k in [
            "how many","count","size","largest","biggest","rows","tables","indexes",
            "what database","connected to","schema","columns","foreign key","table size"
        ])

    def _enforce_grounding(self, user_input: str, response: str) -> str:
        """If the user asked a DB-fact question, forbid hedgy language unless we ran SQL."""
        if not response:
            return response
        if self._is_db_fact_question(user_input) and _HEDGES.search(response):
            return ("I need to run a query to determine this.\n\n"
                    "```sql\n-- VAST will generate and execute the appropriate SELECT against the connected database\n```")
        return response
'''
    src = src.rstrip() + "\n" + helpers
    changed=True

if changed:
    backup = target.with_suffix(target.suffix + ".bak")
    if not backup.exists():
        backup.write_text(orig, encoding="utf-8")
    target.write_text(src, encoding="utf-8")
    print(f"Updated {target} ✅ (backup: {backup.name})")
else:
    print("No changes made (already applied).")
