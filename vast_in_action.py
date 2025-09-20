#!/usr/bin/env python
"""
VAST in Action - See it work with your database
"""

from src.vast.conversation import VastConversation
from rich.console import Console

console = Console()

# Start VAST
console.print("\n[bold cyan]🚀 VAST in Action - Real Database Conversation[/]\n")

vast = VastConversation("live_demo")

# Example 1: Simple query
console.print("[yellow]You:[/] How many films do we have in the database?")
response = vast.process("How many films are in the database?", auto_execute=True)
console.print(f"[green]VAST:[/] {response}\n")

# Example 2: Complex analysis
console.print("[yellow]You:[/] What's the average rental duration by film category?")
response = vast.process(
    "Show me the average rental duration for each film category. Order by duration.",
    auto_execute=True
)
# Just show first part of response
console.print(f"[green]VAST:[/] {response[:300]}...\n")

# Example 3: Business insight
console.print("[yellow]You:[/] Which customers haven't rented anything in the last year?")
response = vast.process(
    "Find customers who haven't rented any films recently. Just show me the count.",
    auto_execute=True
)
console.print(f"[green]VAST:[/] {response[:300]}...\n")

# Show that VAST remembers
console.print("[bold cyan]═══ VAST Memory Check ═══[/]")
console.print(f"Messages in conversation: {len(vast.messages)}")
console.print(f"Session file: {vast.session_file}")
console.print("\n[green]✓ Everything above is permanently saved![/]")
console.print("[dim]Run this script again and VAST will remember this conversation.[/]")
