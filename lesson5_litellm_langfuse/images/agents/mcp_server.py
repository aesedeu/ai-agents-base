"""Bank MCP server — the same three tools as in lesson 3, now as a Kubernetes service.

    list_tables()          tables of the bank database
    describe_table(table)  columns of a table and what they mean
    run_sql(sql)           a read-only SQL query

In the cluster: Deployment `bank-mcp` (2 replicas) behind Service `bank-mcp:8101`.
The database address comes from the environment (BANK_DSN, set in the manifest).
"""

import os

import psycopg
from mcp.server.fastmcp import FastMCP

from common import make_logger

DSN = os.getenv("BANK_DSN", "postgresql://mcp_reader:mcp_reader@bank-db:5432/bank")   # read-only role
PORT = 8101

log = make_logger("bank-mcp")
# host="0.0.0.0": listen on the pod's network, not only inside the container.
#   (On 0.0.0.0 FastMCP does not restrict the Host header, so "bank-mcp:8101" from other pods is accepted.)
# stateless_http=True: no MCP session kept in memory. With several replicas behind one Service, a session
#   opened on replica A would be unknown to replica B → "Session terminated". Stateless — any replica answers.
mcp = FastMCP("bank", host="0.0.0.0", port=PORT, stateless_http=True, log_level="WARNING")


def query(sql: str, params=None) -> str:
    """Run SQL and return the result as text: a header line, then one line per row (max 50).

    A database error is returned as text too — the model reads it and fixes its query.
    """
    try:
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            cursor = conn.execute(sql, params)
            lines = [" | ".join(column.name for column in cursor.description)]
            lines += [" | ".join(str(value) for value in row) for row in cursor.fetchmany(50)]
        return "\n".join(lines)
    except psycopg.Error as error:
        log.info("   ↳ SQL error: %s", str(error).splitlines()[0])
        return f"SQL error: {error}"


@mcp.tool()
def list_tables() -> str:
    """List the tables of the bank database with a short description of each."""
    log.info("🔧 list_tables()")
    return query("SELECT relname AS table_name, obj_description(oid) AS description "
                 "FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' ORDER BY 1")


@mcp.tool()
def describe_table(table: str) -> str:
    """Show the columns of one table: name, type and what the column means."""
    log.info("🔧 describe_table(%s)", table)
    return query("SELECT column_name, data_type, col_description(%s::regclass, ordinal_position) AS meaning "
                 "FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
                 (table, table))


@mcp.tool()
def run_sql(sql: str) -> str:
    """Run a read-only SQL query (PostgreSQL) and return up to 50 rows."""
    log.info("🔧 run_sql: %s", " ".join(sql.split())[:200])
    return query(sql)


if __name__ == "__main__":
    log.info("bank MCP server → :%s/mcp  (database: %s)", PORT, DSN.split("@")[-1])
    mcp.run(transport="streamable-http")
