import pyodbc
import os
import json
import argparse
import sys
import asyncio
from typing import Any, Sequence

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
import uvicorn
import re

# Parse command-line arguments
parser = argparse.ArgumentParser(description="MS Access Explorer MCP Server")
parser.add_argument("--db-path", type=str, action='append', dest='db_paths', 
                   help="Path to Access database file (.accdb or .mdb). Use multiple times for multiple databases.")
parser.add_argument('--db-password', type=str, action='append', dest='db_passwords', 
                   default=[], help='Password for each database. Must match the order of --db-path arguments.')
parser.add_argument("--db-name", type=str, action='append', dest='db_names',
                   help="Friendly name for the database. Must match the order of --db-path arguments.")
parser.add_argument("--db-desc", type=str, action='append', dest='db_descriptions',
                   help="Description for the database. Must match the order of --db-path arguments.")
parser.add_argument('--transport', default='stdio', choices=['stdio', 'sse'], help='Transport method (default: stdio)')
parser.add_argument('--host', default='127.0.0.1', help='Host for SSE (default: 127.0.0.1)')
parser.add_argument('--port', type=int, default=8000, help='Port for SSE (default: 8000)')
parser.add_argument('--path', default='/sse', help='Path for SSE endpoint (default: /sse)')
parser.add_argument('--cors-origin', default='*', help='CORS allowed origins (default: *)')
parser.add_argument('--auth-header', type=str, help='Required Authorization header value for authentication')
args = parser.parse_args()

# Get the database paths
db_paths = args.db_paths or []
db_passwords = args.db_passwords or []
db_names = args.db_names or []
db_descriptions = args.db_descriptions or []

if not db_paths:
    print("Error: No database paths provided. Use --db-path argument.")
    print("Example: ms-access-mcp --db-path \"C:\\path\\to\\database1.accdb\" --db-path \"C:\\path\\to\\database2.accdb\"")
    print("With names: ms-access-mcp --db-path \"db1.accdb\" --db-name \"Sales\" --db-path \"db2.accdb\" --db-name \"Inventory\"")
    print("Full example: ms-access-mcp --db-path \"sales.accdb\" --db-name \"Sales\" --db-desc \"Sales and customer data\" --db-path \"inventory.accdb\" --db-name \"Inventory\" --db-desc \"Product inventory database\"")
    print("SSE with auth: ms-access-mcp --transport sse --cors-origin \"https://myapp.com\" --auth-header \"Bearer your-token-here\" --db-path \"db.accdb\"")
    sys.exit(1)

# Create database registry
databases = {}
for i, db_path in enumerate(db_paths):
    # Verify the database exists
    if not os.path.exists(db_path):
        print(f"Error: Database file not found at {db_path}")
        sys.exit(1)
    
    # Use provided name or generate one
    if i < len(db_names) and db_names[i]:
        db_key = db_names[i]
    else:
        db_key = f"db_{i+1}"
    
    # Use provided description or default
    if i < len(db_descriptions) and db_descriptions[i]:
        db_desc = db_descriptions[i]
    else:
        db_desc = f"Database {db_key}"
    
    databases[db_key] = {
        'path': db_path,
        'description': db_desc
    }
    print(f"Registered database '{db_key}': {db_path}")
    print(f"  Description: {db_desc}")

# Set default database (first one)
default_db_key = list(databases.keys())[0]
print(f"Default database: {default_db_key}")

# Initialize the MCP server
server = Server("MS Access Explorer")

class AuthorizationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, required_auth_header: str = None):
        super().__init__(app)
        self.required_auth_header = required_auth_header
    
    async def dispatch(self, request: Request, call_next):
        # Skip auth check if no auth header is required
        if not self.required_auth_header:
            return await call_next(request)
        
        # Skip auth check for OPTIONS requests (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)
        
        # Check for Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"error": "Missing Authorization header"}
            )
        
        # Validate the Authorization header
        if auth_header != self.required_auth_header:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid Authorization header"}
            )
        
        return await call_next(request)

def get_database_path(db_name: str = None) -> tuple[str, str]:
    """Get database path and key from name
    
    Returns:
        tuple: (db_key, db_path)
    """
    if not db_name:
        db_key = default_db_key
        db_path = databases[default_db_key]['path']
    elif db_name in databases:
        db_key = db_name
        db_path = databases[db_name]['path']
    else:
        raise ValueError(f"Database '{db_name}' not found. Available databases: {list(databases.keys())}")
    
    return db_key, db_path

@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    """List available resources"""
    return [
        types.Resource(
            uri=f"schema://{db_key}",
            name=f"Database Schema: {db_key}",
            description=f"Schema for {db_info['description']}",
            mimeType="text/plain",
        )
        for db_key, db_info in databases.items()
    ]

@server.read_resource()
async def handle_read_resource(uri: types.AnyUrl) -> str:
    """Read a resource"""
    if str(uri).startswith("schema://"):
        db_key = str(uri).replace("schema://", "")
        if db_key not in databases:
            raise ValueError(f"Database '{db_key}' not found")
        
        db_path = databases[db_key]['path']
        
        # Create a connection string
        conn_str = (
            r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
            fr'DBQ={db_path};'
            + (f'PWD={db_passwords[i]};' if i < len(db_passwords) else '')
        )
        
        # Establish the connection
        conn = pyodbc.connect(conn_str)
        cursor = conn.cursor()
        
        # Get table schema information
        tables = [f"Database: {db_key} ({databases[db_key]['description']})"]
        for row in cursor.tables(tableType='TABLE'):
            if not row.table_name.startswith('MSys'):  # Skip system tables
                table_name = row.table_name
                tables.append(f"Table: {table_name}")
                # Get column information for each table
                columns = cursor.columns(table=table_name)
                for column in columns:
                    tables.append(f"  - Column: {column.column_name}, Type: {column.type_name}")
        
        conn.close()
        return "\n".join(tables)
    else:
        raise ValueError(f"Unknown resource: {uri}")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools"""
    return [
        types.Tool(
            name="list_databases",
            description="List all available databases. Use this when you need to know what databases exist or their descriptions. No parameters required.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="get_schema_tool",
            description="Get comprehensive database schema information for AI agents. Always specify the database name. Example: {'database': 'mrpplus_be', 'format': 'json', 'include_samples': true}",
            inputSchema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "Name of the database to query (required for multi-DB awareness)",
                    },
                    "format": {
                        "type": "string",
                        "description": "Output format - 'text' or 'json'",
                        "enum": ["text", "json"],
                        "default": "text",
                    },
                    "include_samples": {
                        "type": "boolean",
                        "description": "Whether to include sample data for each table",
                        "default": False,
                    },
                },
                "required": ["database"],
            },
        ),
        types.Tool(
            name="query_data",
            description="""Execute SQL queries across multiple Microsoft Access databases with automatic cross-database JOIN support.

⚠️ CRITICAL CONSTRAINTS (Access has strict limitations):

1. DATABASE PREFIX REQUIREMENT:
   - ALWAYS use [database_name].[table_name] syntax for ALL tables
   - Example: FROM [sales_db].[customers] not FROM customers
   - Missing prefixes will cause "table not found" errors

2. CROSS-DATABASE JOIN LIMITATIONS:
   - Direct JOINs between databases often fail with cryptic errors
   - Access IN clause limited to 255 elements maximum
   - Complex JOINs across databases may timeout or return incorrect results
   - WORKAROUND: Use WHERE clause instead of JOIN syntax

