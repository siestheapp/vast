"""
VAST Conversation Engine
A persistent, context-aware database agent that acts as your DBA/CTO
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field, asdict
from enum import Enum

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from .config import settings
from .db import get_engine, safe_execute
from sqlalchemy import text
from .introspect import list_tables, table_columns, schema_fingerprint
from .agent import load_or_build_schema_summary

console = Console()

# Conversation storage path
CONVERSATION_DIR = Path(".vast/conversations")
CONVERSATION_DIR.mkdir(parents=True, exist_ok=True)

class MessageRole(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    EXECUTION = "execution"

@dataclass
class Message:
    role: MessageRole
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self):
        return {
            "role": self.role.value,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data):
        return cls(
            role=MessageRole(data["role"]),
            content=data["content"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=data.get("metadata", {})
        )

@dataclass
class ConversationContext:
    """Persistent context about the database and decisions made"""
    database_url: str
    schema_summary: str
    business_rules: List[str] = field(default_factory=list)
    design_decisions: List[str] = field(default_factory=list)
    naming_patterns: Dict[str, str] = field(default_factory=dict)
    last_fingerprint: Optional[str] = None
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data):
        return cls(**data)

class VastConversation:
    """
    A persistent conversation with memory about your database.
    Unlike ChatGPT, VAST remembers everything and can directly operate your database.
    """
    
    def __init__(self, session_name: str = None):
        self.session_name = session_name or f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.session_file = CONVERSATION_DIR / f"{self.session_name}.json"
        
        self.messages: List[Message] = []
        self.context: ConversationContext = None
        self.last_actions: List[Dict[str, Any]] = []
        
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.engine = get_engine()
        
        # Load or initialize
        if self.session_file.exists():
            self._load_session()
        else:
            self._initialize_session()
    
    def _initialize_session(self):
        """Start a new conversation with fresh context"""
        console.print("\n[bold cyan]🚀 Initializing VAST - Your AI Database Architect[/]")
        
        # Build initial context
        schema_summary = load_or_build_schema_summary()
        self.context = ConversationContext(
            database_url=settings.database_url,
            schema_summary=schema_summary,
            last_fingerprint=schema_fingerprint()
        )
        
        # System message that defines VAST's personality and capabilities
        system_msg = Message(
            role=MessageRole.SYSTEM,
            content=f"""You are VAST, an AI Database Architect and Operator. You are like a senior DBA/CTO who:

1. REMEMBERS everything about this database (schema, decisions, patterns)
2. CAN DIRECTLY MODIFY the database (CREATE, ALTER, INSERT, UPDATE)
3. FOLLOWS business rules and design patterns consistently
4. THINKS LONG-TERM about schema evolution and maintainability

Current Database Context:
{schema_summary}

Your capabilities:
- Design and create database schemas
- Execute DDL operations (CREATE TABLE, ALTER, etc.)
- Insert and update data
- Maintain consistency across complex relationships
- Remember all decisions and patterns for future use

When suggesting database changes:
1. Explain the reasoning
2. Show the SQL that will be executed
3. Consider impacts on existing data
4. Maintain referential integrity

You are conversational but professional. You make decisions like a senior engineer would.
"""
        )
        
        self.messages.append(system_msg)
        self._save_session()
        
        console.print("[green]✓ VAST initialized with current database context[/]")
    
    def _load_session(self):
        """Load an existing conversation from disk"""
        with open(self.session_file, 'r') as f:
            data = json.load(f)
        
        self.messages = [Message.from_dict(m) for m in data["messages"]]
        self.context = ConversationContext.from_dict(data["context"])
        
        # Check if schema has changed
        current_fingerprint = schema_fingerprint()
        if current_fingerprint != self.context.last_fingerprint:
            console.print("[yellow]⚠ Database schema has changed since last session[/]")
            self._refresh_schema_context()
    
    def _save_session(self):
        """Persist conversation to disk"""
        data = {
            "session_name": self.session_name,
            "messages": [m.to_dict() for m in self.messages],
            "context": self.context.to_dict(),
            "last_updated": datetime.now().isoformat()
        }
        
        with open(self.session_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    def _refresh_schema_context(self):
        """Update context when database schema changes"""
        self.context.schema_summary = load_or_build_schema_summary()
        self.context.last_fingerprint = schema_fingerprint()
        
        # Add a note about the schema change
        refresh_msg = Message(
            role=MessageRole.SYSTEM,
            content=f"[Schema Update] The database schema has been refreshed:\n{self.context.schema_summary}"
        )
        self.messages.append(refresh_msg)
        self._save_session()
    
    def _execute_sql(self, sql: str, allow_ddl: bool = False) -> Dict[str, Any]:
        """Execute SQL and return results with safety checks"""
        try:
            # For DDL operations, we need different safety checks
            if allow_ddl:
                # DDL operations (CREATE, ALTER, DROP)
                with self.engine.begin() as conn:
                    result = conn.execute(text(sql))
                    return {
                        "success": True,
                        "type": "ddl",
                        "message": "DDL operation completed successfully",
                        "sql": sql
                    }
            else:
                # DML operations (SELECT, INSERT, UPDATE)
                rows = safe_execute(sql, allow_writes=True, force_write=True)
                return {
                    "success": True,
                    "type": "dml",
                    "rows": rows,
                    "count": len(rows) if isinstance(rows, list) else 0,
                    "sql": sql
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "sql": sql
            }
    
    def _get_llm_response(self, user_input: str) -> str:
        """Get response from LLM with full conversation context"""
        # Build messages for API
        api_messages = []
        
        # Include relevant context
        for msg in self.messages[-20:]:  # Last 20 messages for context
            if msg.role in [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.SYSTEM]:
                api_messages.append({
                    "role": msg.role.value if msg.role != MessageRole.SYSTEM else "system",
                    "content": msg.content
                })
        
        # Add current user input
        api_messages.append({"role": "user", "content": user_input})
        
        # Add current context reminder
        context_reminder = f"""
