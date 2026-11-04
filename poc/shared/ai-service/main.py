"""AI report service: turns a question in plain language into SQL with a local
Ollama model, checks the SQL, and runs it with the user's branch enforced by
PostgreSQL row-level security.

Only the table structure is sent to the model, never the data.
Each branch uses its own database login, so PostgreSQL row-level security
limits every query to that branch no matter what SQL the model writes.
"""

import json
import logging
import os
import re
from decimal import Decimal
from datetime import date, datetime

import httpx
import psycopg
import sqlglot
from sqlglot import exp
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
# One database login per branch ("ai_tehran", ...; "ai_all" for managers).
# The database decides from the login which rows are visible.
DATA_DB_DSN_TEMPLATE = os.environ.get(
    "DATA_DB_DSN_TEMPLATE", "postgresql://ai_{role}:ai_reader@sample-db:5432/sales"
)
SERVICE_TOKEN = os.environ.get("AI_SERVICE_TOKEN", "change-me")
ROW_LIMIT = int(os.environ.get("AI_ROW_LIMIT", "1000"))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("ai-service")

app = FastAPI(title="AI report service")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    branch: str = Field(min_length=1, max_length=64)
    user: str = Field(min_length=1, max_length=128)


class ChartSpec(BaseModel):
    type: str = "table"
    x: str | None = None
    y: str | None = None
    title: str = ""


class AskResponse(BaseModel):
    sql: str
    chart: ChartSpec
    columns: list[str]
    rows: list[list]


class BadSql(Exception):
    pass


BLOCKED_FUNCTIONS = {
    "set_config", "current_setting", "query_to_xml", "query_to_xml_and_xmlschema",
    "table_to_xml", "database_to_xml", "schema_to_xml", "cursor_to_xml",
}


def dsn_for(branch: str) -> str:
    return DATA_DB_DSN_TEMPLATE.format(role="all" if branch == "*" else branch)


def schema_description(branch: str) -> str:
    """Describe the tables this branch's role can read, with column types."""
    query = """
        SELECT c.table_name, c.column_name, c.data_type
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND has_table_privilege(quote_ident(c.table_schema) || '.' || quote_ident(c.table_name), 'SELECT')
        ORDER BY c.table_name, c.ordinal_position
    """
    tables: dict[str, list[str]] = {}
    with psycopg.connect(dsn_for(branch)) as conn:
        for table, column, dtype in conn.execute(query):
            tables.setdefault(table, []).append(f"{column} {dtype}")
    return "\n".join(f"{t}({', '.join(cols)})" for t, cols in tables.items())


SYSTEM_PROMPT = """You write PostgreSQL queries for a business reporting tool.
Database tables:
{schema}

Notes:
- sales.branch references branches.code; sales.product_id references products.id.
- sales.amount is the money value of a sale.
- Never filter by branch yourself: access control is applied automatically.

Reply with a JSON object only, with these keys:
  "sql":   one PostgreSQL SELECT statement, no trailing semicolon,
  "chart": one of "bar", "line", "pie", "number", "table",
  "x":     the column used for labels or the time axis (or null),
  "y":     the numeric column to plot (or null),
  "title": a short title in the same language as the question.
Give every computed column a simple snake_case alias."""


def ask_model(question: str, schema: str, previous_error: str | None) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(schema=schema)},
        {"role": "user", "content": question},
    ]
    if previous_error:
        messages.append(
            {
                "role": "user",
                "content": f"The previous query failed with: {previous_error}\n"
                "Fix it and reply with the JSON object again.",
            }
        )
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=300,
    )
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise BadSql(f"model did not return JSON: {content[:200]}") from e


def check_sql(sql: str) -> str:
    """Allow exactly one read-only SELECT and add a row limit."""
    sql = sql.strip().rstrip(";")
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as e:
        raise BadSql(f"SQL does not parse: {e}") from e
    if len(statements) != 1 or statements[0] is None:
        raise BadSql("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise BadSql("only SELECT queries are allowed")
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
                 exp.Alter, exp.Command, exp.Into)
    if any(tree.find(f) for f in forbidden):
        raise BadSql("only SELECT queries are allowed")
    # Defence in depth: the database already limits rows by login, but also
    # keep generated queries away from settings and server internals.
    for func in tree.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in BLOCKED_FUNCTIONS or name.startswith(("pg_", "lo_", "dblink")):
            raise BadSql(f"function {name} is not allowed")
    return f"SELECT * FROM ({sql}) AS ai_query LIMIT {ROW_LIMIT}"


def to_json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def run_sql(sql: str, branch: str) -> tuple[list[str], list[list]]:
    with psycopg.connect(dsn_for(branch)) as conn:
        with conn.transaction():
            cur = conn.execute(sql)
            columns = [d.name for d in cur.description]
            rows = [[to_json_value(v) for v in row] for row in cur.fetchall()]
    return columns, rows


@app.get("/health")
def health():
    return {"ok": True, "model": OLLAMA_MODEL}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, x_service_token: str = Header(default="")):
    if x_service_token != SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="bad service token")
    if not re.fullmatch(r"[a-z0-9_]+|\*", req.branch):
        raise HTTPException(status_code=400, detail="bad branch value")

    schema = schema_description(req.branch)
    error = None
    for attempt in range(2):
        try:
            answer = ask_model(req.question, schema, error)
            safe_sql = check_sql(str(answer.get("sql", "")))
            columns, rows = run_sql(safe_sql, req.branch)
        except (BadSql, psycopg.Error) as e:
            error = str(e).splitlines()[0]
            log.info(json.dumps({"event": "ai_retry", "user": req.user,
                                 "attempt": attempt, "error": error}))
            continue
        chart = ChartSpec(
            type=answer.get("chart") or "table",
            x=answer.get("x"),
            y=answer.get("y"),
            title=answer.get("title") or req.question,
        )
        log.info(json.dumps({"event": "ai_ask", "user": req.user, "branch": req.branch,
                             "question": req.question, "sql": answer["sql"],
                             "rows": len(rows)}, ensure_ascii=False))
        return AskResponse(sql=answer["sql"], chart=chart, columns=columns, rows=rows)

    raise HTTPException(status_code=422, detail=f"could not build a working query: {error}")