3. ACCESS SQL DIALECT CONSTRAINTS:
   - Square brackets: [TableName], [Column Name] - REQUIRED around ALL names
   - Date literals: #2023-01-01# - MUST use # delimiters, not quotes
   - String concatenation: & operator - NOT + (will cause type errors)
   - Conditionals: IIF(condition, true_val, false_val) - NOT CASE WHEN
   - Limit results: TOP N - NOT LIMIT (unsupported function)
   - Boolean values: 1/0 or -1/0 - NOT True/False (will cause parameter errors)
   - Type conversion: CInt(), CDbl(), CStr() - NOT CAST() (unsupported)

4. PERFORMANCE CONSTRAINTS:
   - File size limit: 2GB maximum per database
   - Record limit: ~1-2 million records per table (performance degrades)
   - Query timeout: Complex queries may timeout after 30 seconds
   - Memory limit: Large result sets can cause out-of-memory errors
   - Concurrent users: Maximum 5-10 simultaneous connections

5. DATA TYPE CONSTRAINTS:
   - Text fields: 255 character limit (use Memo for longer text)
   - Number precision: Currency vs Double precision differences
   - Date range: Years 100-9999 only
   - Boolean inconsistency: Some tables use 1/0, others use -1/0
   - Unicode issues: Older databases may have encoding problems

WORKING EXAMPLES:
✅ Simple cross-DB: SELECT TOP 10 [name] FROM [db1].[customers] WHERE [active] = 1
✅ Cross-DB WHERE: SELECT * FROM [db1].[orders] AS [o], [db2].[customers] AS [c] WHERE [o].[cust_id] = [c].[id]
✅ Cross-DB UNION: SELECT [name] FROM [db1].[customers] UNION ALL SELECT [name] FROM [db2].[vendors]

PROBLEMATIC PATTERNS:
❌ Direct JOIN: FROM [db1].[table1] JOIN [db2].[table2] ON [condition]
❌ Standard SQL: SELECT * FROM table1 LIMIT 10 WHERE date > '2024-01-01'
❌ Wrong types: WHERE active = True AND amount > CAST(field AS INT)

The system converts [database].[table] references to Access IN clause syntax automatically.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": """SQL query using [database].[table] prefixes for ALL tables with Access SQL syntax.

MANDATORY TEMPLATE: SELECT [field1], [field2] FROM [database_name].[table_name] WHERE [field3] > #2024-01-01#

