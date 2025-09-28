"""
VAST CLI Module
Command-line interface for VAST operations

Copyright (c) 2024 Sean Davey. All rights reserved.
This software is proprietary and confidential. Unauthorized use is prohibited.
"""

import json
import os
from typing import Iterable, Optional

import typer
from rich import print
from rich.table import Table
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from src.vast import service
from src.vast.actions import build_review_feature_migration
from src.vast.config import get_ro_url, get_rw_url, settings
from src.vast.catalog_pg import (
    load_schema_cards as load_schema_cards_pg,
    schema_fingerprint as catalog_schema_fingerprint,
)
from src.vast.db import get_engine
from src.vast.identifier_guard import IdentifierValidationError, format_identifier_error, load_schema_cache
from src.vast.perms import bootstrap_perms

app = typer.Typer(help="Vast1 — AI DB operator (MVP)")
perms_app = typer.Typer(name="perms", help="Manage Vast database permissions")
app.add_typer(perms_app, name="perms")
catalog_app = typer.Typer(name="catalog", help="Manage schema catalog cache")
app.add_typer(catalog_app, name="catalog")


def _owner_engine(url: str):
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": (
                f"-c statement_timeout={settings.default_statement_timeout_ms}"
                f" -c idle_in_transaction_session_timeout={settings.idle_in_tx_timeout_ms}"
            )
        },
    )

def _parse_params(params_json: str | None) -> dict:
    if not params_json:
        return {}
    try:
        data = json.loads(params_json)
        if not isinstance(data, dict):
            raise ValueError("params must be a JSON object")
        return data
    except Exception as e:
        raise typer.BadParameter(f"Invalid --params JSON: {e}")


def _resolve_roles(ro_role: Optional[str], rw_role: Optional[str]) -> tuple[str, str]:
    ro = ro_role or getattr(settings, "read_role", None) or "vast_ro"
    rw = rw_role or os.getenv("VAST_WRITE_ROLE") or "vast_rw"
    return ro, rw


ASK_ENV_HELP = """
Set required environment variables, for example:
  export DATABASE_URL_RO=postgresql://vast_ro:password@localhost/pagila
  export DATABASE_URL_RW=postgresql://vast_rw:password@localhost/pagila
  # legacy fallback
  export DATABASE_URL=postgresql://vast_ro:password@localhost/pagila
""".strip()

DEMO_ENV_HELP = """
Set required environment variables, for example:
  export DATABASE_URL_RO=postgresql://vast_ro:password@localhost/pagila
  export DATABASE_URL_RW=postgresql://vast_rw:password@localhost/pagila
  export DATABASE_URL_OWNER=postgresql://postgres:password@localhost/pagila
  # legacy fallback
  export DATABASE_URL=postgresql://vast_ro:password@localhost/pagila
""".strip()

DEMO_REFERENCE_TABLES: Iterable[str] = ("public.customer", "public.film")


def _diagnostics_enabled(no_diagnostics: bool) -> bool:
    env_setting = os.getenv("VAST_DIAG")
    if env_setting == "1":
        return True
    if env_setting == "0":
        return False
    return not no_diagnostics


def _print_connection_banner(info: dict[str, object], mode: str) -> None:
    if info.get("error"):
        print(f"[red]Failed to fetch connection info:[/] {info['error']}")
        raise typer.Exit(code=1)

    db = info.get("db") or "unknown"
    user = info.get("whoami") or "unknown"
    host = info.get("host") or "local"
    port = info.get("port") or "?"
    print(f"Using db={db} user={user} host={host} port={port} (mode={mode})")

