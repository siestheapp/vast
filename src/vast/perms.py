"""Permission management helpers for Vast roles."""

from __future__ import annotations

from typing import Dict, Iterable, List

from sqlalchemy import text


def _quote_ident(identifier: str) -> str:
    if not identifier:
        raise ValueError("Identifier must be a non-empty string")
    escaped = identifier.replace("\"", "\"\"")
    return f'"{escaped}"'


def _grant_statements(schema: str, ro_role: str, rw_role: str) -> List[str]:
    sch = _quote_ident(schema)
    ro = _quote_ident(ro_role)
    rw = _quote_ident(rw_role)

    return [
        f"GRANT USAGE ON SCHEMA {sch} TO {ro}, {rw}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {sch} TO {ro}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {sch} TO {ro}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {sch} TO {rw}",
        f"GRANT REFERENCES ON ALL TABLES IN SCHEMA {sch} TO {rw}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {rw} IN SCHEMA {sch} GRANT SELECT ON TABLES TO {ro}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {rw} IN SCHEMA {sch} GRANT USAGE, SELECT ON SEQUENCES TO {ro}",
    ]


def bootstrap_perms(engine_owner, schema: str, ro_role: str, rw_role: str) -> Dict[str, Iterable[str]]:
    """Grant baseline privileges for Vast roles using the owner connection."""

    statements = _grant_statements(schema, ro_role, rw_role)

    with engine_owner.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))

    return {
        "schema": schema,
        "read_role": ro_role,
        "write_role": rw_role,
        "statements": statements,
    }
