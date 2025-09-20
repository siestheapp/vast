#!/usr/bin/env python
"""
Demo of VAST conversational capabilities
Shows how VAST differs from ChatGPT by having persistent memory and direct database access
"""

from src.vast.conversation import VastConversation
from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

console = Console()

def demo():
    """Demonstrate VAST's capabilities"""
    
    console.print(Panel.fit(
        "[bold cyan]VAST Demo - Persistent Database Agent[/]\n\n"
        "This demonstrates how VAST differs from ChatGPT:\n"
        "• Remembers all context between sessions\n"
        "• Can directly modify your database\n"
        "• Acts as your persistent DBA/CTO",
        border_style="cyan"
    ))
    
    # Create a conversation
    vast = VastConversation("demo_session")
    
    # Example 1: Ask about the database
    console.print("\n[bold cyan]Example 1: Understanding the current database[/]")
    console.print("[dim]You: What tables do we have in the database?[/]\n")
    
    response = vast.process("What tables do we have in the database? Give me a brief summary.")
    console.print(f"[green]VAST:[/] {response[:500]}...")
    
    # Example 2: Design decision that will be remembered
    console.print("\n[bold cyan]Example 2: Making a design decision[/]")
    console.print("[dim]You: We should always use soft deletes instead of hard deletes[/]\n")
    
    vast.add_business_rule("Always use soft deletes - never hard delete records")
    response = vast.process("I want to implement soft deletes. What pattern should we use?")
    console.print(f"[green]VAST:[/] {response[:500]}...")
    
    # Example 3: Show that VAST remembers
    console.print("\n[bold cyan]Example 3: VAST remembers everything[/]")
    console.print("[dim]Current business rules VAST remembers:[/]")
    for rule in vast.context.business_rules:
        console.print(f"  • {rule}")
    
    console.print("\n[dim]Schema context VAST maintains:[/]")
    console.print(f"  • Database fingerprint: {vast.context.last_fingerprint}")
    console.print(f"  • Tables tracked: {len(vast.context.schema_summary.split('\\n'))} lines of context")
    
    # Show session persistence
    console.print("\n[bold cyan]Session Persistence[/]")
    console.print(f"[dim]This conversation is saved at: {vast.session_file}[/]")
    console.print("[dim]When you restart, VAST will remember everything from this session.[/]")
    
    # Example of what ChatGPT can't do
    console.print("\n[bold yellow]What makes VAST different from ChatGPT:[/]")
    console.print("""
ChatGPT would:
  ✗ Forget this conversation after it ends
  ✗ Need the entire schema explained again
  ✗ Can't actually execute SQL
  ✗ Lose track of business rules
  
VAST:
  ✓ Remembers everything permanently
  ✓ Maintains live database connection
  ✓ Can execute DDL and DML directly
  ✓ Enforces business rules consistently
  ✓ Acts as your persistent DBA/CTO
""")

if __name__ == "__main__":
    demo()
