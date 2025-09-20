#!/usr/bin/env python
"""
Test VAST's ability to create SQL dumps autonomously
"""

from src.vast.conversation import VastConversation
from rich.console import Console

console = Console()

def test_dump():
    console.print("\n[bold cyan]Testing VAST's SQL Dump Capability[/]\n")
    
    # Create VAST instance
    vast = VastConversation("test_dump_session")
    
    # Test 1: Ask VAST to create a SQL dump
    console.print("[yellow]Test 1: Asking VAST to create a database dump[/]")
    console.print("[dim]User: Can you create a SQL dump of the database?[/]\n")
    
    response = vast.process(
        "Can you create a SQL dump of the database? Please create a backup file with today's date.",
        auto_execute=True
    )
    
    console.print(f"[green]VAST Response:[/] {response[:500]}...")
    
    # Test 2: Ask for a specific table dump
    console.print("\n[yellow]Test 2: Asking VAST to dump specific tables[/]")
    console.print("[dim]User: Create a backup of just the film and actor tables[/]\n")
    
    response = vast.process(
        "Create a backup of just the film and actor tables",
        auto_execute=True
    )
    
    console.print(f"[green]VAST Response:[/] {response[:500]}...")
    
    # Test 3: Ask about the dumps created
    console.print("\n[yellow]Test 3: Checking what dumps were created[/]")
    console.print("[dim]User: What database dumps have you created?[/]\n")
    
    response = vast.process(
        "What database dumps have you created in this session?",
        auto_execute=False  # Just ask, don't execute
    )
    
    console.print(f"[green]VAST Response:[/] {response[:500]}...")

if __name__ == "__main__":
    test_dump()
