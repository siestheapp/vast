"""
VAST CLI Module
Command-line interface for VAST operations

Copyright (c) 2024 Sean Davey. All rights reserved.
This software is proprietary and confidential. Unauthorized use is prohibited.
"""

import json
import typer
from rich import print
from rich.table import Table

from src.vast import service
from src.vast.actions import build_review_feature_migration
from src.vast.db import get_engine
from src.vast.identifier_guard import IdentifierValidationError, format_identifier_error, load_schema_cache

app = typer.Typer(help="Vast1 — AI DB operator (MVP)")

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
):
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
        )
    except IdentifierValidationError as err:
        print(f"[red]{format_identifier_error(err.details)}[/]")
        raise typer.Exit(code=1)

    label = "SQL" if service.looks_like_sql(q) else "Generated SQL"
    print(f"[bold]{label}:[/]\n{outcome['sql']}")
    print(outcome["execution"])


@app.command("demo:writes")
def demo_writes() -> None:
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