CONSTRAINT EXAMPLES:
- Cross-DB WHERE: SELECT [c].[name], [o].[total] FROM [sales_db].[customers] AS [c], [orders_db].[orders] AS [o] WHERE [c].[id] = [o].[customer_id]
- Date filtering: WHERE [order_date] > #2024-01-01# AND [order_date] < #2024-12-31#
- Boolean check: WHERE [active] = 1 AND [deleted] = 0
- String concat: SELECT [first_name] & ' ' & [last_name] AS [full_name]
- Type conversion: WHERE CInt([text_field]) > 100
- Conditional: SELECT IIF([balance] > 1000, 'High', 'Low') AS [category]""",
                    },
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="test_cross_db_connectivity",
            description="Test connectivity to all databases and verify cross-database query functionality. No parameters required. This tool helps diagnose connection issues and tests the query rewriting system.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="query_builder_help", 
            description="Get specific help for building Access SQL queries with examples for your use case",
            inputSchema={
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["simple_select", "cross_database_join", "union", "aggregation", "date_filtering", "boolean_filtering"],
                        "description": "Type of query you want to build"
                    },
                    "databases": {
                        "type": "array", 
                        "items": {"type": "string"},
                        "description": "List of database names you want to query (optional)"
                    },
                    "description": {
                        "type": "string",
                        "description": "Describe what you want to accomplish (optional)"
                    }
                },
                "required": ["query_type"]
            }
        ),
        types.Tool(
            name="validate_query_syntax",
            description="Validate Access SQL syntax before execution to catch common errors",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string", 
                        "description": "SQL query to validate"
                    }
                },
                "required": ["sql"]
            }
        ),
        types.Tool(
            name="query_limitations",
            description="Get information about Access SQL limitations and workarounds for specific scenarios",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": ["joins", "performance", "data_types", "functions", "best_practices", "all"],
                        "description": "Specific limitation topic to learn about"
                    }
                },
                "required": ["topic"]
            }
        ),
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    """Handle tool calls"""
    if arguments is None:
        arguments = {}
    
    if name == "list_databases":
        if not databases:
            result = "No databases registered"
        else:
            db_list = []
            db_list.append(f"Available Databases ({len(databases)}):")
            db_list.append("-" * 40)
            
            for db_name, db_info in databases.items():
                is_default = " (default)" if db_name == default_db_key else ""
                db_list.append(f"• {db_name}{is_default}")
                db_list.append(f"  Path: {db_info['path']}")
                db_list.append(f"  Description: {db_info['description']}")
                
                # Try to get basic info about the database
                try:
                    conn_str = (
                        r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
                        fr'DBQ={db_info["path"]};'
                        + (f'PWD={db_passwords[i]};' if i < len(db_passwords) else '')
                    )
                    conn = pyodbc.connect(conn_str)
                    cursor = conn.cursor()
                    
                    # Count tables
                    table_count = len([row for row in cursor.tables(tableType='TABLE') if not row.table_name.startswith('MSys')])
                    db_list.append(f"  Tables: {table_count}")
                    conn.close()
                    
                except Exception as e:
                    db_list.append(f"  Status: Error - {str(e)}")
                
                db_list.append("")  # Empty line for spacing
            
            result = "\n".join(db_list)
    
    elif name == "get_schema_tool":
        database = arguments.get("database")
        format_type = arguments.get("format", "text")
        include_samples = arguments.get("include_samples", False)
        
        try:
            db_key, db_path = get_database_path(database)
        except ValueError as e:
            result = str(e)
        else:
            # Create a connection string
            conn_str = (
                r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
                fr'DBQ={db_path};'
                + (f'PWD={db_password};' if db_password else '')
            )
            
            # Establish the connection
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()
            
            # Get comprehensive schema information
            schema_info = {
                "database": db_key,
                "path": db_path,
                "description": databases[db_key]['description'],
                "tables": {},
                "relationships": [],
                "saved_queries": []
            }
            
            try:
                # Get all tables
                tables = []
                for row in cursor.tables(tableType='TABLE'):
                    if not row.table_name.startswith('MSys'):  # Skip system tables
                        tables.append(row.table_name)
                
                # Process each table
                for table_name in tables:
                    table_info = {
                        "columns": [],
                        "primary_keys": [],
                        "foreign_keys": [],
                        "indexes": [],
                        "sample_data": None,
                        "row_count": None
                    }
                    
                    # Get column information
                    columns = cursor.columns(table=table_name)
                    for column in columns:
                        col_info = {
                            "name": column.column_name,
                            "type": column.type_name,
                            "size": column.column_size,
                            "nullable": column.nullable,
                            "default": getattr(column, 'column_def', None),
                            "position": column.ordinal_position
                        }
                        table_info["columns"].append(col_info)
                    
                    # Get primary key information
                    try:
                        pk_columns = cursor.primaryKeys(table=table_name)
                        for pk in pk_columns:
                            table_info["primary_keys"].append({
                                "column": pk.column_name,
                                "key_seq": pk.key_seq
                            })
                    except:
                        pass
                    
                    # Get foreign key information
                    try:
                        fk_columns = cursor.foreignKeys(table=table_name)
                        for fk in fk_columns:
                            table_info["foreign_keys"].append({
                                "column": fk.fkcolumn_name,
                                "references_table": fk.pktable_name,
                                "references_column": fk.pkcolumn_name,
                                "constraint_name": getattr(fk, 'fk_name', 'Unknown')
                            })
                    except:
                        pass
                    
                    # Get table statistics
                    try:
                        count_result = cursor.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()
                        table_info["row_count"] = count_result[0] if count_result else 0
                    except:
                        table_info["row_count"] = "Unable to determine"
                    
                    # Get sample data if requested
                    if include_samples and table_info["row_count"] != 0:
                        try:
                            sample_result = cursor.execute(f"SELECT TOP 3 * FROM [{table_name}]").fetchall()
                            if sample_result:
                                # Convert to list of dictionaries for better readability
                                column_names = [col["name"] for col in table_info["columns"]]
                                samples = []
                                for row in sample_result:
                                    sample_row = {}
                                    for i, value in enumerate(row):
                                        if i < len(column_names):
                                            # Convert to string for JSON serialization
                                            sample_row[column_names[i]] = str(value) if value is not None else None
                                    samples.append(sample_row)
                                table_info["sample_data"] = samples
                        except:
                            table_info["sample_data"] = "Unable to retrieve sample data"
                    
                    schema_info["tables"][table_name] = table_info
                
                # Get saved queries/views (Access queries)
                try:
                    query_tables = cursor.tables(tableType='VIEW')
                    for query_row in query_tables:
                        if not query_row.table_name.startswith('MSys'):
                            query_info = {
                                "name": query_row.table_name,
                                "type": "VIEW/QUERY"
                            }
                            
                            # Try to get column information for the query
                            try:
                                query_columns = cursor.columns(table=query_row.table_name)
                                query_info["columns"] = []
                                for col in query_columns:
                                    query_info["columns"].append({
                                        "name": col.column_name,
                                        "type": col.type_name
                                    })
                            except:
                                query_info["columns"] = "Unable to retrieve query columns"
                            
                            schema_info["saved_queries"].append(query_info)
                except:
                    pass
                
                # Try to get relationship information from foreign keys
                relationships = {}
                for table_name, table_info in schema_info["tables"].items():
                    for fk in table_info["foreign_keys"]:
                        rel_key = f"{table_name}.{fk['column']} -> {fk['references_table']}.{fk['references_column']}"
                        relationships[rel_key] = {
                            "from_table": table_name,
                            "from_column": fk['column'],
                            "to_table": fk['references_table'],
                            "to_column": fk['references_column']
                        }
                schema_info["relationships"] = list(relationships.values())
                
            except Exception as e:
                schema_info["error"] = f"Error retrieving schema: {str(e)}"
            
            conn.close()
            
            if format_type.lower() == "json":
                result = json.dumps(schema_info, indent=2)
            else:
                # Return AI-friendly text format
                output = []
                output.append(f"DATABASE SCHEMA FOR AI AGENT")
                output.append(f"Database: {db_key} ({db_path})")
                output.append(f"Description: {databases[db_key]['description']}")
                output.append("=" * 80)
                
                # Tables section
                output.append(f"\nTABLES ({len(schema_info['tables'])} total):")
                output.append("-" * 40)
                
                for table_name, table_info in schema_info["tables"].items():
                    output.append(f"\n📋 TABLE: {table_name}")
                    output.append(f"   Rows: {table_info['row_count']}")
                    
                    # Primary keys
                    if table_info["primary_keys"]:
                        pk_cols = [pk["column"] for pk in table_info["primary_keys"]]
                        output.append(f"   🔑 Primary Key: {', '.join(pk_cols)}")
                    
                    # Columns
                    output.append("   📊 Columns:")
                    for col in table_info["columns"]:
                        nullable = "NULL" if col["nullable"] else "NOT NULL"
                        default_info = f", Default: {col['default']}" if col['default'] else ""
                        output.append(f"      • {col['name']}: {col['type']}({col['size']}) {nullable}{default_info}")
                    
                    # Foreign keys
                    if table_info["foreign_keys"]:
                        output.append("   🔗 Foreign Keys:")
                        for fk in table_info["foreign_keys"]:
                            output.append(f"      • {fk['column']} → {fk['references_table']}.{fk['references_column']}")
                    
                    # Sample data
                    if include_samples and table_info["sample_data"]:
                        output.append("   📋 Sample Data:")
                        if isinstance(table_info["sample_data"], list):
                            for i, sample in enumerate(table_info["sample_data"][:2]):  # Show max 2 samples
                                sample_str = ", ".join([f"{k}={v}" for k, v in sample.items() if v is not None])
                                output.append(f"      Row {i+1}: {sample_str}")
                        else:
                            output.append(f"      {table_info['sample_data']}")
                
                # Relationships section
                if schema_info["relationships"]:
                    output.append(f"\n🔗 RELATIONSHIPS ({len(schema_info['relationships'])} total):")
                    output.append("-" * 40)
                    for rel in schema_info["relationships"]:
                        output.append(f"   {rel['from_table']}.{rel['from_column']} → {rel['to_table']}.{rel['to_column']}")
                
                # Saved queries section
                if schema_info["saved_queries"]:
                    output.append(f"\n💾 SAVED QUERIES/VIEWS ({len(schema_info['saved_queries'])} total):")
                    output.append("-" * 40)
                    for query in schema_info["saved_queries"]:
                        output.append(f"   📝 {query['name']} ({query['type']})")
                        if isinstance(query['columns'], list):
                            col_names = [col['name'] for col in query['columns']]
                            output.append(f"      Columns: {', '.join(col_names)}")
                
                # AI Tips section
                output.append(f"\n🤖 AI QUERY TIPS:")
                output.append("-" * 40)
                output.append("• Use square brackets around table/column names: [TableName], [Column Name]")
                output.append("• Access uses different syntax: Use & for string concatenation, not +")
                output.append("• For dates, use # delimiters: WHERE [DateField] = #2023-01-01#")
                output.append("• Primary keys are ideal for WHERE clauses and JOINs")
                output.append("• Use foreign key relationships shown above for proper JOINs")
                if schema_info["saved_queries"]:
                    output.append("• Consider using saved queries/views as they may have complex logic")
                output.append("• For queries across multiple databases, use [db_name].[TableName] syntax")
                output.append("• Access SQL differences: Use TOP N (not LIMIT), # for dates, & for string concat, IIF() for conditionals")
                output.append("• Boolean fields typically use 1/0 instead of True/False")
                
                output.append("\n⚠️ CRITICAL CONSTRAINTS:")
                output.append("• Cross-database JOINs often fail - use WHERE clause instead")
                output.append("• Text fields limited to 255 chars (use Memo for longer)")
                output.append("• Date range: Years 100-9999 only")
                output.append("• No CAST(), CASE WHEN, LIMIT - use Access equivalents")
                output.append("• Database files limited to 2GB maximum")
                output.append("• Performance degrades with >100k records")
                output.append("• Boolean inconsistency: some tables use 1/0, others -1/0")
                
                output.append("\nRECOMMENDED WORKFLOW:")
                output.append("1. START: Use list_databases to see available databases")
                output.append("2. EXPLORE: Use get_schema_tool for each relevant database")
                output.append("3. BUILD: Use query_builder_help to see examples for your query type")
                output.append("4. VALIDATE: Use validate_query_syntax to check before executing")
                output.append("5. EXECUTE: Use query_data with proper [database].[table] syntax")
                output.append("6. TROUBLESHOOT: If errors occur, the tool provides specific guidance")
                output.append("7. CONSTRAINTS: Use query_limitations to understand Access limitations")
                
                result = "\n".join(output)
    
    elif name == "query_data":
        sql = arguments.get("sql")
        
        if not sql:
            result = "Error: SQL query is required"
        else:
            is_valid, validation_msg = validate_cross_db_syntax(sql, databases)
            if not is_valid:
                result = f"Error: {validation_msg}"
            else:
                db_key = default_db_key
                db_path = databases[db_key]['path']
                
                rewritten_sql = rewrite_cross_db_query(sql, databases, db_key)
                
                conn_str = (
                    r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
                    fr'DBQ={db_path};'
                    + (f'PWD={db_passwords[i]};' if i < len(db_passwords) else '')
                    + r'ExtendedAnsiSQL=1;'
                )
                
                try:
                    conn = pyodbc.connect(conn_str, timeout=30)
                    cursor = conn.cursor()
                    
                    print(f"DEBUG - Original SQL: {sql}")
                    print(f"DEBUG - Rewritten SQL: {rewritten_sql}")
                    print(f"DEBUG - Primary DB: {db_key}")
                    
                    query_result = cursor.execute(rewritten_sql).fetchall()
                    conn.close()
                    
                    output_lines = [f"Query executed on primary database: {db_key} with cross-DB support"]
                    output_lines.append("-" * 40)
                    output_lines.extend(str(row) for row in query_result)
                    
                    result = "\n".join(output_lines)
                    
                except pyodbc.Error as e:
                    error_msg = str(e)
                    result = f"Database Error ({e.args[0]}): {error_msg}"
                    result += f"\nPrimary Database: {db_key}"
                    result += f"\nOriginal SQL: {sql}"
                    result += f"\nRewritten SQL: {rewritten_sql}"
                    
                    # Use the enhanced error message helper
                    helpful_msg = get_helpful_error_message(error_msg, sql)
                    result += f"\n\n{helpful_msg}"
                    
                    # Add path verification for FROM clause errors
                    if "Syntax error in FROM clause" in error_msg:
                        result += "\n\nVerify database paths exist:"
                        for db_name, db_info in databases.items():
                            exists = "✅" if os.path.exists(db_info['path']) else "❌"
                            result += f"\n  {exists} {db_name}: {db_info['path']}"
                
                except Exception as e:
                    result = f"General Error: {str(e)}\nOriginal SQL: {sql}\nRewritten SQL: {rewritten_sql}"
    
    elif name == "test_cross_db_connectivity":
        test_results = []
        test_results.append("Cross-Database Connectivity Test")
        test_results.append("=" * 50)
        
        working_dbs = []
        for db_key, db_info in databases.items():
            try:
                if not os.path.exists(db_info['path']):
                    test_results.append(f"❌ {db_key}: File not found at {db_info['path']}")
                    continue
                    
                conn_str = (
                    r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
                    fr'DBQ={db_info["path"]};'
                    + (f'PWD={db_password};' if db_password else '')
                )
                conn = pyodbc.connect(conn_str, timeout=10)
                
                cursor = conn.cursor()
                tables = [row.table_name for row in cursor.tables(tableType='TABLE') 
                         if not row.table_name.startswith('MSys')]
                
                test_results.append(f"✅ {db_key}: Connected successfully ({len(tables)} tables)")
                if tables:
                    test_results.append(f"   Sample tables: {', '.join(tables[:3])}{'...' if len(tables) > 3 else ''}")
                
                working_dbs.append((db_key, tables[0] if tables else None))
                conn.close()
                
            except Exception as e:
                test_results.append(f"❌ {db_key}: Connection failed - {str(e)}")
        
        if len(working_dbs) >= 2:
            test_results.append(f"\nTesting Cross-Database Query Rewriting:")
            test_results.append("-" * 30)
            
            db1_key, db1_table = working_dbs[0]
            db2_key, db2_table = working_dbs[1]
            
            if db1_table and db2_table:
                test_sql = f"SELECT COUNT(*) FROM [{db1_key}].[{db1_table}] UNION ALL SELECT COUNT(*) FROM [{db2_key}].[{db2_table}]"
                test_results.append(f"Original SQL: {test_sql}")
                
                rewritten = rewrite_cross_db_query(test_sql, databases, default_db_key)
                test_results.append(f"Rewritten SQL: {rewritten}")
                
                test_results.append("\nTesting rewritten query execution...")
                try:
                    conn_str = (
                        r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
                        fr'DBQ={databases[default_db_key]["path"]};'
                        + (f'PWD={db_passwords[i]};' if i < len(db_passwords) else '')
                    )
                    conn = pyodbc.connect(conn_str, timeout=10)
                    cursor = conn.cursor()
                    result = cursor.execute(rewritten).fetchall()
                    test_results.append(f"✅ Cross-database query executed successfully!")
                    test_results.append(f"   Results: {len(result)} rows returned")
                    conn.close()
                except Exception as e:
                    test_results.append(f"❌ Cross-database query failed: {str(e)}")
        else:
            test_results.append(f"\nNeed at least 2 working databases to test cross-DB queries")
            test_results.append(f"Currently have {len(working_dbs)} working database(s)")
        
        result = "\n".join(test_results)
    
    elif name == "query_builder_help":
        query_type = arguments.get("query_type")
        databases_list = arguments.get("databases", [])
        description = arguments.get("description", "")
        
        examples = {
            "simple_select": """
