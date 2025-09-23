#!/usr/bin/env python
"""
VAST Conversational CLI
A persistent, intelligent database agent that remembers everything

Copyright (c) 2024 Sean Davey. All rights reserved.
This software is proprietary and confidential. Unauthorized use is prohibited.
"""

import sys
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown
from rich.panel import Panel
from rich import print

from src.vast.conversation import start_conversation

console = Console()

def main():
    """Main conversation loop"""
    
    # Welcome banner
    console.print(Panel.fit(
        "[bold cyan]VAST - AI Database Architect & Operator[/]\n\n"
        "Unlike ChatGPT, I remember everything about your database.\n"
        "I can design schemas, execute queries, and maintain your data.\n\n"
        "[dim]Type 'help' for commands, 'exit' to quit[/]",
        border_style="cyan"
    ))
    
    # Ask for session name or use default
    session_name = Prompt.ask(
        "\n[cyan]Session name (or press Enter for new session)[/]",
        default=None
    )
    
    # Start conversation
    try:
        vast = start_conversation(session_name)
    except Exception as e:
        console.print(f"[red]Error initializing VAST: {e}[/]")
        return
    
    # Main loop
    while True:
        try:
            # Get user input
            user_input = Prompt.ask("\n[bold cyan]You[/]")
            
            # Check for special commands
            if user_input.lower() in ['exit', 'quit', 'bye']:
                console.print("\n[cyan]VAST: Goodbye! I'll remember everything for next time.[/]")
                break
            
            elif user_input.lower() == 'help':
                console.print(Panel(
                    "[bold]Available Commands:[/]\n\n"
                    "• [cyan]help[/] - Show this help message\n"
                    "• [cyan]history[/] - Show conversation history\n"
                    "• [cyan]schema[/] - Show current database schema\n"
                    "• [cyan]rules[/] - Show business rules\n"
                    "• [cyan]decisions[/] - Show design decisions\n"
                    "• [cyan]clear[/] - Clear the screen\n"
                    "• [cyan]exit[/] - End conversation\n\n"
                    "[dim]Or just talk naturally about your database needs![/]",
                    title="Help",
                    border_style="cyan"
                ))
                continue
            
            elif user_input.lower() == 'history':
                vast.show_history(20)
                continue
            
            elif user_input.lower() == 'schema':
                console.print("\n[bold]Current Schema:[/]")
                console.print(vast.context.schema_summary)
                continue
            
            elif user_input.lower() == 'rules':
                if vast.context.business_rules:
                    console.print("\n[bold]Business Rules:[/]")
                    for i, rule in enumerate(vast.context.business_rules, 1):
                        console.print(f"{i}. {rule}")
                else:
                    console.print("[dim]No business rules defined yet[/]")
                continue
            
            elif user_input.lower() == 'decisions':
                if vast.context.design_decisions:
                    console.print("\n[bold]Design Decisions:[/]")
                    for i, decision in enumerate(vast.context.design_decisions, 1):
                        console.print(f"{i}. {decision}")
                else:
                    console.print("[dim]No design decisions recorded yet[/]")
                continue
            
            elif user_input.lower() == 'clear':
                console.clear()
                continue
            
            # Process normal conversation
            console.print("\n[bold green]VAST:[/] ", end="")
            
            # Get and display response
            response = vast.process(user_input)
            
            # Pretty print the response
            if "```" in response:
                # Has code blocks, print as-is
                console.print(response)
            else:
                # Regular text, use markdown
                md = Markdown(response)
                console.print(md)
            
            # Show execution results if any
            if vast.last_actions:
                console.print("\n[dim]Execution Summary:[/]")
                for action in vast.last_actions:
                    if action["success"]:
                        console.print(f"  ✓ {action.get('type', 'operation')} succeeded")
                    else:
                        console.print(f"  ✗ {action.get('type', 'operation')} failed: {action['error']}")
        
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            if Confirm.ask("\n\n[yellow]Interrupt detected. Exit VAST?[/]"):
                console.print("\n[cyan]VAST: Session saved. See you next time![/]")
                break
            else:
                continue
        
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/]")
            console.print("[dim]Your conversation is still saved. You can continue.[/]")


if __name__ == "__main__":
    main()
