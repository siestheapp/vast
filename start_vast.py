#!/usr/bin/env python
"""
Start VAST - Your AI Database Architect

This is the main entry point for having conversations with VAST about your database.
VAST remembers everything and can directly modify your database.
"""

import os
import sys
from pathlib import Path

# Ensure we can import from src
sys.path.insert(0, str(Path(__file__).parent))

from src.vast.conversation import VastConversation
from src.vast.introspect import list_tables
from src.vast.service import environment_status
from api.routers.health import health_full
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

def show_welcome():
    """Show welcome message with database info"""
    tables = list_tables()
    
    env = environment_status().get("vast_env", "dev")
    try:
        health = health_full()
        project = health.get("db", {}).get("project_name")
        if not project:
            project = health.get("db", {}).get("project_ref")
        if not project:
            project = health.get("db", {}).get("database", "Unknown DB")
    except Exception:
        project = os.getenv("VAST_PROJECT_NAME", "Unknown DB")

    # Create a nice welcome panel
    welcome = Panel.fit(
        "[bold cyan]VAST - AI Database Architect[/]\n\n"
        f"Connected to: [yellow]{project}[/] ({env})\n"
        f"Tables: [green]{len(tables)}[/] tables available\n\n"
        "I can:\n"
        "  • Answer questions about your data\n"
        "  • Create new tables and features\n"
        "  • Write complex queries\n"
        "  • Implement business rules\n"
        "  • Remember everything for next time\n\n"
        "[dim]Just talk naturally about what you need![/]",
        border_style="cyan",
        title="Welcome to VAST"
    )
    console.print(welcome)
    
    # Show sample tables
    console.print("\n[bold]Your database includes:[/]")
    sample_tables = tables[:8]
    table_list = ", ".join([f"[cyan]{t['table_name']}[/]" for t in sample_tables])
    console.print(f"  {table_list}, and more...\n")

def main():
    """Main conversation loop"""
    show_welcome()
    
    # Start or resume conversation
    session_name = input("Session name (or press Enter for 'main'): ").strip() or "main"
    
    console.print(f"\n[dim]Starting session: {session_name}[/]\n")
    vast = VastConversation(session_name)
    
    # Check if resuming
    if len(vast.messages) > 1:  # More than just system message
        console.print("[green]✓ Resuming previous conversation[/]")
        console.print("[dim]I remember what we discussed before.[/]\n")
    
    console.print("[bold]Ready! Ask me anything about your database.[/]")
    console.print("[dim]Examples:[/]")
    console.print("[dim]  • 'Show me the top 5 customers by rentals'[/]")
    console.print("[dim]  • 'Add a review system for films'[/]")
    console.print("[dim]  • 'Which films have never been rented?'[/]")
    console.print("[dim]  • 'Create a customer loyalty program'[/]")
    console.print("[dim]Type 'exit' to quit[/]\n")
    
    # Main conversation loop
    while True:
        try:
            # Get input
            user_input = input("\n[You]: ").strip()
            
            if not user_input:
                continue
                
            if user_input.lower() in ['exit', 'quit', 'bye']:
                console.print("\n[cyan]VAST: Great working with you! Everything is saved.[/]")
                break
            
            # Process with VAST
            print("\n[VAST]: ", end="", flush=True)
            
            # Get response (this will show SQL and ask for confirmation if needed)
            response = vast.process(user_input)
            
            # Show the response (without SQL blocks since they're already shown)
            if "```sql" in response:
                # Remove SQL blocks as they were already displayed
                parts = response.split("```sql")
                print(parts[0], end="")
                for part in parts[1:]:
                    if "```" in part:
                        idx = part.find("```")
                        print(part[idx+3:], end="")
                    else:
                        print(part, end="")
                print()  # Final newline
            else:
                print(response)
            
            # Show execution summary if actions were taken
            if vast.last_actions:
                console.print("\n[dim]Actions taken:[/]")
                for action in vast.last_actions:
                    if action.get("success"):
                        if action.get("type") == "ddl":
                            console.print(f"  ✓ Database schema modified")
                        elif action.get("type") == "dml":
                            count = action.get("count", 0)
                            if count > 0:
                                console.print(f"  ✓ Query returned {count} results")
                            else:
                                console.print(f"  ✓ Operation completed")
                    else:
                        console.print(f"  ✗ Error: {action.get('error')}")
                        
        except KeyboardInterrupt:
            console.print("\n\n[yellow]Use 'exit' to quit properly and save your session.[/]")
            continue
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/]")
            console.print("[dim]Don't worry, your conversation is saved. You can continue.[/]")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        console.print(f"\n[red]Fatal error: {e}[/]")
        console.print("[dim]Check your database connection in .env[/]")
        sys.exit(1)