SIMPLE SELECT QUERY EXAMPLES:

Basic syntax: SELECT [columns] FROM [database].[table] WHERE [conditions]

Examples:
1. All columns: SELECT * FROM [mrpplus_be].[Customers]
2. Specific columns: SELECT [CustomerName], [Balance] FROM [mrpplus_be].[Customers]
3. With filter: SELECT [Name] FROM [mrpplus_be].[Customers] WHERE [Balance] > 1000
4. With sorting: SELECT TOP 10 [Name] FROM [mrpplus_be].[Customers] ORDER BY [Balance] DESC
5. With date filter: SELECT * FROM [mrpplus_be].[Orders] WHERE [OrderDate] > #2024-01-01#

Remember: Always use [database].[table] format!""",
            
            "cross_database_join": """
CROSS-DATABASE JOIN EXAMPLES:

Note: Access has limitations with cross-database JOINs. Consider using WHERE clauses or UNION instead.

Examples:
1. Simple cross-DB (using WHERE):
   SELECT [c].[Name], [o].[Amount]
   FROM [customers_db].[Customers] AS [c], [orders_db].[Orders] AS [o]
   WHERE [c].[CustomerID] = [o].[CustomerID]

2. With filtering:
   SELECT [c].[Name], [o].[OrderDate], [o].[Amount]
   FROM [sales_db].[Customers] AS [c], [orders_db].[Orders] AS [o]
   WHERE [c].[ID] = [o].[CustomerID] 
   AND [o].[OrderDate] > #2024-01-01#

