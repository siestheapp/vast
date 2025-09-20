#!/usr/bin/env python
"""
Interactive VAST Demo - Have a real conversation with your database
"""

import sys
from rich.console import Console
from rich.prompt import Prompt
from rich.panel import Panel
from rich.markdown import Markdown

from src.vast.conversation import VastConversation

console = Console()

def main():
    """Run an interactive VAST session"""
    
    console.print(Panel.fit(
        "[bold cyan]VAST - Your AI Database Architect[/]\n\n"
        "Let's have a real conversation about your database.\n"
        "I can understand your needs and actually modify the database.\n\n"
        "[yellow]Connected to: Pagila Movie Database[/]\n"
        "[dim]Type 'exit' to quit[/]",
        border_style="cyan"
    ))
    
    # Start conversation
    vast = VastConversation("movie_db_session")
    
    # Give some context
    console.print("\n[green]VAST:[/] Hello! I'm connected to your Pagila movie database.")
    console.print("I can see you have a film rental business with movies, customers, and stores.")
    console.print("What would you like to work on? I can:")
    console.print("  • Analyze your current data")
    console.print("  • Add new features to the database")
    console.print("  • Create reports and queries")
    console.print("  • Implement business rules")
    console.print("\nJust talk to me naturally about what you need!\n")
    
    while True:
        try:
            # Get user input
            user_input = input("\n[You]: ")
            
            if user_input.lower() in ['exit', 'quit', 'bye']:
                console.print("\n[cyan]VAST: Great working with you! I'll remember everything for next time.[/]")
                break
            
            # Process with VAST
            console.print("\n[green]VAST:[/] ", end="")
            response = vast.process(user_input)
            
            # Display response
            if "```sql" in response:
                # Has SQL, show it nicely
                parts = response.split("```sql")
                console.print(parts[0])
                for part in parts[1:]:
                    sql_end = part.find("```")
                    if sql_end > -1:
                        sql = part[:sql_end]
                        rest = part[sql_end+3:]
                        console.print(Panel(sql, title="SQL", border_style="yellow"))
                        console.print(rest)
                    else:
                        console.print(part)
            else:
                console.print(response)
                
        except KeyboardInterrupt:
            console.print("\n\n[yellow]Interrupted. Type 'exit' to quit or continue talking.[/]")
            continue
        except EOFError:
            break
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/]")

if __name__ == "__main__":
    main()