Remember: You are VAST, with direct database access. Current context:
- Database: {self.context.database_url.split('@')[-1]}  
- Tables: {len(list_tables())} tables in the database
- Business Rules: {', '.join(self.context.business_rules[-3:])} 
- Recent Decisions: {', '.join(self.context.design_decisions[-3:])}
"""
        api_messages.append({"role": "system", "content": context_reminder})
        
        # Get response
        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=api_messages,
            temperature=0.3,
            max_tokens=2000
        )
        
        return response.choices[0].message.content
    
    def process(self, user_input: str, auto_execute: bool = False) -> str:
        """
        Process user input and return VAST's response.
        This is where VAST acts as your DBA/CTO.
        """
        # Add user message to history
        user_msg = Message(role=MessageRole.USER, content=user_input)
        self.messages.append(user_msg)
        
        # Clear last actions
        self.last_actions = []
        
        # Get LLM response
        response = self._get_llm_response(user_input)
        
        # Parse response for SQL blocks and actions
        sql_blocks = self._extract_sql_blocks(response)
        
        # Execute SQL if present and user approves
        if sql_blocks:
            console.print("\n[yellow]📋 Proposed SQL Operations:[/]")
            for i, sql in enumerate(sql_blocks, 1):
                console.print(Panel(Syntax(sql, "sql"), title=f"Operation {i}"))
            
            should_execute = auto_execute
            if not auto_execute:
                try:
                    should_execute = console.input("\n[cyan]Execute these operations? (yes/no): [/]").lower() == 'yes'
                except EOFError:
                    # Non-interactive mode
                    console.print("[dim]Non-interactive mode - skipping execution[/]")
                    should_execute = False
            
            if should_execute:
                for sql in sql_blocks:
                    # Determine if this is DDL
                    is_ddl = any(sql.strip().upper().startswith(cmd) for cmd in ['CREATE', 'ALTER', 'DROP'])
                    
                    result = self._execute_sql(sql, allow_ddl=is_ddl)
                    self.last_actions.append(result)
                    
                    if result["success"]:
                        console.print(f"[green]✓ Executed successfully[/]")
                        exec_msg = Message(
                            role=MessageRole.EXECUTION,
                            content=f"Executed SQL: {sql[:100]}...",
                            metadata=result
                        )
                        self.messages.append(exec_msg)
                    else:
                        console.print(f"[red]✗ Error: {result['error']}[/]")
                        response += f"\n\n⚠️ Execution error: {result['error']}"
                
                # Refresh schema if DDL was executed
                if any(a.get("type") == "ddl" for a in self.last_actions):
                    self._refresh_schema_context()
        
        # Extract and save any business rules or decisions mentioned
        self._extract_context_updates(response)
        
        # Add assistant response to history
        assistant_msg = Message(role=MessageRole.ASSISTANT, content=response)
        self.messages.append(assistant_msg)
        
        # Save session
        self._save_session()
        
        return response
    
    def _extract_sql_blocks(self, text: str) -> List[str]:
        """Extract SQL code blocks from response"""
        sql_blocks = []
        lines = text.split('\n')
        in_sql = False
        current_sql = []
        
        for line in lines:
            if line.strip().startswith('```sql'):
                in_sql = True
                current_sql = []
            elif line.strip() == '```' and in_sql:
                in_sql = False
                if current_sql:
                    sql_blocks.append('\n'.join(current_sql))
            elif in_sql:
                current_sql.append(line)
        
        return sql_blocks
    
    def _extract_context_updates(self, response: str):
        """Extract business rules and design decisions from response"""
        # Simple pattern matching for now
        lower_response = response.lower()
        
        # Look for business rules
        if "business rule" in lower_response or "always" in lower_response or "never" in lower_response:
            # This is simplified - in production you'd use NLP
            pass
        
        # Look for design decisions  
        if "we'll use" in lower_response or "i'll create" in lower_response or "design" in lower_response:
            # Track major decisions
            pass
    
    def show_history(self, last_n: int = 10):
        """Display conversation history"""
        console.print("\n[bold]Conversation History[/]")
        for msg in self.messages[-last_n:]:
            if msg.role == MessageRole.USER:
                console.print(f"\n[cyan]You:[/] {msg.content[:200]}...")
            elif msg.role == MessageRole.ASSISTANT:
                console.print(f"\n[green]VAST:[/] {msg.content[:200]}...")
            elif msg.role == MessageRole.EXECUTION:
                console.print(f"\n[yellow]Executed:[/] {msg.content}")
    
    def add_business_rule(self, rule: str):
        """Add a business rule to remember"""
        self.context.business_rules.append(rule)
        self._save_session()
        console.print(f"[green]✓ Business rule added: {rule}[/]")
    
    def add_design_decision(self, decision: str):
        """Record a design decision"""
        self.context.design_decisions.append(decision)
        self._save_session()
        console.print(f"[green]✓ Design decision recorded: {decision}[/]")


# Convenience function for CLI
def start_conversation(session_name: str = None):
    """Start or resume a VAST conversation"""
    vast = VastConversation(session_name)
    
    console.print("\n[bold cyan]VAST Database Agent[/]")
    console.print("I'm your AI DBA/CTO. I remember everything about your database.")
    console.print("I can design schemas, write queries, and maintain your data.\n")
    
    if vast.messages:
        vast.show_history(5)
        console.print("\n[dim]Conversation resumed from last session[/]\n")
    
    return vast
