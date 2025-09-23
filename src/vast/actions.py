from __future__ import annotations
import re, shlex, subprocess
from pathlib import Path
from datetime import datetime, UTC
from typing import TypedDict, List
from .config import settings

class ActionResult(TypedDict):
    ok: bool
    command: str
    stdout: str
    stderr: str
    artifacts: List[str]

ART_DIR = Path(".vast/artifacts")
ART_DIR.mkdir(parents=True, exist_ok=True)


def build_review_feature_migration() -> list[str]:
    """Return SQL statements that build the demo review feature."""
    return [
        (
            "CREATE TABLE IF NOT EXISTS public.review ("
            "  review_id SERIAL PRIMARY KEY,"
            "  customer_id INT NOT NULL,"
            "  film_id INT NOT NULL,"
            "  rating INT CHECK (rating BETWEEN 1 AND 5),"
            "  comment TEXT,"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        ),
        (
            "DO $$\n"
            "BEGIN\n"
            "    IF NOT EXISTS (\n"
            "        SELECT 1 FROM pg_constraint\n"
            "        WHERE conname = 'fk_review_customer'\n"
            "          AND conrelid = 'public.review'::regclass\n"
            "    ) THEN\n"
            "        ALTER TABLE public.review\n"
            "            ADD CONSTRAINT fk_review_customer\n"
            "            FOREIGN KEY (customer_id) REFERENCES public.customer(customer_id);\n"
            "    END IF;\n"
            "END$$;"
        ),
        (
            "DO $$\n"
            "BEGIN\n"
            "    IF NOT EXISTS (\n"
            "        SELECT 1 FROM pg_constraint\n"
            "        WHERE conname = 'fk_review_film'\n"
            "          AND conrelid = 'public.review'::regclass\n"
            "    ) THEN\n"
            "        ALTER TABLE public.review\n"
            "            ADD CONSTRAINT fk_review_film\n"
            "            FOREIGN KEY (film_id) REFERENCES public.film(film_id);\n"
            "    END IF;\n"
            "END$$;"
        ),
        "CREATE INDEX IF NOT EXISTS idx_review_created_at ON public.review(created_at)",
        (
            "INSERT INTO public.review (customer_id, film_id, rating, comment) "
            "VALUES (1, 1, 5, 'Excellent film!') "
            "ON CONFLICT DO NOTHING"
        ),
    ]


def _run(cmd: str, timeout: int = 900) -> ActionResult:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return {
        "ok": p.returncode == 0,
        "command": cmd,
        "stdout": p.stdout,
        "stderr": p.stderr,
        "artifacts": [],
    }

def _normalize_url(url: str) -> str:
    # libpq-style URL for CLI tools
    return url.replace("postgresql+psycopg://", "postgresql://").strip()

