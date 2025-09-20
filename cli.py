import json
import typer
from rich import print
from rich.table import Table

from src.vast.config import settings
from src.vast.introspect import list_tables, table_columns
from src.vast.db import safe_execute
from src.vast.agent import plan_sql, plan_sql_with_retry

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
    print({"DATABASE_URL": bool(settings.database_url), "VAST_ENV": settings.env})

@app.command()
def schema():
    tbl = Table(title="Tables")
    tbl.add_column("schema"); tbl.add_column("table")
    for r in list_tables():
        tbl.add_row(r["table_schema"], r["table_name"])
    print(tbl)

@app.command()
def columns(schema: str, table: str):
    tbl = Table(title=f"{schema}.{table} columns")
    tbl.add_column("name"); tbl.add_column("type"); tbl.add_column("nullable"); tbl.add_column("default")
    for c in table_columns(schema, table):
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
    rows = safe_execute(sql, params=p, allow_writes=write, force_write=force_write)
    print(rows)

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
    
    # Use retry logic by default unless --no-retry is specified
    if no_retry:
        sql = plan_sql(q, allow_writes=write, force_refresh_schema=refresh_schema, param_hints=p)
    else:
        sql = plan_sql_with_retry(
            q, 
            allow_writes=write, 
            force_refresh_schema=refresh_schema, 
            param_hints=p,
            max_retries=max_retries
        )
    
    print(f"[bold]SQL:[/]\n{sql}")
    rows = safe_execute(sql, params=p, allow_writes=write, force_write=force_write)
    print(rows)

if __name__ == "__main__":
    app()