3. Alternative using subquery:
   SELECT [Name] FROM [db1].[Customers] 
   WHERE [ID] IN (SELECT [CustomerID] FROM [db2].[Orders] WHERE [Amount] > 1000)""",
            
            "union": """
UNION QUERY EXAMPLES:

Combine results from multiple tables/databases:

1. Simple UNION:
   SELECT [Name], 'Customer' AS [Type] FROM [db1].[Customers]
   UNION ALL
   SELECT [Name], 'Vendor' AS [Type] FROM [db2].[Vendors]

2. With filtering:
   SELECT [Name] FROM [db1].[ActiveCustomers] WHERE [Status] = 1
   UNION ALL
   SELECT [Name] FROM [db2].[ActiveVendors] WHERE [Active] = 1

3. Cross-database inventory:
   SELECT [PartNumber], [Quantity], 'Warehouse1' AS [Location] FROM [db1].[Inventory]
   UNION ALL
   SELECT [PartNumber], [Quantity], 'Warehouse2' AS [Location] FROM [db2].[Inventory]

Note: Use UNION ALL to keep duplicates, UNION to remove them.""",
            
            "aggregation": """
AGGREGATION QUERY EXAMPLES:

Common aggregate functions: COUNT(), SUM(), AVG(), MIN(), MAX()

1. Count records:
   SELECT COUNT(*) AS [TotalCustomers] FROM [db1].[Customers]

2. Sum with grouping:
   SELECT [State], COUNT(*) AS [CustomerCount], SUM([Balance]) AS [TotalBalance]
   FROM [db1].[Customers]
   GROUP BY [State]
   ORDER BY COUNT(*) DESC

3. Average by category:
   SELECT [Category], AVG([Price]) AS [AvgPrice], COUNT(*) AS [ItemCount]
   FROM [inventory_db].[Products]
   GROUP BY [Category]
   HAVING AVG([Price]) > 100

4. Cross-database totals:
   SELECT 'DB1' AS [Source], COUNT(*) AS [Count] FROM [db1].[Orders]
   UNION ALL
   SELECT 'DB2' AS [Source], COUNT(*) AS [Count] FROM [db2].[Orders]""",
            
            "date_filtering": """
DATE FILTERING EXAMPLES:

Access uses # delimiters for dates: #YYYY-MM-DD#

1. After specific date:
   SELECT * FROM [db1].[Orders] WHERE [OrderDate] > #2024-01-01#

2. Date range:
   SELECT * FROM [db1].[Orders] 
   WHERE [OrderDate] >= #2024-01-01# AND [OrderDate] <= #2024-12-31#

3. Current year (using Year function):
   SELECT * FROM [db1].[Orders] WHERE Year([OrderDate]) = Year(Date())

4. Last 30 days:
   SELECT * FROM [db1].[Orders] WHERE [OrderDate] >= DateAdd('d', -30, Date())

5. Month comparison:
   SELECT * FROM [db1].[Orders] WHERE Month([OrderDate]) = 12 AND Year([OrderDate]) = 2024

Common date functions: Date(), DateAdd(), DateDiff(), Year(), Month(), Day()""",
            
            "boolean_filtering": """
BOOLEAN FILTERING EXAMPLES:

Access uses 1/0 for True/False in most cases:

1. Simple boolean:
   SELECT * FROM [db1].[Customers] WHERE [Active] = 1

2. Negation:
   SELECT * FROM [db1].[Customers] WHERE [Active] = 0

3. Multiple conditions:
   SELECT * FROM [db1].[Orders] 
   WHERE [Shipped] = 1 AND [Paid] = 1 AND [Cancelled] = 0

4. With IIF conditional:
   SELECT [Name], IIF([Active] = 1, 'Active', 'Inactive') AS [Status]
   FROM [db1].[Customers]

5. Complex boolean logic:
   SELECT * FROM [db1].[Products]
   WHERE ([InStock] = 1 AND [Quantity] > 0) OR [OnOrder] = 1

Note: Some Access tables may use -1 for True, check your data!"""
        }
        
        result = examples.get(query_type, "Unknown query type")
        
        if databases_list:
            result += f"\n\nDatabases specified: {', '.join(databases_list)}"
        if description:
            result += f"\n\nYour requirement: {description}"
        
        result += "\n\nTIP: Use validate_query_syntax tool to check your query before running!"
    
    elif name == "validate_query_syntax":
        sql = arguments.get("sql", "")
        
        validation_errors = []
        warnings = []
        
        # Check for common SQL mistakes
        if "LIMIT " in sql.upper():
            validation_errors.append("❌ Use TOP N instead of LIMIT N")
        
        if "CAST(" in sql.upper():
            validation_errors.append("❌ Use CInt(), CDbl(), or CStr() instead of CAST()")
        
        if re.search(r"'\d{4}-\d{2}-\d{2}'", sql):
            validation_errors.append("❌ Use #YYYY-MM-DD# for dates, not 'YYYY-MM-DD'")
        
        if " + " in sql and ("'" in sql or '"' in sql):
            warnings.append("⚠️ Use & for string concatenation instead of +")
        
        if "True" in sql or "False" in sql:
            validation_errors.append("❌ Use 1/0 instead of True/False for boolean values")
        
        # Check for database prefixes
        is_valid, validation_msg = validate_cross_db_syntax(sql, databases)
        if not is_valid:
            validation_errors.append(f"❌ {validation_msg}")
        
        # Check for CASE statements
        if "CASE " in sql.upper():
            validation_errors.append("❌ Use IIF() instead of CASE statements")
        
        # Build result
        if validation_errors:
            result = "VALIDATION FAILED:\n\n" + "\n".join(validation_errors)
        else:
            result = "✅ Query syntax appears valid for Access!"
        
        if warnings:
            result += "\n\nWARNINGS:\n" + "\n".join(warnings)
        
        # Add helpful suggestions
        result += "\n\nQUICK REFERENCE:"
        result += "\n• Dates: #2024-01-01#"
        result += "\n• Booleans: WHERE [Active] = 1"
        result += "\n• String concat: [FirstName] & ' ' & [LastName]"
        result += "\n• Conditionals: IIF([Price] > 100, 'Expensive', 'Affordable')"
        result += "\n• Limit: SELECT TOP 10 ..."
    
    elif name == "query_limitations":
        topic = arguments.get("topic")
        
        limitations = {
            "joins": """
🔗 CROSS-DATABASE JOIN CONSTRAINTS:

❌ HARD LIMITATIONS:
• Direct JOINs between databases fail with cryptic errors like "'c'" or "parameter missing"
• Access IN clause limited to 255 elements maximum
• LEFT/RIGHT JOINs across databases not supported
• Complex multi-table JOINs across databases cause timeouts
• Subqueries with JOINs across databases often fail
• No support for FULL OUTER JOINs

🔍 WHY JOINS FAIL:
• Access query engine can't resolve table aliases across different .mdb/.accdb files
• Cross-database table references use IN clause internally, which has element limits
• Access assumes all tables in a JOIN are in the same database file
• Query optimizer doesn't handle cross-database execution plans well

✅ PROVEN WORKAROUNDS:
1. WHERE clause (most reliable):
   SELECT * FROM [db1].[customers] AS [c], [db2].[orders] AS [o] 
   WHERE [c].[id] = [o].[customer_id]