def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def write_text_file(path: str, content: str) -> ActionResult:
    """Write a UTF-8 text file safely, creating parent directories."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "command": f"write({path})", "stdout": "", "stderr": "", "artifacts": [str(p)]}

def apply_sql_file(filepath: str) -> ActionResult:
    """Apply an .sql file to the configured database in a single transaction using psql."""
    url = str(settings.database_url_rw)  # Use RW for SQL file execution
    if not url:
        return {"ok": False, "command": "", "stdout": "", "stderr": "DATABASE_URL missing", "artifacts": []}
    p = Path(filepath)
    if not p.exists():
        return {"ok": False, "command": "", "stdout": "", "stderr": f"SQL file not found: {filepath}", "artifacts": []}

    url_norm = _normalize_url(url)
    cmd = (
        f"psql {shlex.quote(url_norm)} -v ON_ERROR_STOP=1 --single-transaction -f {shlex.quote(str(p))}"
    )
    res = _run(cmd)
    if res["ok"]:
        res["artifacts"] = [str(p)]
    return res

def pg_dump_database(outfile: str | None = None, container_name: str = "vast-pg", fmt: str = "custom") -> ActionResult:
    """
    Create a pg_dump. Supports custom (compressed) or plain (human-readable) formats.
    If local pg_dump fails due to server/client version mismatch, automatically
    dump *inside* the Docker container and copy out.
    """
    url = str(settings.database_url_ro)  # Use RO for dumps
    if not url:
        return {"ok": False, "command": "", "stdout": "", "stderr": "DATABASE_URL missing", "artifacts": []}

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # Choose default extension based on format
    fmt_norm = (fmt or "custom").lower()
    ext = ".sql" if fmt_norm == "plain" else ".dump"
    out = Path(outfile) if outfile else ART_DIR / f"dump-{ts}{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)

    # 1) Try local pg_dump
    url_norm = _normalize_url(url)
    fmt_flag = "--format=plain" if fmt_norm == "plain" else "--format=custom"
    cmd_local = f"pg_dump {fmt_flag} --no-owner --file {shlex.quote(str(out))} {shlex.quote(url_norm)}"
    res = _run(cmd_local)

    if res["ok"] and out.exists() and out.stat().st_size > 0:
        res["artifacts"] = [str(out)]
        res["stdout"] += f"\nsha256={_sha256(out)}"
        return res

    # 2) Fallback if server/client mismatch
    mismatch = bool(re.search(r"server version.*?pg_dump version", res["stderr"], re.I))
    if mismatch:
        tmp_in = f"/tmp/{out.name}"
        # Use postgres superuser in the container (works with the official image defaults)
        fmt_flag_in = "--format=plain" if fmt_norm == "plain" else "--format=custom"
        cmd_in = (
            "docker exec -i " + shlex.quote(container_name) +
            " bash -lc " + shlex.quote(f"pg_dump -U postgres -d pagila {fmt_flag_in} --no-owner -f {tmp_in}")
        )
        res2 = _run(cmd_in)
        if res2["ok"]:
            cmd_cp = f"docker cp {shlex.quote(container_name)}:{shlex.quote(tmp_in)} {shlex.quote(str(out))}"
            res3 = _run(cmd_cp)
            if res3["ok"] and out.exists() and out.stat().st_size > 0:
                res3["artifacts"] = [str(out)]
                res3["stdout"] = (res["stdout"] + "\n" + res2["stdout"] + "\n" + res3["stdout"]).strip()
                res3["stderr"] = (res["stderr"] + "\n" + res2["stderr"] + "\n" + res3["stderr"]).strip()
                res3["command"] = f"{cmd_local}  ||  {cmd_in} && {cmd_cp}"
                res3["stdout"] += f"\nsha256={_sha256(out)}"
                return res3

    # 3) If we get here, return the original failure (no artifact)
    return res

def pg_restore_list(dumpfile: str) -> ActionResult:
    """List contents of a dump file using pg_restore --list"""
    if not Path(dumpfile).exists():
        return {
            "ok": False,
            "command": f"pg_restore --list {shlex.quote(dumpfile)}",
            "stdout": "",
            "stderr": f"Dump file not found: {dumpfile}",
            "artifacts": []
        }
    
    cmd = f"pg_restore --list {shlex.quote(dumpfile)}"
    return _run(cmd)

def pg_restore_into(dumpfile: str, target_db_url: str, drop: bool = False) -> ActionResult:
    """Restore a dump file into a target database"""
    if not Path(dumpfile).exists():
        return {
            "ok": False,
            "command": f"pg_restore {dumpfile} -> {target_db_url}",
            "stdout": "",
            "stderr": f"Dump file not found: {dumpfile}",
            "artifacts": []
        }
    
    # Normalize the target URL
    normalized_url = _normalize_url(target_db_url)
    
    # Build command
    flags = "--clean " if drop else ""
    cmd = f"pg_restore {flags}--no-owner --dbname {shlex.quote(normalized_url)} {shlex.quote(dumpfile)}"
    
    return _run(cmd)

def sha256(path: str) -> ActionResult:
    """Calculate SHA256 checksum of a file"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "command": "", "stdout": "", "stderr": f"File not found: {path}", "artifacts": []}
    
    checksum = _sha256(p)
    return {"ok": True, "command": f"sha256({path})", "stdout": checksum, "stderr": "", "artifacts": [path]}