@perms_app.command("bootstrap")
@app.command("perms:bootstrap")
def perms_bootstrap(
    schema: str = typer.Option(..., "--schema", help="Target schema name (e.g. public)"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
    owner_url: Optional[str] = typer.Option(
        None,
        "--owner-url",
        envvar="DATABASE_URL_OWNER",
        help="Override DATABASE_URL_OWNER for owner connection",
        show_envvar=True,
    ),
    ro_role: Optional[str] = typer.Option(
        None,
        "--ro-role",
        envvar="VAST_READ_ROLE",
        help="Read-only role to grant permissions to",
        show_envvar=True,
    ),
    rw_role: Optional[str] = typer.Option(
        None,
        "--rw-role",
        envvar="VAST_WRITE_ROLE",
        help="Read/write role to grant permissions to",
        show_envvar=True,
    ),
):
    schema = schema.strip()
    if not schema:
        print("[red]Schema name is required.[/]")
        raise typer.Exit(code=1)

    resolved_owner_url = owner_url or os.getenv("DATABASE_URL_OWNER")
    if not resolved_owner_url:
        print("[red]Owner connection URL is required (set DATABASE_URL_OWNER or use --owner-url).[/]")
        raise typer.Exit(code=1)

    resolved_ro, resolved_rw = _resolve_roles(ro_role, rw_role)

    if not yes:
        confirmed = typer.confirm(
            f"Grant privileges on schema '{schema}' to roles '{resolved_ro}' and '{resolved_rw}'?",
            default=False,
        )
        if not confirmed:
            print("[yellow]Aborted.[/]")
            raise typer.Exit(code=0)

    owner_engine = _owner_engine(resolved_owner_url)
    try:
        result = bootstrap_perms(owner_engine, schema, resolved_ro, resolved_rw)
    except Exception as exc:
        print(f"[red]Failed to bootstrap permissions:[/] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        owner_engine.dispose()

    print("[green]Permissions bootstrapped successfully.[/]")
    print(
        f"Schema: {result['schema']} | read_role: {result['read_role']} | write_role: {result['write_role']}"
    )
    for stmt in result["statements"]:
        print(f" - {stmt}")


@app.command()
def env():
    print(service.environment_status())

@app.command()
def schema():
    tbl = Table(title="Tables")
    tbl.add_column("schema"); tbl.add_column("table")
    for r in service.tables():
        tbl.add_row(r["table_schema"], r["table_name"])
    print(tbl)

@app.command()
def columns(schema: str, table: str):
    tbl = Table(title=f"{schema}.{table} columns")
    tbl.add_column("name"); tbl.add_column("type"); tbl.add_column("nullable"); tbl.add_column("default")
    for c in service.columns(schema, table):
        tbl.add_row(c["column_name"], c["data_type"], c["is_nullable"], str(c["column_default"]))
    print(tbl)


@catalog_app.command("build")
def catalog_build():
    cards = load_schema_cards_pg(refresh=True)
    fingerprint = catalog_schema_fingerprint(cards)
    table_count = len(cards)
    column_count = sum(len(card.get("columns") or []) for card in cards.values())
    print(f"Built {table_count} tables, {column_count} columns, fingerprint {fingerprint}")


@catalog_app.command("show")
def catalog_show(
    table: Optional[str] = typer.Argument(None, help="Table name to display (schema.table)"),
):
    cards = load_schema_cards_pg(refresh=False)
    if not cards:
        print("[yellow]No schema cards available. Run `catalog build` first.[/]")
        raise typer.Exit(code=0)

    if table:
        target = table.strip()
        if not target:
            print("[red]Empty table name provided.[/]")
            raise typer.Exit(code=1)
        if "." not in target:
            target = f"public.{target}"
        target_lower = target.lower()
        matched = None
        for key, card in cards.items():
            if key.lower() == target_lower:
                matched = card
                break
        if matched is None:
            print(f"[red]Table '{target}' not found in schema catalog.[/]")
            raise typer.Exit(code=1)
        print(json.dumps(matched, indent=2))
        raise typer.Exit(code=0)

    fingerprint = catalog_schema_fingerprint(cards)
    print(f"Schema catalog contains {len(cards)} tables (fingerprint {fingerprint}).")
    for key in sorted(cards, key=lambda k: (cards[k]["schema"], cards[k]["table"])):
        card = cards[key]
        aliases = ", ".join(card.get("aliases") or [])
        alias_hint = f" aliases: {aliases}" if aliases else ""
        column_count = len(card.get("columns") or [])
        print(f"- {key} ({column_count} columns){alias_hint}")


@app.command("health")
def health(
    no_refresh: bool = typer.Option(
        False,
        "--no-refresh",
        help="Skip schema summary refresh",
    ),
):
    ok = True

    ro_env = os.getenv("DATABASE_URL_RO") or os.getenv("DATABASE_URL")
    rw_env = os.getenv("DATABASE_URL_RW")
    key_env = os.getenv("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", None)

    typer.echo("== Env ==")
    typer.echo(f"RO configured: {'yes' if ro_env else 'no'}")
    typer.echo(f"RW configured: {'yes' if rw_env else 'no (falls back to RO)'}")
    typer.echo(f"OPENAI key   : {'yes' if key_env else 'no'}")
    typer.echo(f"Include schemas: {settings.VAST_SCHEMA_INCLUDE or 'public'}")

    if ro_env:
        try:
            get_ro_url()
        except Exception as exc:
            ok = False
            typer.echo(f"RO configuration error: {exc}")

    if rw_env:
        try:
            get_rw_url()
        except Exception as exc:
            ok = False
            typer.echo(f"RW configuration error: {exc}")

    try:
        ro_engine = get_engine(readonly=True)
        info = service.connection_info(ro_engine)
        if info.get("error"):
            ok = False
            typer.echo(f"DB connect error: {info['error']}")
        else:
            typer.echo("== DB (RO) ==")
            typer.echo(
                f"db={info['db']} user={info['whoami']} host={info['host']} port={info['port']}"
            )
    except SQLAlchemyError as exc:
        ok = False
        typer.echo(f"DB connect failed: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        ok = False
        typer.echo(f"DB connect failed: {exc}")

    if ok and not no_refresh:
        try:
            from src.vast.agent import load_or_build_schema_summary

            typer.echo("Refreshing schema summary…")
            load_or_build_schema_summary(force_refresh=True)
            typer.echo("Schema summary refreshed.")
        except Exception as exc:
            ok = False
            typer.echo(f"Schema refresh failed: {exc}")

    if ok:
        try:
            q = "select current_user, current_database(), inet_server_addr()::text limit 1"
            result = service.plan_and_execute(
                q,
                allow_writes=False,
                refresh_schema=False,
            )
            execution = result.get("execution", {})
            rows = execution.get("rows") or []
            if rows:
                row = rows[0]
                typer.echo("== Smoke read ==")
                typer.echo(str(row))
            else:
                typer.echo("Smoke read returned no rows")
        except Exception as exc:
            ok = False
            typer.echo(f"Smoke read failed: {exc}")

    raise typer.Exit(code=0 if ok else 1)


@app.command("diagnostics")
def diagnostics():
    return health()

@app.command()
def run(
    sql: str,
    params: str = typer.Option(None, "--params", help='JSON dict of named params, e.g. \'{"name":"TEST"}\''),
    write: bool = typer.Option(False, "--write", help="Permit INSERT/UPDATE"),
    force_write: bool = typer.Option(False, "--force-write", help="Actually execute write (otherwise DRY RUN)"),
):
    p = _parse_params(params)
    try:
        result = service.execute_sql(sql, params=p, allow_writes=write, force_write=force_write)
    except IdentifierValidationError as err:
        print(f"[red]{format_identifier_error(err.details)}[/]")
        raise typer.Exit(code=1)

    print(result)

@app.command()
def ask(
    q: str,
    params: str = typer.Option(None, "--params", help='JSON dict of named params to use in SQL'),
    write: bool = typer.Option(False, "--write", help="Permit INSERT/UPDATE"),
    force_write: bool = typer.Option(False, "--force-write", help="Actually execute write (otherwise DRY RUN)"),
    refresh_schema: bool = typer.Option(False, "--refresh-schema", help="Rebuild schema cache"),
    no_retry: bool = typer.Option(False, "--no-retry", help="Disable automatic retry on SQL errors"),
    max_retries: int = typer.Option(2, "--max-retries", help="Maximum retry attempts (default: 2)"),
    no_diagnostics: bool = typer.Option(
        False,
        "--no-diagnostics",
        help="Suppress connection diagnostics",
    ),
    debug: bool = typer.Option(False, "--debug", help="Print timing details for resolver/LLM/db"),
):
    if not (os.getenv("DATABASE_URL_RO") or os.getenv("DATABASE_URL")):
        typer.echo(
            "Missing required env: set DATABASE_URL_RO (or legacy DATABASE_URL).\n"
            "For example:\n"
            "  export DATABASE_URL_RO=postgresql://vast_ro:password@localhost/pagila\n",
            err=True,
        )
        raise typer.Exit(code=1)

    diagnostics_on = _diagnostics_enabled(no_diagnostics)
    ro_engine = get_engine(readonly=True)

    conn_info = service.connection_info(ro_engine)
    if conn_info.get("error"):
        _print_connection_banner(conn_info, "ro")

    try:
        service.probe_read(ro_engine)
    except Exception as exc:
        print(f"[red]Read probe failed:[/] {exc}")
        raise typer.Exit(code=1)

    if diagnostics_on:
        _print_connection_banner(conn_info, "ro")

    p = _parse_params(params)
    try:
        outcome = service.plan_and_execute(
            q,
            params=p,
            allow_writes=write,
            force_write=force_write,
            refresh_schema=refresh_schema,
            retry=not no_retry,
            max_retries=max_retries,
            debug=debug,
        )
    except IdentifierValidationError as err:
        print(f"[red]{format_identifier_error(err.details)}[/]")
        raise typer.Exit(code=1)

    if outcome.get("answer") is not None:
        print(f"[bold]Answer:[/] {outcome['answer']}")

    sql_text = outcome.get("sql")
    if sql_text:
        label = "SQL" if service.looks_like_sql(q) else "Generated SQL"
        print(f"[bold]{label}:[/]\n{sql_text}")
    execution_payload = outcome.get("execution")
    if execution_payload is not None:
        print(execution_payload)

    if debug and outcome.get("meta"):
        print(f"[bold]Timing meta:[/] {outcome['meta']}")


@app.command("demo:writes")
def demo_writes(
    no_diagnostics: bool = typer.Option(
        False,
        "--no-diagnostics",
        help="Suppress connection diagnostics",
    )
) -> None:
    if not (os.getenv("DATABASE_URL_RO") or os.getenv("DATABASE_URL")):
        typer.echo(
            "Missing required env: set DATABASE_URL_RO (or legacy DATABASE_URL).\n"
            "For example:\n"
            "  export DATABASE_URL_RO=postgresql://vast_ro:password@localhost/pagila\n",
            err=True,
        )
        raise typer.Exit(code=1)

    if not os.getenv("DATABASE_URL_RW"):
        typer.echo(
            "Missing required env: set DATABASE_URL_RW for write operations.\n"
            "For example:\n"
            "  export DATABASE_URL_RW=postgresql://vast_rw:password@localhost/pagila\n",
            err=True,
        )
        raise typer.Exit(code=1)

    diagnostics_on = _diagnostics_enabled(no_diagnostics)
    rw_engine = get_engine(readonly=False)
    conn_info = service.connection_info(rw_engine)
    if conn_info.get("error"):
        _print_connection_banner(conn_info, "rw")

    try:
        service.probe_read(rw_engine)
    except Exception as exc:
        print(f"[red]Write-role connection probe failed:[/] {exc}")
        raise typer.Exit(code=1)

    if diagnostics_on:
        _print_connection_banner(conn_info, "rw")

    privilege_report = service.check_reference_privileges(rw_engine, DEMO_REFERENCE_TABLES)
    missing_refs = [entry for entry in privilege_report if not entry.get("has_ref")]
    if missing_refs:
        whoami = conn_info.get("whoami") or "current role"
        for entry in missing_refs:
            print(
                f"[red]Insufficient privileges for {whoami} on {entry['table']} (REFERENCES=false).[/]"
                " Run: python cli.py perms:bootstrap --schema public --yes"
            )
        raise typer.Exit(code=1)

    statements = build_review_feature_migration()
    if not statements:
        print("[red]No demo statements found.[/]")
        raise typer.Exit(code=1)

    print("[bold]Preflight[/bold]")
    try:
        ro_engine = get_engine(readonly=True)
        schema_map, _ = load_schema_cache(ro_engine)
        notes = service.preflight_statements(statements, engine=ro_engine, schema_map=schema_map)
    except IdentifierValidationError as err:
        print(f"[red]{format_identifier_error(err.details)}[/]")
        raise typer.Exit(code=1)
    except Exception as exc:
        print(f"[red]Preflight failed:[/] {exc}")
        raise typer.Exit(code=1)
    else:
        for note in notes:
            print(note)

    print("[bold]Apply[/bold]")
    try:
        ro_engine = get_engine(readonly=True)
        schema_map, _ = load_schema_cache(ro_engine)
        service.apply_statements(statements, engine=ro_engine, schema_map=schema_map)
    except IdentifierValidationError as err:
        print(f"[red]{format_identifier_error(err.details)}[/]")
        raise typer.Exit(code=1)
    except Exception as exc:
        print(f"[red]Apply failed:[/] {exc}")
        raise typer.Exit(code=1)

    print("[bold]Verify[/bold]")
    preview_sql = (
        "SELECT review_id, customer_id, film_id, rating, comment, created_at "
        "FROM public.review ORDER BY review_id DESC LIMIT 1"
    )
    try:
        preview = service.execute_sql(preview_sql)
        rows = preview.get("rows", [])
    except IdentifierValidationError as err:
        print(f"[red]{format_identifier_error(err.details)}[/]")
        raise typer.Exit(code=1)
    except Exception as exc:
        print(f"[red]Verification failed:[/] {exc}")
        raise typer.Exit(code=1)

    tbl = Table(title="public.review preview")
    tbl.add_column("review_id")
    tbl.add_column("customer_id")
    tbl.add_column("film_id")
    tbl.add_column("rating")
    tbl.add_column("comment")
    tbl.add_column("created_at")

    if rows:
        row0 = rows[0]
        if isinstance(row0, dict):
            record = row0
        else:
            record = dict(getattr(row0, "_mapping", row0))
        tbl.add_row(
            str(record.get("review_id")),
            str(record.get("customer_id")),
            str(record.get("film_id")),
            str(record.get("rating")),
            str(record.get("comment")),
            str(record.get("created_at")),
        )
    else:
        tbl.add_row("(none)", "-", "-", "-", "-", "-")

    print(tbl)
    print("[bold green]Demo complete:[/] review feature deployed in a single transaction.")

if __name__ == "__main__":
    app()