2. Separate queries + application logic:
   Query 1: SELECT * FROM [db1].[customers] WHERE [active] = 1
   Query 2: SELECT * FROM [db2].[orders] WHERE [customer_id] IN (results from Query 1)

3. UNION for combining data:
   SELECT [name], 'Customer' AS [type] FROM [db1].[customers]
   UNION ALL
   SELECT [name], 'Vendor' AS [type] FROM [db2].[vendors]

4. Subquery approach (limited):
   SELECT * FROM [db1].[customers] 
   WHERE [id] IN (SELECT [customer_id] FROM [db2].[orders])

⚠️ CONSTRAINT SPECIFICS:
• Maximum 255 elements in IN clause
• No correlated subqueries across databases
• Aliases must be unique across all referenced databases
• Performance degrades exponentially with multiple cross-DB references""",

            "performance": """
⚡ PERFORMANCE CONSTRAINTS:

📊 HARD LIMITS:
• Database file size: 2GB maximum (.mdb and .accdb)
• Table record limit: ~2 million records (theoretical), ~500k practical
• Query timeout: 30 seconds default, 60 seconds maximum
• Memory usage: ~2GB per connection (32-bit Access driver)
• Concurrent connections: 5-10 maximum before lock contention
• Field size: 255 chars (Text), 65,535 chars (Memo)

🐌 PERFORMANCE DEGRADATION POINTS:
• Tables >100k records: Noticeable slowdown
• Cross-DB queries >50k records: Significant slowdown (5-30 seconds)
• Complex WHERE clauses >1M records: Often timeout
• UNION operations >100k total records: Memory issues
• Text searches without indexes: Extremely slow

📈 QUERY PERFORMANCE EXPECTATIONS:
┌─────────────────────┬──────────────┬─────────────────┐
│ Operation Type      │ Record Count │ Expected Time   │
├─────────────────────┼──────────────┼─────────────────┤
│ Simple SELECT       │ <10k         │ <1 second       │
│ Filtered SELECT     │ <100k        │ 1-5 seconds     │
│ Cross-DB WHERE      │ <50k         │ 5-15 seconds    │
│ Cross-DB UNION      │ <100k        │ 10-30 seconds   │
│ Complex aggregation │ >100k        │ 30+ seconds     │
│ Text search (no idx)│ >10k         │ 30+ seconds     │
└─────────────────────┴──────────────┴─────────────────┘

⚠️ MEMORY CONSTRAINTS:
• Large result sets (>10k rows) can cause out-of-memory errors
• Cross-database queries load entire result sets into memory
• No streaming support for large datasets
• 32-bit ODBC driver has 2GB memory limit per connection

🚀 OPTIMIZATION STRATEGIES:
1. Always use TOP N to limit results
2. Add WHERE clauses to filter data early
3. Create indexes on frequently queried columns
4. Avoid SELECT * - specify only needed columns
5. Break large queries into smaller chunks
6. Use UNION ALL instead of UNION (faster)
7. Avoid complex subqueries across databases
8. Consider data archiving for tables >500k records

🔧 INDEX CONSTRAINTS:
• Maximum 32 indexes per table
• Composite indexes limited to 10 fields
• Index key size maximum 255 characters
• No partial indexes or filtered indexes
• No function-based indexes""",

            "data_types": """
📊 DATA TYPE CONSTRAINTS:

🔢 NUMERIC CONSTRAINTS:
• Byte: 0 to 255 only
• Integer: -32,768 to 32,767 (16-bit)
• Long Integer: -2,147,483,648 to 2,147,483,647 (32-bit)
• Single: 7 digits precision, scientific notation issues
• Double: 15 digits precision, rounding errors common
• Currency: 4 decimal places maximum, 15 digits before decimal
• Decimal: 28 digits precision, but not supported in all Access versions

📅 DATE/TIME CONSTRAINTS:
• Date range: January 1, 100 AD to December 31, 9999 AD
• Time precision: Seconds only (no milliseconds)
• Date format: Must use #YYYY-MM-DD# in queries
• No timezone support
• Year 2000 problem: 2-digit years interpreted as 1930-2029

📝 TEXT CONSTRAINTS:
• Text field: 255 characters maximum
• Memo field: 65,535 characters maximum (1GB theoretical)
• No Unicode support in older .mdb files
• Character encoding issues with special characters
• No full-text search capabilities
• Case-sensitive comparisons depend on database collation

✅/❌ BOOLEAN CONSTRAINTS:
• No native Boolean type
• Yes/No field uses -1 for True, 0 for False
• Some tables use 1 for True, 0 for False
• Cannot use True/False literals in queries
• Must use 1/0 or -1/0 in WHERE clauses

🔄 TYPE CONVERSION CONSTRAINTS:
• No CAST() function - use CInt(), CDbl(), CStr(), CDate()
• Implicit conversions often fail
• String to number conversion strict (no automatic trimming)
• Date conversions depend on regional settings
• No TRY_CAST equivalent - conversions fail with errors

🌐 ENCODING ISSUES:
• .mdb files: ANSI encoding (regional dependent)
• .accdb files: UTF-16 encoding
• Mixed encoding across databases causes corruption
• Special characters (accents, symbols) may not display correctly
• No support for multi-byte character sets in .mdb

⚠️ COMMON TYPE ERRORS:
• "Data type mismatch" - wrong boolean values (True/False vs 1/0)
• "Invalid use of Null" - null values in calculations
• "Overflow" - number too large for field type
• "Type mismatch" - string/number conversion failures
• "Parameter missing" - field name typos or wrong types in comparisons

🔧 WORKAROUNDS:
1. Standardize boolean representation across databases
2. Use conversion functions explicitly: CInt([text_field])
3. Handle nulls: IIF(IsNull([field]), 0, [field])
4. Test data types: SELECT TypeName([field]) FROM [table]
5. Use Memo fields for long text
6. Validate data before cross-database operations""",

            "functions": """
🔧 SQL FUNCTION CONSTRAINTS:

❌ UNSUPPORTED STANDARD SQL:
• CAST() - Use CInt(), CDbl(), CStr(), CDate() instead
• CASE WHEN - Use IIF() for conditionals
• LIMIT - Use TOP N in SELECT clause
• OFFSET - Not supported at all
• COALESCE() - Use IIF(IsNull(), alternative, value)
• NULLIF() - Use IIF(field1 = field2, Null, field1)
• ROW_NUMBER() - No window functions supported
• PARTITION BY - No analytical functions
• OVER() clause - Not supported
• Common Table Expressions (CTEs) - Not supported
• MERGE statements - Not supported
• UPSERT operations - Not supported

✅ ACCESS-SPECIFIC FUNCTIONS:
• String functions: Left(), Right(), Mid(), InStr(), Len(), Trim()
• Date functions: DateAdd(), DateDiff(), DatePart(), Year(), Month(), Day()
• Conversion: CInt(), CDbl(), CStr(), CDate(), Val()
• Conditional: IIF(condition, true_value, false_value)
• Null handling: IsNull(), Nz() (null to zero)
• Math: Abs(), Int(), Rnd(), Sgn(), Sqr()
• Aggregate: Count(), Sum(), Avg(), Min(), Max(), First(), Last()

🔍 FUNCTION LIMITATIONS:
• No user-defined functions in queries
• No recursive functions
• Limited string manipulation (no regex)
• No JSON functions
• No XML functions
• No encryption/hashing functions
• No array functions
• No pivot/unpivot operations

