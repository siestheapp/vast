#!/usr/bin/env python
"""
Demonstrate VAST's capabilities with the Pagila movie database
"""

from src.vast.conversation import VastConversation
from src.vast.introspect import list_tables
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

console = Console()

def demo():
    console.print(Panel.fit(
        "[bold cyan]VAST Capabilities Demo[/]\n"
        "Showing what VAST can do with your movie database",
        border_style="cyan"
    ))
    
    # Create a fresh session for demo
    vast = VastConversation("capability_demo")
    
    # Demo 1: Analytics
    console.print("\n[bold yellow]1. ANALYTICS - Natural language to SQL[/]")
    console.print("[dim]Question: 'What are the top 3 most rented films?'[/]\n")
    
    response = vast.process(
        "What are the top 3 most rented films? Show the title and rental count.",
        auto_execute=True
    )
    console.print("[green]VAST generated and executed:[/]")
    if vast.last_actions and vast.last_actions[0].get("success"):
        console.print(f"✓ Query returned {vast.last_actions[0].get('count', 0)} results")
    
    # Demo 2: Business Rules
    console.print("\n[bold yellow]2. BUSINESS RULES - Persistent memory[/]")
    vast.add_business_rule("Late fees should never exceed the film rental price")
    vast.add_business_rule("Customers must verify email before first rental")
    console.print("Added business rules that VAST will remember:")
    for rule in vast.context.business_rules[-2:]:
        console.print(f"  • {rule}")
    
    # Demo 3: Schema Design (without execution)
    console.print("\n[bold yellow]3. SCHEMA DESIGN - Intelligent database architecture[/]")
    console.print("[dim]Request: 'Design a film recommendation system'[/]\n")
    
    response = vast.process(
        "Design a recommendation system where we track which customers recommended "
        "which films to other customers. Don't execute it, just show the design.",
        auto_execute=False
    )
    
    # Show a snippet of the response
    console.print("[green]VAST's design:[/]")
    console.print(response[:400] + "...")
    
    # Demo 4: Complex Query
    console.print("\n[bold yellow]4. COMPLEX QUERIES - Multi-table joins[/]")
    console.print("[dim]Question: 'Which store has generated the most revenue?'[/]\n")
    
    response = vast.process(
        "Which store has generated the most revenue from rentals? "
        "Show store ID and total revenue.",
        auto_execute=True
    )
    
    if vast.last_actions and vast.last_actions[0].get("success"):
        console.print(f"✓ Analysis completed successfully")
    
    # Show what's persisted
    console.print("\n[bold cyan]═══ VAST REMEMBERS EVERYTHING ═══[/]")
    console.print(f"📁 Session saved at: [yellow]{vast.session_file}[/]")
    console.print(f"📋 Business rules stored: [green]{len(vast.context.business_rules)}[/]")
    console.print(f"💬 Conversation history: [green]{len(vast.messages)} messages[/]")
    console.print(f"🔍 Database fingerprint: [dim]{vast.context.last_fingerprint[:16]}...[/]")
    
    console.print("\n[bold green]✓ All of this is permanently saved![/]")
    console.print("[dim]Next time you start VAST with session 'capability_demo',")
    console.print("it will remember everything from this conversation.[/]")
    
    # Show the key difference
    console.print("\n[bold]The Key Difference:[/]")
    comparison = Table(title="VAST vs ChatGPT", show_header=True)
    comparison.add_column("Feature", style="cyan")
    comparison.add_column("ChatGPT", style="red")
    comparison.add_column("VAST", style="green")
    
    comparison.add_row(
        "Memory",
        "❌ Forgets after session",
        "✅ Permanent storage"
    )
    comparison.add_row(
        "Database Access",
        "❌ Can't execute SQL",
        "✅ Direct execution"
    )
    comparison.add_row(
        "Business Rules",
        "❌ Lost each time",
        "✅ Enforced consistently"
    )
    comparison.add_row(
        "Schema Awareness",
        "❌ Must re-explain",
        "✅ Auto-tracked"
    )
    
    console.print(comparison)

if __name__ == "__main__":
    try:
        demo()
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/]")
        console.print("[yellow]Make sure your database is running and .env is configured[/]")
