"""
VAST System Operations Module
Enables VAST to perform DBA operations like dumps, restores, and maintenance
"""

import subprocess
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from rich.console import Console

console = Console()

class SystemOperations:
    """Handle system-level database operations for VAST"""
    
    def __init__(self):
        self.operations_log = []
        self.allowed_commands = {
            'pg_dump': self._pg_dump,
            'pg_restore': self._pg_restore,
            'psql': self._psql_command,
            'pg_dumpall': self._pg_dumpall,
            'vacuumdb': self._vacuumdb,
            'reindexdb': self._reindexdb,
            'pg_basebackup': self._pg_basebackup,
        }
    
    def execute_command(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a system command safely
        
        Args:
            command: The command to execute (must be in allowed_commands)
            args: Arguments for the command
            
        Returns:
            Dict with success status, output, and any errors
        """
        if command not in self.allowed_commands:
            return {
                "success": False,
                "error": f"Command '{command}' is not allowed. Available commands: {list(self.allowed_commands.keys())}"
            }
        
        try:
            result = self.allowed_commands[command](args)
            self._log_operation(command, args, result)
            return result
        except Exception as e:
            error_result = {
                "success": False,
                "error": str(e)
            }
            self._log_operation(command, args, error_result)
            return error_result
    
    def _pg_dump(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a database dump using pg_dump
        
        Args:
            database: Database name
            output_file: Output file path
            format: Output format (c=custom, p=plain, d=directory, t=tar)
            host: Database host
            port: Database port
            username: Database username
            password: Database password (will be set as env var)
            tables: Optional list of specific tables to dump
            schema_only: If True, dump only schema
            data_only: If True, dump only data
        """
        database = args.get('database', 'pagila')
        output_file = args.get('output_file', f'dump_{database}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.sql')
        format_type = args.get('format', 'p')  # plain SQL by default
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'vast_ro')
        password = args.get('password', 'vast_ro_pwd')
        tables = args.get('tables', [])
        schema_only = args.get('schema_only', False)
        data_only = args.get('data_only', False)
        
        # Build command
        cmd = [
            'pg_dump',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-d', database,
            '-f', output_file,
            '--no-owner',
            '--no-acl'
        ]
        
        # Add format
        if format_type != 'p':
            cmd.extend(['-F', format_type])
        
        # Add schema/data options
        if schema_only:
            cmd.append('--schema-only')
        elif data_only:
            cmd.append('--data-only')
        
        # Add specific tables
        for table in tables:
            cmd.extend(['-t', table])
        
        # Set password as environment variable
        env = os.environ.copy()
        env['PGPASSWORD'] = password
        
        console.print(f"[yellow]Executing: {' '.join(cmd)}[/]")
        
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.returncode == 0:
                # Check if file was created
                if Path(output_file).exists():
                    file_size = Path(output_file).stat().st_size
                    return {
                        "success": True,
                        "message": f"Database dump created successfully",
                        "output_file": output_file,
                        "file_size": file_size,
                        "command": ' '.join(cmd)
                    }
                else:
                    return {
                        "success": False,
                        "error": "Command completed but output file was not created",
                        "stderr": result.stderr
                    }
            else:
                # Parse common errors
                error_msg = f"pg_dump failed with return code {result.returncode}"
                if "version mismatch" in result.stderr:
                    error_msg = "Version mismatch between pg_dump client and server. This is a known limitation - the pg_dump version must match the server version."
                elif "permission denied" in result.stderr.lower():
                    error_msg = "Permission denied. Check database user privileges."
                elif "could not connect" in result.stderr.lower():
                    error_msg = "Could not connect to database. Check connection parameters."
                
                return {
                    "success": False,
                    "error": error_msg,
                    "stderr": result.stderr,
                    "command": ' '.join(cmd),
                    "note": "Consider using COPY commands as an alternative for data export"
                }
                
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Command timed out after 5 minutes"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute pg_dump: {str(e)}"
            }
    
    def _pg_restore(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Restore a database from a dump file"""
        input_file = args.get('input_file')
        database = args.get('database')
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'postgres')
        password = args.get('password')
        
        if not input_file or not Path(input_file).exists():
            return {
                "success": False,
                "error": f"Input file '{input_file}' does not exist"
            }
        
        cmd = [
            'pg_restore',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-d', database,
            '--no-owner',
            '--no-acl',
            input_file
        ]
        
        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password
        
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            
            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Database restored successfully from {input_file}"
                }
            else:
                return {
                    "success": False,
                    "error": f"pg_restore failed",
                    "stderr": result.stderr
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute pg_restore: {str(e)}"
            }
    
    def _psql_command(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a psql command"""
        command = args.get('command')
        database = args.get('database', 'pagila')
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'vast_ro')
        password = args.get('password', 'vast_ro_pwd')
        
        if not command:
            return {
                "success": False,
                "error": "No command provided"
            }
        
        cmd = [
            'psql',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-d', database,
            '-c', command
        ]
        
        env = os.environ.copy()
        env['PGPASSWORD'] = password
        
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            
            return {
                "success": result.returncode == 0,
                "output": result.stdout,
                "error": result.stderr if result.returncode != 0 else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute psql: {str(e)}"
            }
    
    def _pg_dumpall(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dump all databases"""
        output_file = args.get('output_file', f'dumpall_{datetime.now().strftime("%Y%m%d_%H%M%S")}.sql')
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'postgres')
        password = args.get('password')
        
        cmd = [
            'pg_dumpall',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-f', output_file
        ]
        
        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password
        
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            
            if result.returncode == 0 and Path(output_file).exists():
                return {
                    "success": True,
                    "message": f"All databases dumped to {output_file}",
                    "output_file": output_file,
                    "file_size": Path(output_file).stat().st_size
                }
            else:
                return {
                    "success": False,
                    "error": "pg_dumpall failed",
                    "stderr": result.stderr
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute pg_dumpall: {str(e)}"
            }
    
    def _vacuumdb(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run vacuum on database"""
        database = args.get('database', 'pagila')
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'postgres')
        password = args.get('password')
        analyze = args.get('analyze', True)
        
        cmd = [
            'vacuumdb',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-d', database
        ]
        
        if analyze:
            cmd.append('-z')  # Also run ANALYZE
        
        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password
        
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
            
            return {
                "success": result.returncode == 0,
                "message": f"Vacuum {'and analyze ' if analyze else ''}completed on {database}",
                "output": result.stdout,
                "error": result.stderr if result.returncode != 0 else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute vacuumdb: {str(e)}"
            }
    
    def _reindexdb(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Reindex database"""
        database = args.get('database', 'pagila')
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'postgres')
        password = args.get('password')
        
        cmd = [
            'reindexdb',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-d', database
        ]
        
        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password
        
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
            
            return {
                "success": result.returncode == 0,
                "message": f"Reindex completed on {database}",
                "output": result.stdout,
                "error": result.stderr if result.returncode != 0 else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute reindexdb: {str(e)}"
            }
    
    def _pg_basebackup(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Create a base backup of the database cluster"""
        output_dir = args.get('output_dir', f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
        host = args.get('host', 'localhost')
        port = args.get('port', 5433)
        username = args.get('username', 'postgres')
        password = args.get('password')
        
        # Create output directory
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        cmd = [
            'pg_basebackup',
            '-h', str(host),
            '-p', str(port),
            '-U', username,
            '-D', output_dir,
            '-Ft',  # tar format
            '-z',   # gzip
            '-P'    # show progress
        ]
        
        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password
        
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
            
            if result.returncode == 0:
                return {
                    "success": True,
                    "message": f"Base backup created in {output_dir}",
                    "output_dir": output_dir
                }
            else:
                return {
                    "success": False,
                    "error": "pg_basebackup failed",
                    "stderr": result.stderr
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to execute pg_basebackup: {str(e)}"
            }
    
    def _log_operation(self, command: str, args: Dict[str, Any], result: Dict[str, Any]):
        """Log operation for audit trail"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "command": command,
            "args": args,
            "result": result
        }
        self.operations_log.append(log_entry)
        
        # Also save to file
        log_file = Path(".vast/operations_log.json")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        
        if log_file.exists():
            with open(log_file, 'r') as f:
                logs = json.load(f)
        else:
            logs = []
        
        logs.append(log_entry)
        
        with open(log_file, 'w') as f:
            json.dump(logs, f, indent=2, default=str)
    
    def list_available_operations(self) -> List[str]:
        """List all available system operations"""
        return list(self.allowed_commands.keys())
    
    def get_operation_help(self, command: str) -> str:
        """Get help text for a specific operation"""
        if command in self.allowed_commands:
            return self.allowed_commands[command].__doc__
        return f"Unknown command: {command}"
