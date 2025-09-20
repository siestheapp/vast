#!/usr/bin/env python
"""
Test VAST with real database operations
"""

from src.vast.conversation import VastConversation
from rich.console import Console

console = Console()

# Start a conversation
vast = VastConversation("test_session")

console.print("\n[bold cyan]=== Testing VAST with Real Database Operations ===[/]\n")

# Test 1: Analytics query
console.print("[yellow]Test 1: Analytics Query[/]")
console.print("[dim]You: Which actors have appeared in the most films?[/]\n")

response = vast.process("Which actors have appeared in the most films? Show me the top 5.", auto_execute=True)
console.print(f"[green]VAST:[/] {response[:500]}...")

# Test 2: Add a business rule
console.print("\n[yellow]Test 2: Business Rule[/]")
console.print("[dim]You: We should track when each film was last rented[/]\n")

vast.add_business_rule("Track last rental date for each film")
response = vast.process("We need to track when each film was last rented. How should we implement this?", auto_execute=False)
console.print(f"[green]VAST:[/] {response[:500]}...")

# Test 3: Schema modification (without executing)
console.print("\n[yellow]Test 3: Schema Design[/]")
console.print("[dim]You: I want to add a customer wishlist feature[/]\n")

response = vast.process("""
I want to add a wishlist feature where customers can save films they want to watch later.
Design the schema for this but don't execute it yet.
""", auto_execute=False)
console.print(f"[green]VAST:[/] {response[:500]}...")

# Show what VAST remembers
console.print("\n[yellow]=== What VAST Remembers ===[/]")
console.print(f"Business Rules: {vast.context.business_rules}")
console.print(f"Session saved at: {vast.session_file}")
console.print("\n[green]✓ VAST will remember all of this for the next conversation![/]")
