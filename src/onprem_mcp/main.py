"""On-prem Enterprise MCP Server.

A small "internal enterprise system" exposed as an MCP server. It stands in for the ERP / HR /
procurement / inventory systems whose data must NOT leave the customer's data center. The server runs
inside the data center and is reached by an agent on GreenNode AgentBase only through:

    Agent -> MCP Gateway (Private) -> customer VPC -> VPN Site-to-Site (IPsec) -> this server

Tools (all read-only, backed by a local SQLite database seeded with fictional demo data):
    find_employee(query)             search the employee directory (id, name, department, title)
    leave_balance(employee_id)       annual leave balance of one employee
    sick_leave_balance(employee_id)  sick leave balance of one employee (separate tool, separate policy)
    list_purchase_orders(status)     purchase orders, optionally filtered by status
    get_purchase_order(po_id)        one purchase order with its line items
    inventory_level(sku)             stock level of one SKU across warehouses

Every tool returns a JSON object (structured output). A failed call raises ToolError, so the MCP result has
`isError: true` and a plain-text message.

Transport: MCP streamable HTTP at /mcp, health probe at GET /health, port from env PORT (default 8080).

Authentication (fail-closed): /mcp requires an API key.
  - MCP_API_KEYS="key1,key2"  (several keys at once allow zero-downtime rotation)
  - Header `X-Api-Key: <key>` or `Authorization: Bearer <key>`
  - No key configured -> /mcp answers 503 (the server never opens itself up). Only for local
    development set ALLOW_ANONYMOUS=true.
  - /health is always open (load balancers and health probes).
  Behind an MCP Gateway connector, Outbound Auth = API Key adds the header `X-Api-Key`; the agent never
  sees the key.

Audit log: every authenticated tools/call is logged as `audit tool=<name> caller=<ip>`.
Tool arguments and secrets are never logged.
"""

import json
import logging
import os
import secrets
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("onprem-mcp")
audit_log = logging.getLogger("onprem-mcp.audit")

# --------------------------------------------------------------------------------------
# Configuration (environment variables)
# --------------------------------------------------------------------------------------

SERVER_NAME = "onprem-erp-mcp"
DB_PATH = os.environ.get("DB_PATH", "data/erp.db")


