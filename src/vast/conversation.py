# VAST: patch smoke-test marker (safe to keep or remove)
"""
VAST Conversation Engine
A persistent, context-aware database agent that acts as your DBA/CTO
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import re
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
from .system_ops import SystemOperations
from . import service
from .knowledge import get_knowledge_store

# Ensure we can access the engine with fallback
try:
    from .db import get_engine  # if this file is in a package
except Exception:
    from db import get_engine   # if db.py is at repo root

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
        self.system_ops = SystemOperations()  # Add system operations capability
        
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
            database_url=str(settings.database_url_ro),
            schema_summary=schema_summary,
            last_fingerprint=schema_fingerprint()
        )

        # Capture initial schema snapshot in knowledge store
        try:
            get_knowledge_store().capture_schema_snapshot()
        except Exception as exc:
            console.print(f"[yellow]Knowledge store snapshot failed: {exc}[/]")
        
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
- Execute system commands for DBA operations using ```bash blocks:
  * pg_dump: Create database backups (e.g., pg_dump -f backup.sql pagila)
  * pg_restore: Restore from backups
  * psql: Execute PostgreSQL commands
  * vacuumdb: Perform database maintenance
  * reindexdb: Rebuild indexes
- Generate and apply SQL migration files via the repo helpers (`/repo/write`, `/repo/read`) when changes require persistent scripts.

When suggesting database changes:
1. Explain the reasoning
2. Show the SQL that will be executed (use ```sql blocks)
3. Show system commands when needed (use ```bash blocks)
4. Consider impacts on existing data
5. Maintain referential integrity

Style:
- Answer in concise **Markdown**.
- Prefer short paragraphs, bullet lists (≤7 items), and tables when helpful.
- Use code fences for SQL only.
- No filler text; no repeated disclaimers.

Tone: precise, confident, and free of pleasantries or invitations (no "let me know" or "feel free to ask"). Respond like a senior engineer briefing peers—short, factual sentences, include only necessary detail.

        Authoritative DB Policy:
        - For any question about the CURRENT database (counts, sizes, existence, schema, relationships, examples), you MUST:
          1) Generate a read-only SQL query against the connected database,
          2) Execute it via VAST (no guessing),
          3) Answer ONLY from the query result. Include a compact Markdown table when helpful.
        - Never use phrases like “typically”, “usually”, “likely”, or generic answers about databases.
        - If you cannot run a query, reply: "I need to run a query to determine this." and include the SQL in ```sql fences.

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
        try:
            get_knowledge_store().capture_schema_snapshot(force=True)
        except Exception as exc:
            console.print(f"[yellow]Knowledge store refresh failed: {exc}[/]")
        self._save_session()
    
    def _execute_system_command(self, cmd: str) -> Dict[str, Any]:
        """Execute a system command (like pg_dump)"""
        # Parse the command to determine what type it is
        cmd_parts = cmd.strip().split()
        if not cmd_parts:
            return {"success": False, "error": "Empty command"}
        
        command_name = cmd_parts[0]
        
        # Map common commands to our system operations
        if command_name == 'pg_dump':
            # Prefer unified service path that uses settings.DATABASE_URL and robust defaults
            args = self._parse_pg_dump_args(cmd)
            outfile = args.get('output_file')
            res = service.create_dump(outfile=outfile)
            return {
                "success": bool(res.get("ok")),
                "message": "Database dump created successfully" if res.get("ok") else res.get("stderr", "Dump failed"),
                "output_file": (res.get("artifacts") or [None])[0],
                "command": res.get("command"),
                "stderr": res.get("stderr"),
                "stdout": res.get("stdout"),
            }
        elif command_name == 'pg_restore':
            args = self._parse_pg_restore_args(cmd)
            return self.system_ops.execute_command('pg_restore', args)
        elif command_name == 'vacuumdb':
            args = self._parse_vacuumdb_args(cmd)
            return self.system_ops.execute_command('vacuumdb', args)
        elif command_name == 'reindexdb':
            args = self._parse_reindexdb_args(cmd)
            return self.system_ops.execute_command('reindexdb', args)
        elif command_name == 'psql':
            # For psql commands, extract the -c argument
            args = self._parse_psql_args(cmd)
            return self.system_ops.execute_command('psql', args)
        else:
            return {
                "success": False,
                "error": f"Command '{command_name}' is not supported. Supported commands: pg_dump, pg_restore, psql, vacuumdb, reindexdb"
            }
    
    def _parse_pg_dump_args(self, cmd: str) -> Dict[str, Any]:
        """Parse pg_dump command line arguments"""
        import shlex
        parts = shlex.split(cmd)
        args = {
            'database': 'pagila',  # default
            'host': 'localhost',
            'port': 5433,
            'username': 'vast_ro',
            'password': 'vast_ro_pwd'
        }
        
        i = 1  # Skip 'pg_dump'
        while i < len(parts):
            if parts[i] == '-h' and i + 1 < len(parts):
                args['host'] = parts[i + 1]
                i += 2
            elif parts[i] == '-p' and i + 1 < len(parts):
                args['port'] = int(parts[i + 1])
                i += 2
            elif parts[i] == '-U' and i + 1 < len(parts):
                args['username'] = parts[i + 1]
                i += 2
            elif parts[i] == '-d' and i + 1 < len(parts):
                args['database'] = parts[i + 1]
                i += 2
            elif parts[i] == '-f' and i + 1 < len(parts):
                args['output_file'] = parts[i + 1]
                i += 2
            elif parts[i] == '-F' and i + 1 < len(parts):
                args['format'] = parts[i + 1]
                i += 2
            elif parts[i] == '-t' and i + 1 < len(parts):
                if 'tables' not in args:
                    args['tables'] = []
                args['tables'].append(parts[i + 1])
                i += 2
            elif parts[i] == '--schema-only':
                args['schema_only'] = True
                i += 1
            elif parts[i] == '--data-only':
                args['data_only'] = True
                i += 1
            elif not parts[i].startswith('-'):
                # Positional argument (database name)
                args['database'] = parts[i]
                i += 1
            else:
                i += 1
        
        return args
    
    def _parse_pg_restore_args(self, cmd: str) -> Dict[str, Any]:
        """Parse pg_restore command line arguments"""
        import shlex
        parts = shlex.split(cmd)
        args = {'host': 'localhost', 'port': 5433}
        
        i = 1
        while i < len(parts):
            if parts[i] == '-h' and i + 1 < len(parts):
                args['host'] = parts[i + 1]
                i += 2
            elif parts[i] == '-p' and i + 1 < len(parts):
                args['port'] = int(parts[i + 1])
                i += 2
            elif parts[i] == '-U' and i + 1 < len(parts):
                args['username'] = parts[i + 1]
                i += 2
            elif parts[i] == '-d' and i + 1 < len(parts):
                args['database'] = parts[i + 1]
                i += 2
            elif not parts[i].startswith('-'):
                args['input_file'] = parts[i]
                i += 1
            else:
                i += 1
        
        return args
    
    def _parse_psql_args(self, cmd: str) -> Dict[str, Any]:
        """Parse psql command line arguments"""
        import shlex
        parts = shlex.split(cmd)
        args = {
            'database': 'pagila',
            'host': 'localhost',
            'port': 5433,
            'username': 'vast_ro',
            'password': 'vast_ro_pwd'
        }
        
        i = 1
        while i < len(parts):
            if parts[i] == '-c' and i + 1 < len(parts):
                args['command'] = parts[i + 1]
                i += 2
            elif parts[i] == '-h' and i + 1 < len(parts):
                args['host'] = parts[i + 1]
                i += 2
            elif parts[i] == '-p' and i + 1 < len(parts):
                args['port'] = int(parts[i + 1])
                i += 2
            elif parts[i] == '-U' and i + 1 < len(parts):
                args['username'] = parts[i + 1]
                i += 2
            elif parts[i] == '-d' and i + 1 < len(parts):
                args['database'] = parts[i + 1]
                i += 2
            else:
                i += 1
        
        return args
    
    def _parse_vacuumdb_args(self, cmd: str) -> Dict[str, Any]:
        """Parse vacuumdb command line arguments"""
        import shlex
        parts = shlex.split(cmd)
        args = {'database': 'pagila', 'analyze': False}
        
        i = 1
        while i < len(parts):
            if parts[i] == '-z':
                args['analyze'] = True
                i += 1
            elif parts[i] == '-d' and i + 1 < len(parts):
                args['database'] = parts[i + 1]
                i += 2
            else:
                i += 1
        
        return args
    
    def _parse_reindexdb_args(self, cmd: str) -> Dict[str, Any]:
        """Parse reindexdb command line arguments"""
        import shlex
        parts = shlex.split(cmd)
        args = {'database': 'pagila'}
        
        i = 1
        while i < len(parts):
            if parts[i] == '-d' and i + 1 < len(parts):
                args['database'] = parts[i + 1]
                i += 2
            else:
                i += 1
        
        return args
    
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

        # Add knowledge context if available
        try:
            knowledge_entries = get_knowledge_store().search(user_input, top_k=3)
        except Exception as exc:
            console.print(f"[yellow]Knowledge search failed: {exc}[/]")
            knowledge_entries = []

        # Always remind the model which runtime it is using
        knowledge_entries.insert(0, type("Injected", (), {
            "title": "Runtime",
            "content": f"VAST is running on OpenAI model {settings.openai_model} with embedding model {settings.openai_embedding_model}.",
        })())

        if any(token in user_input.lower() for token in ("dump", "backup", "restore", "pg_dump")):
            try:
                artifacts = service.list_artifacts()
            except Exception as exc:
                console.print(f"[yellow]Artifact listing failed: {exc}[/]")
                artifacts = []

            if artifacts:
                recent = artifacts[-5:]
                knowledge_entries.insert(1, type("Injected", (), {
                    "title": "Database Dumps",
                    "content": "Latest dumps: " + ", ".join(recent),
                })())
            else:
                knowledge_entries.insert(1, type("Injected", (), {
                    "title": "Database Dumps",
                    "content": "No database dumps currently stored in .vast/artifacts.",
                })())

        if knowledge_entries:
            knowledge_blocks = []
            for entry in knowledge_entries:
                knowledge_blocks.append(
                    f"{entry.title}:\n{entry.content}"
                )
            api_messages.append(
                {
                    "role": "system",
                    "content": "Authoritative database knowledge:\n" + "\n\n".join(knowledge_blocks),
                }
            )

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
            model=settings.openai_model,
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
        
        # --- Strict DB-fact routing (grounded only) ---------------------------------
        t = user_input.lower()
        DB_FACT_TRIGGERS = (
            "how many", "count", "size", "largest", "biggest", "rows",
            "tables", "indexes", "connected to", "what database", "schema",
            "columns", "foreign key", "slow queries", "optimiz", "improv",
        )
        if any(k in t for k in DB_FACT_TRIGGERS):
            sections = []

            # identity
            if ("what database" in t) or ("connected to" in t and "database" in t):
                sections.append(self._render_db_identity_markdown())

            # largest tables
            if ("biggest tables" in t) or ("largest" in t and "tables" in t):
                rows = self._biggest_tables(limit=10)
                sections.append(self._render_biggest_tables_markdown(rows))

            # health report / optimization
            if any(k in t for k in ("improv", "optimiz", "performance", "slow queries", "health report")):
                report = self._db_health_report()     # runs real queries
                sections.append(self._render_db_health_markdown(report))

            # database size
            if ("size" in t and ("database" in t or "db" in t)) or ("database size" in t):
                size = self._database_size()
                sections.append(self._render_database_size_markdown(size))

            if sections:
                md = "\n\n---\n\n".join(sections)
                assistant_msg = Message(role=MessageRole.ASSISTANT, content=md)
                self.messages.append(assistant_msg)
                self._save_session()
                return md
        # ---------------------------------------------------------------------------

# MVP: auto-execute dump requests without waiting for the model
        if re.search(r"\b(sql\s+dump|database\s+dump|\bdump\b|\bbackup\b|\bexport\b)", user_input, re.I):
            # Choose plain format if the user asks for something human-readable
            wants_plain = bool(re.search(r"(human[- ]?readable|plain|.sql|text)", user_input, re.I))
            res = service.create_dump(fmt="plain" if wants_plain else "custom")
            ok = bool(res.get("ok"))
            path = (res.get("artifacts") or [None])[0]
            exec_msg = Message(
                role=MessageRole.EXECUTION,
                content=f"Executed system command: pg_dump -> {path if path else 'n/a'}",
                metadata={
                    "success": ok,
                    "output_file": path,
                    "stderr": res.get("stderr"),
                    "stdout": res.get("stdout"),
                    "command": res.get("command"),
                },
            )
            self.last_actions.append(exec_msg.metadata)
            self.messages.append(exec_msg)
            self._save_session()
            if ok:
                return (
                    f"Created a database dump.\n- File: {path}\n- Format: {'plain SQL' if wants_plain else 'custom (.dump)'}\n- Details: sha256 is in the log output."
                )
            else:
                return (
                    "Attempted to create a database dump but it failed.\n- Error: "
                    + (res.get('stderr') or 'unknown error')
                )

        # Get LLM response
        response = self._get_llm_response(user_input)
        
        # Extract all code blocks
        code_blocks = self._extract_code_blocks(response)
        sql_blocks = list(code_blocks.get('sql', []))

        # Reclassify mis-labeled system commands that appear inside ```sql blocks
        misclassified = []
        for sql in sql_blocks:
            s = sql.strip()
            if s.startswith('pg_dump') or s.startswith('pg_restore') or s.startswith('psql') or s.startswith('vacuumdb') or s.startswith('reindexdb'):
                misclassified.append(sql)
        if misclassified:
            for m in misclassified:
                sql_blocks.remove(m)
            code_blocks.setdefault('bash', [])
            code_blocks['bash'].extend(misclassified)
        
        # Execute SQL: auto-run strictly read-only queries, ask for writes/DDL
        if sql_blocks:
            console.print("\n[yellow]📋 Proposed SQL Operations:[/]")
            for i, sql in enumerate(sql_blocks, 1):
                console.print(Panel(Syntax(sql, "sql"), title=f"Operation {i}"))

            for sql in sql_blocks:
                normalized = sql.strip()
                upper = normalized.upper()
                is_ddl = any(upper.startswith(cmd) for cmd in ['CREATE', 'ALTER', 'DROP', 'TRUNCATE'])
                is_write = any(upper.startswith(cmd) for cmd in ['INSERT', 'UPDATE', 'DELETE', 'MERGE'])

                if not is_ddl and not is_write:
                    # Auto-execute read-only (e.g., SELECT, EXPLAIN, SHOW)
                    result = self._execute_sql(normalized, allow_ddl=False)
                    self.last_actions.append(result | {"type": "dml"})
                    if result.get("success"):
                        # Store up to 10 rows in conversation log for provenance
                        if "rows" in result and result["rows"]:
                            serializable_rows = []
                            for row in result["rows"][:10]:
                                try:
                                    if hasattr(row, '_asdict'):
                                        serializable_rows.append(row._asdict())
                                    elif hasattr(row, '_mapping'):
                                        serializable_rows.append(dict(row._mapping))
                                    else:
                                        serializable_rows.append(str(row))
                                except:
                                    serializable_rows.append(str(row))
                            result["rows"] = serializable_rows
                        exec_msg = Message(
                            role=MessageRole.EXECUTION,
                            content=f"Executed read-only SQL: {normalized[:100]}...",
                            metadata=result
                        )
                        self.messages.append(exec_msg)
                    else:
                        console.print(f"[red]✗ Error: {result.get('error')}[/]")
                        response += f"\n\n⚠️ Execution error: {result.get('error')}"
                    continue

                # Writes/DDL require explicit approval unless auto_execute=True
                should_execute = auto_execute
                if not auto_execute:
                    try:
                        prompt = "\n[cyan]This operation modifies schema/data. Execute? (yes/no): [/]"
                        should_execute = console.input(prompt).lower() == 'yes'
                    except EOFError:
                        console.print("[dim]Non-interactive mode - skipping write/DDL execution[/]")
                        should_execute = False

                if should_execute:
                    result = self._execute_sql(normalized, allow_ddl=is_ddl)
                    self.last_actions.append(result)
                    if result.get("success"):
                        console.print("[green]✓ Executed successfully[/]")
                        exec_msg = Message(
                            role=MessageRole.EXECUTION,
                            content=f"Executed SQL: {normalized[:100]}...",
                            metadata=result
                        )
                        self.messages.append(exec_msg)
                    else:
                        console.print(f"[red]✗ Error: {result.get('error')}[/]")
                        response += f"\n\n⚠️ Execution error: {result.get('error')}"

            # Refresh schema if any DDL executed
            if any(a.get("type") == "ddl" for a in self.last_actions):
                self._refresh_schema_context()
        
        # Handle system commands (bash/shell)
        bash_blocks = code_blocks.get('bash', []) + code_blocks.get('system', [])
        if bash_blocks:
            console.print("\n[yellow]🔧 Proposed System Operations:[/]")
            for i, cmd in enumerate(bash_blocks, 1):
                console.print(Panel(Syntax(cmd, "bash"), title=f"System Command {i}"))
            
            should_execute = auto_execute
            if not auto_execute:
                try:
                    should_execute = console.input("\n[cyan]Execute these system commands? (yes/no): [/]").lower() == 'yes'
                except EOFError:
                    console.print("[dim]Non-interactive mode - skipping execution[/]")
                    should_execute = False
            
            if should_execute:
                for cmd in bash_blocks:
                    result = self._execute_system_command(cmd)
                    self.last_actions.append(result)
                    
                    if result["success"]:
                        console.print(f"[green]✓ System command executed successfully[/]")
                        if result.get("output_file"):
                            console.print(f"[cyan]Output file: {result['output_file']}[/]")
                        exec_msg = Message(
                            role=MessageRole.EXECUTION,
                            content=f"Executed system command: {cmd[:100]}...",
                            metadata=result
                        )
                        self.messages.append(exec_msg)
                        response += f"\n\nSystem command executed: {result.get('message', 'Success')}"
                    else:
                        console.print(f"[red]✗ Error: {result['error']}[/]")
                        response += f"\n\n⚠️ System command error: {result['error']}"

        # Autonomous migration execution: if the model proposes a migration file, write and apply it
        if 'migrations/' in response and ('```sql' in response or '```SQL' in response):
            try:
                import re as _re
                path_match = _re.search(r"migrations/[-_a-zA-Z0-9./]+\.sql", response)
                if path_match:
                    path = path_match.group(0)
                    start = response.lower().find('```sql')
                    if start != -1:
                        start += len('```sql')
                        end = response.find('```', start)
                        if end != -1:
                            sql_content = response[start:end].strip()
                            wf = service.repo_write(path, sql_content, overwrite=True)
                            ap = service.apply_sql(path)
                            self.last_actions.append({"success": True if ap.get("ok") else False, "type": "ddl", "file": path})
                            if ap.get("ok"):
                                response += f"\n\nApplied migration: {path}"
                            else:
                                response += f"\n\n⚠️ Failed to apply migration {path}: {ap.get('stderr')}"
            except Exception as _e:
                response += f"\n\n⚠️ Migration auto-apply encountered an error: {_e}"
        
        # Extract and save any business rules or decisions mentioned
        self._extract_context_updates(response)
        
        # Add assistant response to history
        if self._is_db_fact_question(user_input) and not self._grounding_evidence_present_this_turn():
            # refuse generic answer and force a query proposal
            response = ("I need to run a query to determine this.\n\n"
                        "```sql\n-- I will generate and execute the appropriate SELECT against the connected database\n```")

        # Optional: block hedgy language on DB facts
        if self._is_db_fact_question(user_input) and self._HEDGES.search(response or ""):
            response = response.replace("typically", "").replace("usually", "").replace("likely", "")

        response = self._enforce_grounding(user_input, response)
        assistant_msg = Message(role=MessageRole.ASSISTANT, content=response)
        self.messages.append(assistant_msg)
        
        # Save session
        self._save_session()
        
        return response
    
    def _extract_code_blocks(self, text: str) -> Dict[str, List[str]]:
        """Extract SQL and system command blocks from the response"""
        blocks = {"sql": [], "bash": [], "system": []}
        lines = text.split('\n')
        in_block = False
        block_type = None
        current_block = []
        
        for line in lines:
            if line.strip().startswith('```sql'):
                in_block = True
                block_type = 'sql'
                current_block = []
            elif line.strip().startswith('```bash') or line.strip().startswith('```shell'):
                in_block = True
                block_type = 'bash'
                current_block = []
            elif line.strip().startswith('```system'):
                in_block = True
                block_type = 'system'
                current_block = []
            elif line.strip() == '```' and in_block:
                in_block = False
                if current_block and block_type:
                    blocks[block_type].append('\n'.join(current_block))
                current_block = []
                block_type = None
            elif in_block:
                current_block.append(line)
        
        return blocks
    
    def _extract_sql_blocks(self, text: str) -> List[str]:
        """Extract SQL code blocks from response (backward compatibility)"""
        blocks = self._extract_code_blocks(text)
        return blocks.get('sql', [])
    
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

    # ------------------------ Grounded helpers -------------------------------
    def _biggest_tables(self, limit: int = 10):
        """Return largest tables by total size using pg_total_relation_size."""
        sql = """
        SELECT
          n.nspname AS schema,
          c.relname AS table,
          pg_total_relation_size(c.oid) AS total_bytes,
          pg_relation_size(c.oid)      AS table_bytes,
          COALESCE(pg_stat_get_live_tuples(c.oid), 0) AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT :limit;
        """
        with get_engine(readonly=True).begin() as conn:
            rows = conn.execute(text(sql), {"limit": limit}).mappings().all()
        return rows

    def _render_biggest_tables_markdown(self, rows) -> str:
        def fmt_bytes(b):
            units = ["B","KB","MB","GB","TB","PB"]
            i = 0
            b = float(b or 0)
            while b >= 1024 and i < len(units)-1:
                b /= 1024.0
                i += 1
            return f"{b:.0f} {units[i]}"
        lines = [
            "Here are the largest tables **in the connected database** (by total size):",
            "",
            "| # | schema.table | size | rows |",
            "|---|--------------|------|------|",
        ]
        for i, r in enumerate(rows, 1):
            fq = f"{r['schema']}.{r['table']}"
            lines.append(f"| {i} | `{fq}` | {fmt_bytes(r['total_bytes'])} | {int(r['approx_rows'])} |")
        return "\n".join(lines)

    def _database_size(self):
        sql = """
        SELECT
          pg_size_pretty(pg_database_size(current_database())) AS database_size_pretty,
          pg_database_size(current_database())                AS database_size_bytes
        """
        with get_engine(readonly=True).begin() as conn:
            row = conn.execute(text(sql)).mappings().first()
        return row or {"database_size_pretty": None, "database_size_bytes": None}

    def _render_database_size_markdown(self, row) -> str:
        pretty = row.get("database_size_pretty") if isinstance(row, dict) else row["database_size_pretty"]
        bytes_ = row.get("database_size_bytes") if isinstance(row, dict) else row["database_size_bytes"]
        return (
            "### Database size\n"
            "| pretty | bytes |\n"
            "|--------|-------|\n"
            f"| {pretty} | {bytes_} |"
        )

    # --- Live DB identity (no guessing) ---
    def _render_db_identity_markdown(self) -> str:
        sql = """
        SELECT
          current_database()               AS database,
          inet_server_addr()::text         AS host,
          inet_server_port()               AS port,
          version()                        AS version
        """
        with get_engine(readonly=True).begin() as conn:
            row = conn.execute(text(sql)).mappings().first()
        db = row["database"] or "unknown"
        host = row["host"] or "localhost"
        port = row["port"]
        ver = row["version"].split(" on ", 1)[0]
        return (
            f"I am connected to **{db}** at **{host}:{port}**.\n\n"
            f"_PostgreSQL: {ver}_"
        )

    def _db_health_report(self):
        """
        Collect high-signal read-only diagnostics from Postgres.
        Returns a dict of result lists. All queries run against the connected DB.
        """
        out = {}
        with get_engine(readonly=True).begin() as conn:
            # Largest tables by total size
            out["largest_tables"] = conn.execute(text("""
                SELECT
                  n.nspname AS schema,
                  c.relname AS table,
                  pg_total_relation_size(c.oid) AS total_bytes,
                  COALESCE(pg_stat_get_live_tuples(c.oid), 0) AS approx_rows
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                ORDER BY pg_total_relation_size(c.oid) DESC
                LIMIT 10
            """)).mappings().all()

            # Tables with heavy seq scans (likely missing/weak indexes)
            out["seq_scan_heavy"] = conn.execute(text("""
                SELECT
                  relname AS table,
                  seq_scan,
                  idx_scan,
                  n_live_tup
                FROM pg_stat_user_tables
                ORDER BY seq_scan DESC
                LIMIT 10
            """)).mappings().all()

            # Unused or rarely used indexes (idx_scan = 0)
            out["unused_indexes"] = conn.execute(text("""
                SELECT
                  s.relname AS table,
                  i.indexrelname AS index,
                  pg_relation_size(i.indexrelid) AS index_bytes,
                  COALESCE(x.idx_scan, 0) AS idx_scan
                FROM pg_class c
                JOIN pg_index i ON i.indrelid = c.oid
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_stat_user_indexes x ON x.indexrelid = i.indexrelid
                JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE x.idx_scan = 0
                ORDER BY pg_relation_size(i.indexrelid) DESC
                LIMIT 10
            """)).mappings().all()

            # Dead tuples (vacuum pressure)
            out["dead_tuples"] = conn.execute(text("""
                SELECT
                  relname AS table,
                  n_live_tup,
                  n_dead_tup
                FROM pg_stat_user_tables
                WHERE n_dead_tup > 0
                ORDER BY n_dead_tup DESC
                LIMIT 10
            """)).mappings().all()

            # Cache hit ratio (overall)
            out["cache_hit"] = conn.execute(text("""
                SELECT
                  ROUND(100.0 * SUM(blks_hit) / NULLIF(SUM(blks_hit + blks_read),0), 2) AS cache_hit_pct
                FROM pg_stat_database
            """)).mappings().first()

            # Autovacuum key settings (for visibility & tuning)
            out["autovacuum_settings"] = conn.execute(text("""
                SELECT name, setting
                FROM pg_settings
                WHERE name IN (
                  'autovacuum', 'autovacuum_naptime', 'autovacuum_vacuum_scale_factor',
                  'autovacuum_analyze_scale_factor', 'autovacuum_vacuum_threshold',
                  'autovacuum_analyze_threshold'
                )
                ORDER BY name
            """)).mappings().all()

        return out

    def _render_db_health_markdown(self, r) -> str:
        def fmt_bytes(b):
            units = ["B","KB","MB","GB","TB","PB"]; i=0; b=float(b or 0)
            while b>=1024 and i<len(units)-1: b/=1024.0; i+=1
            return f"{b:.0f} {units[i]}"

        lines = []

        # Summary bullets (prioritized)
        cache = r.get("cache_hit", {}) or {}
        cache_pct = cache.get("cache_hit_pct")
        if cache_pct is not None:
            lines.append(f"**Buffer cache hit ratio:** {cache_pct}%")

        if r["seq_scan_heavy"]:
            t = r["seq_scan_heavy"][0]
            lines.append(f"**High seq scans:** `{t['table']}` (seq_scan={t['seq_scan']}, idx_scan={t['idx_scan']}) → likely index gaps.")

        if r["dead_tuples"]:
            dt = r["dead_tuples"][0]
            lines.append(f"**Dead tuples pressure:** `{dt['table']}` has {dt['n_dead_tup']} dead vs {dt['n_live_tup']} live tuples → vacuum/auto-vacuum tuning.")

        lines.append("")
        lines.append("### Largest tables (by size)")
        lines.append("| # | table | size | approx_rows |")
        lines.append("|---|-------|------|-------------|")
        for i, row in enumerate(r["largest_tables"], 1):
            fq = f"{row['schema']}.{row['table']}"
            lines.append(f"| {i} | `{fq}` | {fmt_bytes(row['total_bytes'])} | {int(row['approx_rows'])} |")

        if r["seq_scan_heavy"]:
            lines.append("")
            lines.append("### Tables with heavy sequential scans")
            lines.append("| table | seq_scan | idx_scan | live_rows |")
            lines.append("|-------|----------|----------|-----------|")
            for row in r["seq_scan_heavy"]:
                lines.append(f"| `{row['table']}` | {row['seq_scan']} | {row['idx_scan']} | {row['n_live_tup']} |")

        if r["unused_indexes"]:
            lines.append("")
            lines.append("### Unused / rarely used indexes (idx_scan = 0)")
            lines.append("| table | index | size | idx_scan |")
            lines.append("|-------|-------|------|----------|")
            for row in r["unused_indexes"]:
                lines.append(f"| `{row['table']}` | `{row['index']}` | {fmt_bytes(row['index_bytes'])} | {row['idx_scan']} |")

        if r["dead_tuples"]:
            lines.append("")
            lines.append("### Dead tuples (vacuum pressure)")
            lines.append("| table | dead_tup | live_tup |")
            lines.append("|-------|----------|----------|")
            for row in r["dead_tuples"]:
                lines.append(f"| `{row['table']}` | {row['n_dead_tup']} | {row['n_live_tup']} |")

        if r["autovacuum_settings"]:
            lines.append("")
            lines.append("### Autovacuum settings (current)")
            lines.append("| name | value |")
            lines.append("|------|-------|")
            for s in r["autovacuum_settings"]:
                lines.append(f"| `{s['name']}` | `{s['setting']}` |")

        # Actionable recommendations tied to evidence
        recs = []
        if r["seq_scan_heavy"]:
            recs.append("Add/verify indexes for the high **seq_scan** tables above (start with the top 3).")
        if r["unused_indexes"]:
            recs.append("Drop or justify **unused indexes** (idx_scan=0) after verifying in production traffic.")
        if r["dead_tuples"]:
            recs.append("Tune **autovacuum** and schedule manual `VACUUM (VERBOSE, ANALYZE)` for the top dead-tuple tables.")
        if cache_pct is not None and cache_pct < 95:
            recs.append("Cache hit ratio <95%: review working-set size, increase memory, or optimize repeated scans.")

        if recs:
            lines.insert(0, "#### Recommended next steps (based on live metrics)")
            lines.insert(1, "\n".join([f"- {x}" for x in recs]) + "\n")

        return "\n".join(lines)

    # --- Grounding enforcement (blocks "typically/likely/usually" on DB facts) ---
    import re as _re
    _HEDGES = _re.compile(r"\b(typically|usually|likely|generally|commonly|often|might|may)\b", _re.I)

    def _is_db_fact_question(self, s: str) -> bool:
        s = s.lower()
        return any(k in s for k in [
            "how many","count","size","largest","biggest","rows","tables","indexes",
            "what database","connected to","schema","columns","foreign key","slow queries","optimiz","improv"
        ])

    def _grounding_evidence_present_this_turn(self) -> bool:
        # Minimal: if any of our grounded helpers ran, we would have pushed an action
        # or you can track a flag when you call get_engine/execute in this turn.
        return any(a.get("type") in {"sql_query","health_report","biggest_tables","db_identity"}
                   for a in (self.last_actions or []))

    def _enforce_grounding(self, user_input: str, response: str) -> str:
        if not response:
            return response
        if self._is_db_fact_question(user_input) and self._HEDGES.search(response):
            return ("I need to run a query to determine this.\n\n"
                    "```sql\n-- VAST will generate and execute the appropriate SELECT against the connected database\n```")
        return response


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