📊 AGGREGATE CONSTRAINTS:
• No MEDIAN() function
• No PERCENTILE functions
• No statistical functions (STDDEV, VARIANCE)
• GROUP BY limitations with Memo fields
• HAVING clause limited functionality
• No multiple grouping sets
• No ROLLUP or CUBE operations

🔤 STRING FUNCTION CONSTRAINTS:
• No REGEXP or pattern matching
• LIKE operator limited: * (any chars), ? (single char), # (single digit)
• No SOUNDEX or phonetic matching
• No string splitting functions
• Concatenation only with & operator
• No LTRIM/RTRIM - use Trim() only
• No REPLACE() function in older versions

📅 DATE FUNCTION CONSTRAINTS:
• No DATEADD with custom intervals
• DateDiff() limited to standard intervals (years, months, days, etc.)
• No timezone conversion functions
• No date formatting beyond basic Format() function
• No ISO date functions
• No business day calculations

🔢 MATH FUNCTION CONSTRAINTS:
• No advanced math functions (SIN, COS, TAN, LOG)
• No POWER() function - use ^ operator
• No ROUND() with precision control
• No FLOOR()/CEILING() - use Int() and custom logic
• No MOD() function - use Mod operator
• No bitwise operations

⚠️ FUNCTION ERROR PATTERNS:
• "Function not recognized" - using standard SQL functions
• "Wrong number of arguments" - Access function syntax differs
• "Type mismatch" - function expects different data type
• "Invalid use of Null" - function can't handle null values

🔧 COMMON CONVERSIONS:
┌─────────────────────┬─────────────────────────────────────┐
│ Standard SQL        │ Access Equivalent                   │
├─────────────────────┼─────────────────────────────────────┤
│ CAST(x AS INT)      │ CInt(x)                            │
│ CASE WHEN x THEN y  │ IIF(x, y, z)                       │
│ LIMIT 10            │ TOP 10                             │
│ COALESCE(x, y)      │ IIF(IsNull(x), y, x)               │
│ SUBSTRING(x, 1, 5)  │ Left(x, 5) or Mid(x, 1, 5)        │
│ LENGTH(x)           │ Len(x)                             │
│ UPPER(x)            │ UCase(x)                           │
│ LOWER(x)            │ LCase(x)                           │
│ CONCAT(x, y)        │ x & y                              │
│ NOW()               │ Now() or Date()                    │
└─────────────────────┴─────────────────────────────────────┘""",

            "best_practices": """
✨ ACCESS QUERY BEST PRACTICES:

🎯 WHEN TO USE ACCESS:
✅ Small to medium datasets (<500k records)
✅ Departmental reporting and analysis
✅ Data migration and transformation
✅ Rapid prototyping and testing
✅ Legacy system integration
✅ Single-user or small team environments
✅ File-based data storage requirements

🚫 WHEN NOT TO USE ACCESS:
❌ High-volume transactional systems
❌ Real-time applications requiring <1 second response
❌ Multi-user systems (>10 concurrent users)
❌ Mission-critical business systems
❌ Complex analytical workloads
❌ Systems requiring high availability
❌ Applications needing advanced security
❌ Big data or data warehouse scenarios

📋 QUERY DESIGN PRINCIPLES:
1. Always use [database].[table] syntax for clarity
2. Start with simple queries, add complexity gradually
3. Use TOP N to limit results during development
4. Add WHERE clauses to filter data early
5. Test with small datasets before scaling up
6. Validate data types and null handling
7. Use meaningful table aliases
8. Comment complex queries for maintenance

🔍 DEVELOPMENT WORKFLOW:
1. **Discovery**: Use list_databases to see available data
2. **Schema**: Use get_schema_tool to understand table structures
3. **Planning**: Use query_builder_help for syntax examples
4. **Validation**: Use validate_query_syntax before execution
5. **Testing**: Start with TOP 10 and simple WHERE clauses
6. **Optimization**: Add indexes and refine WHERE conditions
7. **Documentation**: Document complex queries and their purpose

⚠️ COMMON PITFALLS TO AVOID:
• Forgetting [database].[table] prefixes
• Using standard SQL syntax instead of Access dialect
• Attempting complex JOINs across databases
• Not handling null values properly
• Ignoring data type constraints
• Creating queries without WHERE clauses
• Using SELECT * in production queries
• Not testing with realistic data volumes

🚀 PERFORMANCE OPTIMIZATION:
1. **Indexing Strategy**:
   - Create indexes on frequently queried columns
   - Use composite indexes for multi-column WHERE clauses
   - Avoid over-indexing (max 32 per table)

2. **Query Optimization**:
   - Use EXISTS instead of IN for subqueries
   - Avoid functions in WHERE clauses
   - Use UNION ALL instead of UNION when duplicates acceptable
   - Break complex queries into simpler parts

3. **Data Management**:
   - Archive old data regularly
   - Compact and repair databases monthly
   - Monitor database file sizes
   - Use appropriate data types

🔧 ERROR HANDLING STRATEGY:
1. **Proactive Validation**:
   - Use validate_query_syntax before execution
   - Check data types with get_schema_tool
   - Test with small datasets first

2. **Error Interpretation**:
   - "Too few parameters" = field name typo or wrong data type
   - "Syntax error in FROM clause" = cross-database JOIN issue
   - "Reserved error" = unsupported function or complex query
   - "Data type mismatch" = boolean or date format issue

3. **Recovery Strategies**:
   - Use query_limitations to understand constraints
   - Simplify queries when errors occur
   - Use alternative approaches (WHERE vs JOIN)
   - Test individual components separately

📊 MONITORING AND MAINTENANCE:
• Monitor query execution times
• Track database file sizes
• Regular compact and repair operations
• Backup databases before major changes
• Document query patterns and performance
• Plan for data growth and archiving

🎓 LEARNING PROGRESSION:
1. **Beginner**: Master basic SELECT with WHERE clauses
2. **Intermediate**: Learn cross-database querying with constraints
3. **Advanced**: Optimize complex queries and handle edge cases
4. **Expert**: Design efficient data access patterns and troubleshoot issues

💡 PRO TIPS:
• Keep a reference of Access-specific functions handy
• Use query_builder_help for syntax examples
• Test all queries with realistic data volumes
• Document workarounds for future reference
• Plan database structure to minimize cross-database queries
• Consider migrating to SQL Server for complex requirements""",

            "all": """
📚 COMPLETE ACCESS CONSTRAINTS REFERENCE:

This tool has fundamental limitations due to Microsoft Access architecture:

🏗️ ARCHITECTURAL CONSTRAINTS:
1️⃣ FILE-BASED SYSTEM: 2GB limit per database file
2️⃣ SINGLE-THREADED: Limited concurrent access (5-10 users)
3️⃣ 32-BIT ODBC: Memory limitations and compatibility issues
4️⃣ WINDOWS-ONLY: Requires Microsoft Access ODBC driver
5️⃣ NO TRANSACTIONS: Limited ACID compliance across databases
6️⃣ LOCK GRANULARITY: Page-level locking causes contention
7️⃣ NO CLUSTERING: Cannot distribute across multiple servers
8️⃣ LIMITED BACKUP: No point-in-time recovery or hot backups

