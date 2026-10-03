"""On-prem Enterprise MCP Server.

A small "internal enterprise system" exposed as an MCP server. It stands in for the ERP / HR /
procurement / inventory systems whose data must NOT leave the customer's data center. The server runs
inside the data center and is reached by an agent on GreenNode AgentBase only through:

    Agent -> MCP Gateway (Private) -> customer VPC -> VPN Site-to-Site (IPsec) -> this server

Tools (all read-only, backed by a local SQLite database seeded with fictional demo data):
    find_employee(query)             search the employee directory
    leave_balance(employee_id)       annual / sick leave balance of one employee
    list_purchase_orders(status)     purchase orders, optionally filtered by status
    get_purchase_order(po_id)        one purchase order with its line items
    inventory_level(sku)             stock level of one SKU across warehouses

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

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from mcp.server.fastmcp import FastMCP
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
    email       TEXT NOT NULL,
    department  TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT NOT NULL,
    manager_id  TEXT
);
CREATE TABLE IF NOT EXISTS leave_balances (
    employee_id   TEXT PRIMARY KEY REFERENCES employees(employee_id),
    year          INTEGER NOT NULL,
    annual_total  REAL NOT NULL,
    annual_used   REAL NOT NULL,
    sick_total    REAL NOT NULL,
    sick_used     REAL NOT NULL
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
    po_id       TEXT NOT NULL REFERENCES purchase_orders(po_id),
    line_no     INTEGER NOT NULL,
    sku         TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL,
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

# All names, e-mail addresses (example.com) and figures below are fictional.
SEED_EMPLOYEES = [
    ("E1001", "Alice Nguyen", "alice.nguyen@example.com", "Finance", "Finance Director", "Ho Chi Minh City", None),
    ("E1002", "Brian Tran", "brian.tran@example.com", "Finance", "Senior Accountant", "Ho Chi Minh City", "E1001"),
    ("E1003", "Chloe Le", "chloe.le@example.com", "Procurement", "Procurement Manager", "Hanoi", "E1001"),
    ("E1004", "David Pham", "david.pham@example.com", "Procurement", "Buyer", "Hanoi", "E1003"),
    ("E1005", "Emma Vo", "emma.vo@example.com", "Engineering", "Platform Engineer", "Da Nang", "E1006"),
    ("E1006", "Felix Hoang", "felix.hoang@example.com", "Engineering", "Engineering Manager", "Da Nang", "E1001"),
    ("E1007", "Grace Do", "grace.do@example.com", "Human Resources", "HR Business Partner", "Ho Chi Minh City", "E1001"),
    ("E1008", "Henry Bui", "henry.bui@example.com", "Warehouse", "Warehouse Supervisor", "Hai Phong", "E1003"),
]

SEED_LEAVE = [
    # employee_id, year, annual_total, annual_used, sick_total, sick_used
    ("E1001", 2026, 18, 6.5, 30, 0),
    ("E1002", 2026, 14, 9, 30, 2),
    ("E1003", 2026, 16, 4, 30, 1),
    ("E1004", 2026, 12, 12, 30, 0),
    ("E1005", 2026, 12, 3.5, 30, 3),
    ("E1006", 2026, 16, 10, 30, 0),
    ("E1007", 2026, 14, 2, 30, 0),
    ("E1008", 2026, 12, 7, 30, 5),
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

SEED_PO_LINES = [
    # po_id, line_no, sku, description, quantity, unit_price
    ("PO-2026-0001", 1, "SKU-CBL-001", "Cat6a patch cable 2 m", 200, 3.20),
    ("PO-2026-0001", 2, "SKU-SWT-024", "24-port managed switch", 4, 410.00),
    ("PO-2026-0002", 1, "SKU-PPR-A4", "A4 copy paper, 5 reams", 60, 18.50),
    ("PO-2026-0003", 1, "SKU-PLT-120", "Pallet 120 x 100 cm", 300, 12.75),
    ("PO-2026-0004", 1, "SKU-SWT-024", "24-port managed switch", 2, 410.00),
    ("PO-2026-0004", 2, "SKU-UPS-1K", "UPS 1 kVA rack mount", 2, 389.90),
    ("PO-2026-0005", 1, "SKU-RAK-42U", "42U server rack", 1, 1250.00),
    ("PO-2026-0006", 1, "SKU-PLT-120", "Pallet 120 x 100 cm", 150, 12.75),
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


def init_db(path: str | None = None, *, seed: bool = True) -> None:
    """Create the schema and, when the employees table is empty, load the demo data."""
    target = path or DB_PATH
    parent = Path(target).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as con:
        con.executescript(SCHEMA)
        if seed and con.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
            con.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?)", SEED_EMPLOYEES)
            con.executemany("INSERT INTO leave_balances VALUES (?,?,?,?,?,?)", SEED_LEAVE)
            con.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?)", SEED_POS)
            con.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?)", SEED_PO_LINES)
            con.executemany("INSERT INTO inventory VALUES (?,?,?,?,?,?)", SEED_INVENTORY)
        con.commit()


def _connect_ro() -> sqlite3.Connection:
    """Read-only connection: the MCP tools can never modify the database."""
    if not Path(DB_PATH).exists():
        init_db()  # first request when the server is not started through __main__
    uri = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _query(sql: str, params: tuple = ()) -> list[dict]:
    with closing(_connect_ro()) as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------------------
# Input validation helpers
# --------------------------------------------------------------------------------------

EMPLOYEE_ID_RE = re.compile(r"^E\d{4}$")
PO_ID_RE = re.compile(r"^PO-\d{4}-\d{4}$")
SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,23}$")
PO_STATUSES = ("pending_approval", "approved", "received", "cancelled")
MAX_QUERY_LEN = 64
MAX_RESULTS = 20


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _like_pattern(text: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --------------------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------------------

mcp = FastMCP(SERVER_NAME, stateless_http=True, json_response=True, host="0.0.0.0")


@mcp.tool()
def find_employee(query: str) -> str:
    """Search the employee directory by name, e-mail or department (case-insensitive substring).

    query: 2-64 characters, for example "nguyen", "procurement" or "felix.hoang".
    Returns at most 20 matches with employee_id, name, e-mail, department, title, location and manager.
    """
    q = (query or "").strip()
    if not 2 <= len(q) <= MAX_QUERY_LEN:
        return _err(f"query must be 2-{MAX_QUERY_LEN} characters")
    pattern = _like_pattern(q)
    rows = _query(
        "SELECT employee_id, full_name, email, department, title, location, manager_id "
        "FROM employees "
        "WHERE full_name LIKE ?1 ESCAPE '\\' OR email LIKE ?1 ESCAPE '\\' OR department LIKE ?1 ESCAPE '\\' "
        "ORDER BY employee_id LIMIT ?2",
        (pattern, MAX_RESULTS),
    )
    return _ok({"query": q, "count": len(rows), "employees": rows})


@mcp.tool()
def leave_balance(employee_id: str) -> str:
    """Leave balance (annual and sick, in days) of one employee for the current leave year.

    employee_id: format E1001 (use find_employee to look it up).
    """
    eid = (employee_id or "").strip().upper()
    if not EMPLOYEE_ID_RE.match(eid):
        return _err("employee_id must look like E1001 (use find_employee to look it up)")
    rows = _query(
        "SELECT e.employee_id, e.full_name, l.year, l.annual_total, l.annual_used, l.sick_total, l.sick_used "
        "FROM leave_balances l JOIN employees e ON e.employee_id = l.employee_id "
        "WHERE l.employee_id = ?",
        (eid,),
    )
    if not rows:
        return _err(f"no leave record for employee {eid}")
    r = rows[0]
    return _ok({
        "employee_id": r["employee_id"],
        "name": r["full_name"],
        "year": r["year"],
        "annual": {
            "entitled_days": r["annual_total"],
            "used_days": r["annual_used"],
            "remaining_days": round(r["annual_total"] - r["annual_used"], 2),
        },
        "sick": {
            "entitled_days": r["sick_total"],
            "used_days": r["sick_used"],
            "remaining_days": round(r["sick_total"] - r["sick_used"], 2),
        },
    })


@mcp.tool()
def list_purchase_orders(status: str = "all") -> str:
    """List purchase orders (newest first, at most 20) with their total amount.

    status: one of pending_approval | approved | received | cancelled | all (default all).
    """
    st = (status or "all").strip().lower()
    if st != "all" and st not in PO_STATUSES:
        return _err(f"status must be one of: {', '.join(PO_STATUSES)}, all")
    where = "" if st == "all" else "WHERE p.status = ?"
    params: tuple = () if st == "all" else (st,)
    rows = _query(
        "SELECT p.po_id, p.vendor, p.status, p.currency, p.created_on, p.requester_id, "
        "       ROUND(COALESCE(SUM(l.quantity * l.unit_price), 0), 2) AS total_amount "
        "FROM purchase_orders p LEFT JOIN po_lines l ON l.po_id = p.po_id "
        f"{where} GROUP BY p.po_id ORDER BY p.created_on DESC, p.po_id DESC LIMIT {MAX_RESULTS}",
        params,
    )
    return _ok({"status_filter": st, "count": len(rows), "purchase_orders": rows})


@mcp.tool()
def get_purchase_order(po_id: str) -> str:
    """One purchase order with its line items and total amount.

    po_id: format PO-2026-0001 (use list_purchase_orders to find it).
    """
    pid = (po_id or "").strip().upper()
    if not PO_ID_RE.match(pid):
        return _err("po_id must look like PO-2026-0001 (use list_purchase_orders to find it)")
    header = _query(
        "SELECT p.po_id, p.vendor, p.status, p.currency, p.created_on, p.requester_id, e.full_name AS requester "
        "FROM purchase_orders p JOIN employees e ON e.employee_id = p.requester_id WHERE p.po_id = ?",
        (pid,),
    )
    if not header:
        return _err(f"purchase order {pid} not found")
    lines = _query(
        "SELECT line_no, sku, description, quantity, unit_price, ROUND(quantity * unit_price, 2) AS line_total "
        "FROM po_lines WHERE po_id = ? ORDER BY line_no",
        (pid,),
    )
    po = header[0]
    po["lines"] = lines
    po["total_amount"] = round(sum(item["line_total"] for item in lines), 2)
    return _ok(po)


@mcp.tool()
def inventory_level(sku: str) -> str:
    """Stock level of one SKU per warehouse: on hand, reserved, available and reorder status.

    sku: for example SKU-SWT-024 (letters, digits and hyphens, 3-24 characters).
    """
    s = (sku or "").strip().upper()
    if not SKU_RE.match(s):
        return _err("sku must be 3-24 characters: letters, digits and hyphens, for example SKU-SWT-024")
    rows = _query(
        "SELECT warehouse, description, on_hand, reserved, reorder_level "
        "FROM inventory WHERE sku = ? ORDER BY warehouse",
        (s,),
    )
    if not rows:
        return _err(f"unknown SKU {s}")
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
    return _ok({
        "sku": s,
        "description": rows[0]["description"],
        "total_available": sum(w["available"] for w in warehouses),
        "warehouses": warehouses,
    })


TOOL_NAMES = ["find_employee", "leave_balance", "list_purchase_orders", "get_purchase_order", "inventory_level"]

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