def _load_api_keys() -> list[str]:
    raw = os.environ.get("MCP_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


API_KEYS = _load_api_keys()
ALLOW_ANONYMOUS = _env_flag("ALLOW_ANONYMOUS")
# Only enable when the server sits behind a reverse proxy you control (for example the Caddy TLS profile):
# the audit log then records the client address from the last X-Forwarded-For entry.
TRUST_FORWARDED_FOR = _env_flag("TRUST_FORWARDED_FOR")
MAX_BODY_BYTES = 1024 * 1024  # request bodies larger than 1 MiB are rejected on /mcp

for _k in API_KEYS:
    if len(_k) < 24:
        log.warning("MCP_API_KEYS contains a key shorter than 24 characters; use `openssl rand -hex 32`")

# --------------------------------------------------------------------------------------
# Database: schema + fictional demo data
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    department  TEXT NOT NULL,
    title       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leave_balances (
    employee_id   TEXT NOT NULL REFERENCES employees(employee_id),
    year          INTEGER NOT NULL,
    annual_total  REAL NOT NULL,
    annual_used   REAL NOT NULL,
    sick_total    REAL NOT NULL,
    sick_used     REAL NOT NULL,
    PRIMARY KEY (employee_id, year)
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id        TEXT PRIMARY KEY,
    vendor       TEXT NOT NULL,
    status       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    created_on   TEXT NOT NULL,
    requester_id TEXT NOT NULL REFERENCES employees(employee_id)
);
CREATE TABLE IF NOT EXISTS po_lines (
    po_id            TEXT NOT NULL REFERENCES purchase_orders(po_id),
    line_no          INTEGER NOT NULL,
    sku              TEXT NOT NULL,
    description      TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    PRIMARY KEY (po_id, line_no)
);
CREATE TABLE IF NOT EXISTS inventory (
    sku           TEXT NOT NULL,
    warehouse     TEXT NOT NULL,
    description   TEXT NOT NULL,
    on_hand       INTEGER NOT NULL,
    reserved      INTEGER NOT NULL,
    reorder_level INTEGER NOT NULL,
    PRIMARY KEY (sku, warehouse)
);
"""

# All names and figures below are fictional.
SEED_EMPLOYEES = [
    # employee_id, full_name, department, title
    ("E1001", "Alice Nguyen", "Finance", "Finance Director"),
    ("E1002", "Brian Tran", "Finance", "Senior Accountant"),
    ("E1003", "Chloe Le", "Procurement", "Procurement Manager"),
    ("E1004", "David Pham", "Procurement", "Buyer"),
    ("E1005", "Emma Vo", "Engineering", "Platform Engineer"),
    ("E1006", "Felix Hoang", "Engineering", "Engineering Manager"),
    ("E1007", "Grace Do", "Human Resources", "HR Business Partner"),
    ("E1008", "Henry Bui", "Warehouse", "Warehouse Supervisor"),
]

# The leave year is not stored here: the rows are seeded for the current year (see init_db).
SEED_LEAVE = [
    # employee_id, annual_total, annual_used, sick_total, sick_used
    ("E1001", 18, 6.5, 30, 0),
    ("E1002", 14, 9, 30, 2),
    ("E1003", 16, 4, 30, 1),
    ("E1004", 12, 12, 30, 0),
    ("E1005", 12, 3.5, 30, 3),
    ("E1006", 16, 10, 30, 0),
    ("E1007", 14, 2, 30, 0),
    ("E1008", 12, 7, 30, 5),
]

SEED_POS = [
    # po_id, vendor, status, currency, created_on, requester_id
    ("PO-2026-0001", "Northwind Components", "approved", "USD", "2026-01-12", "E1004"),
    ("PO-2026-0002", "Contoso Office Supply", "pending_approval", "USD", "2026-02-03", "E1003"),
    ("PO-2026-0003", "Fabrikam Logistics", "received", "USD", "2026-02-18", "E1008"),
    ("PO-2026-0004", "Northwind Components", "pending_approval", "USD", "2026-03-07", "E1004"),
    ("PO-2026-0005", "Adventure Works Hardware", "cancelled", "USD", "2026-03-21", "E1005"),
    ("PO-2026-0006", "Fabrikam Logistics", "approved", "USD", "2026-04-02", "E1008"),
]

# Money is stored as integer cents so that line totals and PO totals are exact (no floating-point rounding).
SEED_PO_LINES = [
    # po_id, line_no, sku, description, quantity, unit_price_cents
    ("PO-2026-0001", 1, "SKU-CBL-001", "Cat6a patch cable 2 m", 200, 320),
    ("PO-2026-0001", 2, "SKU-SWT-024", "24-port managed switch", 4, 41000),
    ("PO-2026-0002", 1, "SKU-PPR-A4", "A4 copy paper, 5 reams", 60, 1850),
    ("PO-2026-0003", 1, "SKU-PLT-120", "Pallet 120 x 100 cm", 300, 1275),
    ("PO-2026-0004", 1, "SKU-SWT-024", "24-port managed switch", 2, 41000),
    ("PO-2026-0004", 2, "SKU-UPS-1K", "UPS 1 kVA rack mount", 2, 38990),
    ("PO-2026-0005", 1, "SKU-RAK-42U", "42U server rack", 1, 125000),
    ("PO-2026-0006", 1, "SKU-PLT-120", "Pallet 120 x 100 cm", 150, 1275),
]

SEED_INVENTORY = [
    # sku, warehouse, description, on_hand, reserved, reorder_level
    ("SKU-CBL-001", "WH-HCM", "Cat6a patch cable 2 m", 850, 120, 300),
    ("SKU-CBL-001", "WH-HAN", "Cat6a patch cable 2 m", 140, 0, 300),
    ("SKU-SWT-024", "WH-HCM", "24-port managed switch", 12, 4, 6),
    ("SKU-PPR-A4", "WH-HAN", "A4 copy paper, 5 reams", 35, 10, 50),
    ("SKU-PLT-120", "WH-HPG", "Pallet 120 x 100 cm", 640, 150, 200),
    ("SKU-UPS-1K", "WH-HCM", "UPS 1 kVA rack mount", 3, 2, 4),
    ("SKU-RAK-42U", "WH-HCM", "42U server rack", 2, 0, 1),
]

# Asia/Ho_Chi_Minh is UTC+7 all year round (no daylight saving), so a fixed offset needs no tz database.
VIETNAM_TZ = timezone(timedelta(hours=7))


def _current_year() -> int:
    """The leave year that leave tools report: the current calendar year in Vietnam."""
    return datetime.now(VIETNAM_TZ).year


def init_db(path: str | None = None) -> None:
    """Create the schema (idempotent) and, when the employees table is empty, load the demo data."""
    target = path or DB_PATH
    Path(target).resolve().parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as con:
        con.executescript(SCHEMA)
        if con.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
            year = _current_year()
            con.executemany("INSERT INTO employees VALUES (?,?,?,?)", SEED_EMPLOYEES)
            con.executemany("INSERT INTO leave_balances VALUES (?,?,?,?,?,?)",
                            [(eid, year, *days) for eid, *days in SEED_LEAVE])
            con.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?)", SEED_POS)
            con.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?)", SEED_PO_LINES)
            con.executemany("INSERT INTO inventory VALUES (?,?,?,?,?,?)", SEED_INVENTORY)
        con.commit()


def _fold(text: str) -> str:
    """Case-insensitive comparison key. SQLite lower()/LIKE only fold ASCII, so "Ánh" would not match "ánh"."""
    return unicodedata.normalize("NFC", text).casefold()


def _connect_ro() -> sqlite3.Connection:
    """Read-only connection: the MCP tools can never modify the database."""
    uri = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.create_function("fold", 1, _fold, deterministic=True)
    return con


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with closing(_connect_ro()) as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


MAX_RESULTS = 20  # list-style tools return at most this many rows and report `total` / `truncated`


def _query_capped(columns: str, source: str, params: tuple, order_by: str) -> tuple[list[dict], int]:
    """Up to MAX_RESULTS rows of `SELECT columns FROM source` plus the number of ALL matching rows.

    The SQL fragments are constants of this module; user input only ever travels in `params`.
    """
    with closing(_connect_ro()) as con:
        total = con.execute(f"SELECT COUNT(*) FROM {source}", params).fetchone()[0]
        rows = con.execute(f"SELECT {columns} FROM {source} ORDER BY {order_by} LIMIT {MAX_RESULTS}", params)
        return [dict(r) for r in rows], total


# --------------------------------------------------------------------------------------
# Tool argument types (the constraints end up in the tool's JSON schema and are enforced on every call)
# --------------------------------------------------------------------------------------

SearchText = Annotated[
    str,
    StringConstraints(strip_whitespace=True),
    Field(min_length=2, max_length=64,
          description='Part of a name or department, any letter case, for example "nguyen" or "procurement".'),
]
EmployeeId = Annotated[
    str,
    Field(pattern=r"^E[0-9]{4}$", min_length=5, max_length=5,
          description="Employee id, for example E1001 (use find_employee to look it up)."),
]
PoId = Annotated[
    str,
    Field(pattern=r"^PO-[0-9]{4}-[0-9]{4}$", min_length=12, max_length=12,
          description="Purchase order id, for example PO-2026-0001 (use list_purchase_orders to find it)."),
]
Sku = Annotated[
    str,
    Field(pattern=r"^[A-Z0-9][A-Z0-9-]{2,23}$", min_length=3, max_length=24,
          description="Stock keeping unit: upper-case letters, digits and hyphens, for example SKU-SWT-024."),
]
PoStatus = Literal["pending_approval", "approved", "received", "cancelled", "all"]

# Every tool only reads from a closed, local database.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _like_pattern(text: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _money(cents: int) -> float:
    return cents / 100


# --------------------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------------------

# host="0.0.0.0" keeps the SDK from switching on its localhost-only DNS-rebinding protection (the default for
# 127.0.0.1): behind the MCP Gateway or Caddy the Host header is an internal IP or name, which that protection
# would reject with 421. Requests are protected by the API key instead. The listen address itself is set
# by HOST in uvicorn.run() below.
mcp = FastMCP(SERVER_NAME, stateless_http=True, json_response=True, host="0.0.0.0")


@mcp.tool(annotations=READ_ONLY)
def find_employee(query: SearchText) -> dict[str, Any]:
    """Search the employee directory by name or department (case-insensitive substring).

    Returns at most 20 matches (employee_id, name, department, title). `total` is the number of all matches
    and `truncated` is true when some were left out: use a more specific query.
    """
    pattern = _like_pattern(_fold(query))
    employees, total = _query_capped(
        "employee_id, full_name AS name, department, title",
        "employees WHERE fold(full_name) LIKE ?1 ESCAPE '\\' OR fold(department) LIKE ?1 ESCAPE '\\'",
        (pattern,),
        "employee_id",
    )
    return {"query": query, "total": total, "truncated": total > len(employees), "employees": employees}


def _leave_balance(employee_id: str, kind: Literal["annual", "sick"]) -> dict[str, Any]:
    """Shared by the annual and the sick leave tool: one employee, current leave year."""
    year = _current_year()
    rows = _query(
        "SELECT e.employee_id, e.full_name AS name, l.annual_total, l.annual_used, l.sick_total, l.sick_used "
        "FROM leave_balances l JOIN employees e ON e.employee_id = l.employee_id "
        "WHERE l.employee_id = ? AND l.year = ?",
        (employee_id, year),
    )
    if not rows:
        raise ToolError(f"no {kind} leave record for employee {employee_id} in {year}")
    row = rows[0]
    entitled, used = row[f"{kind}_total"], row[f"{kind}_used"]
    return {
        "employee_id": row["employee_id"],
        "name": row["name"],
        "year": year,
        "entitled_days": entitled,
        "used_days": used,
        "remaining_days": round(entitled - used, 2),
    }


@mcp.tool(annotations=READ_ONLY)
def leave_balance(employee_id: EmployeeId) -> dict[str, Any]:
    """Annual leave balance in days of one employee for the current calendar year (Asia/Ho_Chi_Minh)."""
    return _leave_balance(employee_id, "annual")


@mcp.tool(annotations=READ_ONLY)
def sick_leave_balance(employee_id: EmployeeId) -> dict[str, Any]:
    """Sick leave balance in days of one employee for the current calendar year (Asia/Ho_Chi_Minh).

    A separate tool from leave_balance on purpose: gateway policies allow or deny whole tools, so a Policy Group
    can grant annual leave without exposing health-related data.
    """
    return _leave_balance(employee_id, "sick")


@mcp.tool(annotations=READ_ONLY)
def list_purchase_orders(status: PoStatus = "all") -> dict[str, Any]:
    """List purchase orders (newest first) with their total amount.

    Returns at most 20 orders. `total` is the number of all matching orders and `truncated` is true when
    some were left out: filter by status.
    """
    where = "" if status == "all" else "WHERE p.status = ?"
    rows, total = _query_capped(
        "p.po_id, p.vendor, p.status, p.currency, p.created_on, p.requester_id, "
        "COALESCE((SELECT SUM(l.quantity * l.unit_price_cents) FROM po_lines l WHERE l.po_id = p.po_id), 0)"
        " AS total_cents",
        f"purchase_orders p {where}",
        () if status == "all" else (status,),
        "p.created_on DESC, p.po_id DESC",
    )
    orders = []
    for row in rows:
        total_cents = row.pop("total_cents")
        orders.append({**row, "total_amount": _money(total_cents)})
    return {"status_filter": status, "total": total, "truncated": total > len(orders), "purchase_orders": orders}


@mcp.tool(annotations=READ_ONLY)
def get_purchase_order(po_id: PoId) -> dict[str, Any]:
    """One purchase order with its line items and total amount."""
    header = _query(
        "SELECT p.po_id, p.vendor, p.status, p.currency, p.created_on, p.requester_id, e.full_name AS requester_name "
        "FROM purchase_orders p LEFT JOIN employees e ON e.employee_id = p.requester_id WHERE p.po_id = ?",
        (po_id,),
    )
    if not header:
        raise ToolError(f"purchase order {po_id} not found")
    lines = _query(
        "SELECT line_no, sku, description, quantity, unit_price_cents FROM po_lines WHERE po_id = ? ORDER BY line_no",
        (po_id,),
    )
    return {
        **header[0],
        "lines": [{
            "line_no": ln["line_no"],
            "sku": ln["sku"],
            "description": ln["description"],
            "quantity": ln["quantity"],
            "unit_price": _money(ln["unit_price_cents"]),
            "line_total": _money(ln["quantity"] * ln["unit_price_cents"]),
        } for ln in lines],
        "total_amount": _money(sum(ln["quantity"] * ln["unit_price_cents"] for ln in lines)),
    }


@mcp.tool(annotations=READ_ONLY)
def inventory_level(sku: Sku) -> dict[str, Any]:
    """Stock level of one SKU per warehouse: on hand, reserved, available and reorder status."""
    rows = _query(
        "SELECT warehouse, description, on_hand, reserved, reorder_level "
        "FROM inventory WHERE sku = ? ORDER BY warehouse",
        (sku,),
    )
    if not rows:
        raise ToolError(f"unknown SKU {sku}")
    warehouses = []
    for r in rows:
        available = r["on_hand"] - r["reserved"]
        warehouses.append({
            "warehouse": r["warehouse"],
            "on_hand": r["on_hand"],
            "reserved": r["reserved"],
            "available": available,
            "reorder_level": r["reorder_level"],
            "needs_reorder": available <= r["reorder_level"],
        })
    return {
        "sku": sku,
        "description": rows[0]["description"],
        "total_available": sum(w["available"] for w in warehouses),
        "warehouses": warehouses,
    }


TOOL_NAMES = ["find_employee", "leave_balance", "sick_leave_balance", "list_purchase_orders", "get_purchase_order", "inventory_level"]

# --------------------------------------------------------------------------------------
# HTTP app: /mcp (streamable HTTP) + /health, wrapped by audit and fail-closed auth middleware
# --------------------------------------------------------------------------------------


def _auth_mode() -> str:
    if API_KEYS:
        return f"api-key ({len(API_KEYS)} key)"
    return "anonymous (ALLOW_ANONYMOUS, local use only)" if ALLOW_ANONYMOUS else "locked (MCP_API_KEYS not set)"


async def health(request):
    return JSONResponse({
        "status": "ok",
        "server": SERVER_NAME,
        "tools": len(TOOL_NAMES),
        "mcp_endpoint": "/mcp",
        "mcp_auth": _auth_mode(),
    })


# streamable_http_app() returns a Starlette app whose lifespan runs the MCP session manager.
# Add the extra routes to THIS app (mounting it inside another app would skip its lifespan).
asgi_app = mcp.streamable_http_app()
asgi_app.router.routes.append(Route("/health", health, methods=["GET"]))


def _is_mcp_path(path: str) -> bool:
    return path.rstrip("/") == "/mcp" or path.startswith("/mcp/")


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def _extract_key(headers: dict[str, str]) -> str:
    if headers.get("x-api-key"):
        return headers["x-api-key"].strip()
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _key_valid(supplied: str) -> bool:
    if not supplied:
        return False
    ok = False
    for good in API_KEYS:  # visit every key so timing does not reveal which one matched
        ok |= secrets.compare_digest(supplied.encode(), good.encode())
    return ok


def _caller_ip(scope, headers: dict[str, str]) -> str:
    peer = (scope.get("client") or ("unknown",))[0]
    if TRUST_FORWARDED_FOR and headers.get("x-forwarded-for"):
        return headers["x-forwarded-for"].split(",")[-1].strip() or peer
    return peer


async def _reply(send, status: int, message: str, extra=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), *extra]})
    await send({"type": "http.response.body", "body": json.dumps({"error": message}).encode()})


class RequireApiKeyMiddleware:
    """Fail-closed API key check for /mcp.

    - MCP_API_KEYS set                 -> a valid key is mandatory (401 when missing or wrong).
    - No key and ALLOW_ANONYMOUS=true  -> open (local development only).
    - No key and no ALLOW_ANONYMOUS    -> 503, the server never opens itself up.
    - /health is always open.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and _is_mcp_path(scope.get("path", "")):
            if not API_KEYS:
                if not ALLOW_ANONYMOUS:
                    return await _reply(send, 503, "MCP server has no MCP_API_KEYS configured (fail-closed)")
            else:
                headers = _headers(scope)
                if not _key_valid(_extract_key(headers)):
                    log.warning("401 on /mcp from %s: missing or invalid API key", _caller_ip(scope, headers))
                    return await _reply(send, 401, "missing or invalid API key (X-Api-Key / Bearer)",
                                        [(b"www-authenticate", b'Bearer realm="onprem-erp-mcp"')])
        await self.app(scope, receive, send)


def _tool_calls(body: bytes) -> list[str]:
    """Tool names of every JSON-RPC `tools/call` message in a request body (single or batch)."""
    try:
        data = json.loads(body)
    except ValueError:
        return []
    messages = data if isinstance(data, list) else [data]
    names = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            name = (msg.get("params") or {}).get("name")
            if isinstance(name, str):
                names.append(name[:64])
    return names


class AuditMiddleware:
    """Log `tool=<name> caller=<ip>` for every tools/call. Arguments and headers are never logged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not (scope.get("type") == "http" and scope.get("method") == "POST"
                and _is_mcp_path(scope.get("path", ""))):
            return await self.app(scope, receive, send)

        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return await _reply(send, 413, "request body too large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        caller = _caller_ip(scope, _headers(scope))
        for name in _tool_calls(body):
            audit_log.info("audit tool=%s caller=%s", name, caller)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# Order matters: authentication first, so only authenticated callers reach the audit layer.
app = RequireApiKeyMiddleware(AuditMiddleware(asgi_app))


if __name__ == "__main__":
    import uvicorn

    init_db()
    log.info("%s: %d tools, auth: %s", SERVER_NAME, len(TOOL_NAMES), _auth_mode())
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