🔗 CROSS-DATABASE CONSTRAINTS:
• Direct JOINs fail with cryptic errors
• IN clause limited to 255 elements
• No correlated subqueries across databases
• Performance degrades exponentially with multiple DB references
• Query optimizer not designed for cross-database operations

⚡ PERFORMANCE CONSTRAINTS:
• 2GB file size limit per database
• ~500k practical record limit per table
• 30-60 second query timeout limits
• Memory-bound result sets (no streaming)
• Exponential performance degradation with data growth

📊 DATA TYPE CONSTRAINTS:
• Text fields: 255 char limit (Memo: 65k chars)
• No native Boolean type (uses -1/0 or 1/0)
• Date range: Years 100-9999 only
• No Unicode in .mdb files
• Limited numeric precision and range

🔧 SQL FUNCTION CONSTRAINTS:
• No CAST(), CASE WHEN, LIMIT, window functions
• No CTEs, MERGE, UPSERT operations
• Limited string manipulation (no regex)
• No advanced math or statistical functions
• Access-specific dialect required

🛡️ SECURITY CONSTRAINTS:
• File-based permissions only
• No row-level security
• Weak database password protection
• No encryption at rest
• Limited audit capabilities

🔄 CONCURRENCY CONSTRAINTS:
• Optimistic locking only
• No transaction isolation levels
• Page-level locking causes contention
• No deadlock detection
• Limited concurrent write operations

📈 SCALABILITY CONSTRAINTS:
• Cannot scale horizontally
• No load balancing capabilities
• No partitioning support
• No replication features
• Single point of failure

🎯 RECOMMENDED USAGE PATTERNS:
✅ Small departmental databases (<100k records)
✅ Read-heavy reporting workloads
✅ Data migration and ETL processes
✅ Prototyping and development
✅ Single-user analytical work

❌ AVOID FOR:
❌ Production web applications
❌ High-transaction systems
❌ Multi-user concurrent access
❌ Mission-critical systems
❌ Large-scale data processing

🔧 MITIGATION STRATEGIES:
1. Design within constraints from the start
2. Use appropriate data types and sizes
3. Implement proper indexing strategies
4. Plan for data archiving and cleanup
5. Monitor performance and file sizes
6. Have migration path to SQL Server ready

For specific constraint details, use query_limitations with:
• topic='joins' - Cross-database JOIN constraints
• topic='performance' - Speed and size limitations
• topic='data_types' - Type system constraints
• topic='functions' - SQL function limitations
• topic='best_practices' - How to work within constraints

💡 REMEMBER: These are not implementation bugs but fundamental Access limitations. Plan accordingly!"""
        }
        
        result = limitations.get(topic, "Unknown topic. Choose from: joins, performance, data_types, functions, best_practices, all")
    
    else:
        result = f"Unknown tool: {name}"
    
    return [types.TextContent(type="text", text=result)]

def get_helpful_error_message(error_msg: str, sql: str) -> str:
    """Convert cryptic Access errors into actionable guidance"""
    
    guidance = []
    
    if "Syntax error in FROM clause" in error_msg:
        guidance.extend([
            "❌ FROM clause syntax error - Common causes:",
            "• Missing [database].[table] prefix: Use [db_name].[table_name]",
            "• Incorrect path escaping in cross-database query",
            "• Table name misspelled or doesn't exist",
            "• Check if all databases in your query are accessible"
        ])
    
    elif "Too few parameters" in error_msg:
        guidance.extend([
            "❌ Field name error - Common causes:",
            "• Misspelled column name - check exact spelling and case",
            "• Boolean comparison: Use [field] = 1 not [field] = True", 
            "• Date format: Use #2024-01-01# not '2024-01-01'",
            "• Missing square brackets around field names with spaces"
        ])
    
    elif "no read permission" in error_msg:
        guidance.extend([
            "❌ Permission error - Table access denied:",
            "• Table may be locked by another process",
            "• Database file permissions may be restricted", 
            "• Table may not exist in the specified database",
            "• Try a different table or check database connectivity"
        ])
    
    elif "Reserved error" in error_msg:
        guidance.extend([
            "❌ Access internal error - Common causes:",
            "• Unsupported SQL function (try Access-specific functions)",
            "• Data type incompatibility in JOIN or UNION",
            "• Complex query too large for Access to process",
            "• Try simplifying the query or breaking it into parts"
        ])
    
    # Add query-specific suggestions
    if "CAST(" in sql.upper():
        guidance.append("💡 Try CInt(), CDbl(), or CStr() instead of CAST()")
    if "LIMIT " in sql.upper():
        guidance.append("💡 Use TOP N instead of LIMIT N")
    if " + " in sql and "'" in sql:
        guidance.append("💡 Use & for string concatenation instead of +")
    if "True" in sql or "False" in sql:
        guidance.append("💡 Use 1/0 instead of True/False for boolean values")
    
    return "\n".join(guidance)

def validate_cross_db_syntax(sql: str, databases: dict) -> tuple[bool, str]:
    has_db_prefix = False
    for db_key in databases.keys():
        if f'[{db_key}].' in sql:
            has_db_prefix = True
            break
    
    if not has_db_prefix:
        return False, "SQL must use [database_name].[table_name] prefixes for all tables to enable multi-DB support."
    
    return True, "OK"

def rewrite_cross_db_query(sql: str, databases: dict, primary_db: str) -> str:
    def replace_table_ref(match):
        db_key = match.group(1)
        table_name = match.group(2)
        
        if db_key == primary_db:
            return '[' + table_name + ']'
        else:
            # Get the path and escape backslashes for Access
            db_path = databases[db_key]['path']
            # Use string concatenation to avoid f-string Unicode escape issues
            return '[' + table_name + '] IN \'' + db_path + '\''
    
    # Pattern to match [db_name].[table_name]
    pattern = r'\[([^\]]+)\]\.\[([^\]]+)\]'
    return re.sub(pattern, replace_table_ref, sql)

def create_sse_server():
    """Create a Starlette app that handles SSE connections"""
    transport = SseServerTransport("/messages")
    
    async def handle_sse(request):
        async with transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], InitializationOptions(
                    server_name="MS Access Explorer",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                )
            )
    
    routes = [
        Route("/sse", endpoint=handle_sse),
        Mount("/messages", app=transport.handle_post_message),
    ]
    
    app = Starlette(routes=routes)
    
    # Add Authorization middleware if auth header is required
    if args.auth_header:
        app.add_middleware(AuthorizationMiddleware, required_auth_header=args.auth_header)
    
    # Add CORS middleware to allow cross-origin requests
    cors_origins = [args.cors_origin] if args.cors_origin != "*" else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],  # Allow all methods
        allow_headers=["*", "Authorization"],  # Allow all headers including Authorization
    )
    
    return app

async def run_stdio():
    """Run the server with stdio transport"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="MS Access Explorer",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

def run():
    """Run the MCP server with configured transport"""
    if args.transport == 'stdio':
        print("Starting MS Access MCP server with stdio transport...")
        asyncio.run(run_stdio())
    elif args.transport == 'sse':
        print(f"Starting MS Access MCP server with SSE transport on {args.host}:{args.port}{args.path}")
        print(f"CORS origins: {args.cors_origin}")
        if args.auth_header:
            print(f"Authorization required: {args.auth_header}")
        else:
            print("No authorization required")
        app = create_sse_server()
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        print(f"Unknown transport: {args.transport}")
        sys.exit(1)

if __name__ == "__main__":
    run() 