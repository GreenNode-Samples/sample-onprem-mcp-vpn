"""Tool tests: every call goes through the real MCP HTTP path (schema validation, ToolError -> isError,
structured output) against a temporary SQLite database seeded with the demo data."""

import json
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime

import pytest

EXPECTED_TOOLS = {
    "find_employee", "leave_balance", "sick_leave_balance",
    "list_purchase_orders", "get_purchase_order", "inventory_level",
}


def write_sql(m, statement: str, params: tuple = ()) -> None:
    """Modify the temporary database directly (the tools themselves are read-only)."""
    with closing(sqlite3.connect(m.DB_PATH)) as con:
        con.execute(statement, params)
        con.commit()


# ----------------------------- database -----------------------------


def test_seed_is_idempotent(m, temp_db):
    m.init_db(str(temp_db))  # second call must not duplicate rows
    with closing(sqlite3.connect(temp_db)) as con:
        assert con.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 8
        assert con.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 6
        assert con.execute("SELECT COUNT(*) FROM leave_balances").fetchone()[0] == 8


def test_empty_database_file_is_initialised(m, tmp_path, monkeypatch, tool):
    """A zero-byte file (for example created by `touch`) has no tables: init_db must build and seed it."""
    empty = tmp_path / "nested" / "empty.db"
    empty.parent.mkdir()
    empty.touch()
    monkeypatch.setattr(m, "DB_PATH", str(empty))
    m.init_db()
    assert tool("find_employee", {"query": "alice"})["total"] == 1


def test_init_db_creates_missing_directory(m, tmp_path):
    target = tmp_path / "a" / "b" / "erp.db"
    m.init_db(str(target))
    assert target.exists()


def test_tool_connection_is_read_only(m):
    with closing(m._connect_ro()) as con, pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM employees")


# ----------------------------- registry and schemas -----------------------------


def test_tool_registry(client):
    from conftest import AUTH

    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=AUTH)
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == EXPECTED_TOOLS
    for t in tools.values():
        assert t["annotations"]["readOnlyHint"] is True
        assert t["outputSchema"]["type"] == "object"
    assert tools["leave_balance"]["inputSchema"]["properties"]["employee_id"]["pattern"] == "^E[0-9]{4}$"
    assert tools["list_purchase_orders"]["inputSchema"]["properties"]["status"]["enum"] == [
        "pending_approval", "approved", "received", "cancelled", "all"]


def test_successful_results_are_dicts_not_json_strings(call_tool):
    result = call_tool("leave_balance", {"employee_id": "E1002"})
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["employee_id"] == "E1002"  # not wrapped in {"result": "<json string>"}
    assert json.loads(result["content"][0]["text"]) == structured


def test_errors_set_is_error_with_plain_text(call_tool):
    result = call_tool("leave_balance", {"employee_id": "E9999"})
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("Error executing tool leave_balance: no annual leave record")


def test_unknown_tool_is_error(call_tool):
    assert call_tool("drop_tables")["isError"] is True


# ----------------------------- find_employee -----------------------------


def test_find_employee_by_name(tool):
    data = tool("find_employee", {"query": "nguyen"})
    assert data["total"] == 1 and data["truncated"] is False
    assert data["employees"] == [
        {"employee_id": "E1001", "name": "Alice Nguyen", "department": "Finance", "title": "Finance Director"}]


def test_find_employee_by_department_case_insensitive(tool):
    data = tool("find_employee", {"query": "PROCUREMENT"})
    assert {e["employee_id"] for e in data["employees"]} == {"E1003", "E1004"}


def test_find_employee_returns_no_contact_data(tool):
    """PII minimisation: id, name, department and title only (no e-mail, manager or location)."""
    for query in ("nguyen", "procurement", "engineering", "an"):
        data = tool("find_employee", {"query": query})
        assert data["employees"]
        for employee in data["employees"]:
            assert set(employee) == {"employee_id", "name", "department", "title"}
        assert "@" not in json.dumps(data)


def test_find_employee_does_not_search_e_mail(m, tool):
    """Regression: "om" used to match every row through the `example.com` addresses."""
    assert tool("find_employee", {"query": "om"})["total"] == 0
    assert tool("find_employee", {"query": "example.com"})["total"] == 0
    assert "email" not in m.SCHEMA.lower()


@pytest.mark.parametrize("stored", ["NFC", "NFD"])
def test_find_employee_vietnamese_case_insensitive(m, tool, stored):
    """SQLite lower()/LIKE only fold ASCII: "ánh" must still find "Ánh", whatever the Unicode form stored."""
    name, department = "Nguyễn Thị Ánh", "Kế toán"
    write_sql(m, "INSERT INTO employees VALUES (?,?,?,?)",
              ("E2000", unicodedata.normalize(stored, name), unicodedata.normalize(stored, department), "Accountant"))
    for query in ("ánh", "ÁNH", "thị ánh", "kế toán", "KẾ TOÁN"):
        assert [e["employee_id"] for e in tool("find_employee", {"query": query})["employees"]] == ["E2000"], query
    assert tool("find_employee", {"query": "anh"})["total"] == 0  # diacritics are significant


def test_find_employee_no_match(tool):
    assert tool("find_employee", {"query": "zzzz-nobody"})["total"] == 0


def test_find_employee_reports_truncation(m, tool):
    for i in range(25):
        write_sql(m, "INSERT INTO employees VALUES (?,?,?,?)", (f"E3{i:03d}", f"Tester {i}", "Sandbox", "QA"))
    data = tool("find_employee", {"query": "sandbox"})
    assert len(data["employees"]) == m.MAX_RESULTS == 20
    assert data["total"] == 25 and data["truncated"] is True
    assert tool("find_employee", {"query": "tester 1"})["truncated"] is False  # Tester 1, 10..19: 11 matches


@pytest.mark.parametrize("bad", ["", " ", "a", "  a  ", "x" * 65])
def test_find_employee_rejects_bad_length(tool_error, bad):
    assert "validation error" in tool_error("find_employee", {"query": bad})


def test_find_employee_requires_query(tool_error):
    assert "validation error" in tool_error("find_employee", {})


def test_find_employee_wildcards_are_literal(tool):
    # '%' and '_' must not act as LIKE wildcards, otherwise "%%" would dump the whole directory
    assert tool("find_employee", {"query": "%%"})["total"] == 0
    assert tool("find_employee", {"query": "__"})["total"] == 0


def test_find_employee_sql_injection_is_inert(tool):
    assert tool("find_employee", {"query": "x' OR '1'='1"})["total"] == 0


# ----------------------------- leave balances -----------------------------


def test_leave_balance(tool):
    data = tool("leave_balance", {"employee_id": "E1002"})
    assert data == {"employee_id": "E1002", "name": "Brian Tran", "year": data["year"],
                    "entitled_days": 14, "used_days": 9, "remaining_days": 5}


def test_sick_leave_is_a_separate_tool(tool):
    """Gateway policy is per tool: annual leave must not carry sick leave data and vice versa."""
    annual = tool("leave_balance", {"employee_id": "E1002"})
    sick = tool("sick_leave_balance", {"employee_id": "E1002"})
    assert "sick" not in json.dumps(annual)
    assert (sick["entitled_days"], sick["used_days"], sick["remaining_days"]) == (30, 2, 28)


def test_leave_balance_fractional_days(tool):
    assert tool("leave_balance", {"employee_id": "E1001"})["remaining_days"] == 11.5


def test_leave_balance_uses_the_current_year(m, tool, tool_error, monkeypatch):
    """Regression: the primary key used to be employee_id alone, so a stale year was served as "current"."""
    year = m._current_year()
    assert tool("leave_balance", {"employee_id": "E1001"})["year"] == year
    write_sql(m, "INSERT INTO leave_balances VALUES ('E1001', ?, 18, 17, 30, 29)", (year - 1,))
    assert tool("leave_balance", {"employee_id": "E1001"})["used_days"] == 6.5  # not last year's 17

    monkeypatch.setattr(m, "_current_year", lambda: year + 1)
    assert f"in {year + 1}" in tool_error("leave_balance", {"employee_id": "E1001"})


def test_current_year_follows_vietnam_time(m, monkeypatch):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(1798738200, tz)  # 2026-12-31 17:30 UTC

    monkeypatch.setattr(m, "datetime", FakeDateTime)
    assert m._current_year() == 2027  # 17:30 UTC is already 00:30 on 1 January in Vietnam (UTC+7)


def test_leave_balances_are_keyed_by_employee_and_year(m):
    write_sql(m, "INSERT INTO leave_balances VALUES ('E1001', 1999, 1, 0, 1, 0)")  # other year: allowed
    with pytest.raises(sqlite3.IntegrityError):
        write_sql(m, "INSERT INTO leave_balances VALUES ('E1001', 1999, 1, 0, 1, 0)")


@pytest.mark.parametrize("bad", ["", "1001", "E100", "E10011", "e1001", " E1001", "E1001\n", "E١٠٠١",
                                 "E1001; DROP TABLE employees", "../etc/passwd"])
@pytest.mark.parametrize("name", ["leave_balance", "sick_leave_balance"])
def test_leave_balance_rejects_bad_id(tool_error, name, bad):
    assert "validation error" in tool_error(name, {"employee_id": bad})


@pytest.mark.parametrize("name", ["leave_balance", "sick_leave_balance"])
def test_leave_balance_unknown_employee(tool_error, name):
    assert "leave record for employee E9999" in tool_error(name, {"employee_id": "E9999"})


# ----------------------------- purchase orders -----------------------------


def test_list_purchase_orders_all_sorted_newest_first(tool):
    data = tool("list_purchase_orders")
    assert data["total"] == 6 and data["truncated"] is False and data["status_filter"] == "all"
    dates = [po["created_on"] for po in data["purchase_orders"]]
    assert dates == sorted(dates, reverse=True)


def test_list_purchase_orders_filter_and_total(tool):
    data = tool("list_purchase_orders", {"status": "pending_approval"})
    assert {po["po_id"] for po in data["purchase_orders"]} == {"PO-2026-0002", "PO-2026-0004"}
    po4 = next(po for po in data["purchase_orders"] if po["po_id"] == "PO-2026-0004")
    assert po4["total_amount"] == 2 * 410.00 + 2 * 389.90
    assert set(po4) == {"po_id", "vendor", "status", "currency", "created_on", "requester_id", "total_amount"}


def test_list_purchase_orders_reports_truncation(m, tool):
    for i in range(25):
        write_sql(m, "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?)",
                  (f"PO-2027-{i:04d}", "Bulk Vendor", "approved", "USD", "2027-01-01", "E1001"))
    data = tool("list_purchase_orders", {"status": "approved"})
    assert len(data["purchase_orders"]) == 20
    assert data["total"] == 27 and data["truncated"] is True  # 25 new + the 2 seeded approved orders
    assert data["purchase_orders"][0]["total_amount"] == 0  # an order without lines totals 0


@pytest.mark.parametrize("bad", ["open", "APPROVED", "approved'; --", "pending approval", ""])
def test_list_purchase_orders_rejects_bad_status(tool_error, bad):
    assert "validation error" in tool_error("list_purchase_orders", {"status": bad})


def test_get_purchase_order(tool):
    data = tool("get_purchase_order", {"po_id": "PO-2026-0001"})
    assert data["vendor"] == "Northwind Components"
    assert (data["requester_id"], data["requester_name"]) == ("E1004", "David Pham")
    assert [ln["sku"] for ln in data["lines"]] == ["SKU-CBL-001", "SKU-SWT-024"]
    assert data["lines"][1] == {"line_no": 2, "sku": "SKU-SWT-024", "description": "24-port managed switch",
                                "quantity": 4, "unit_price": 410.0, "line_total": 1640.0}
    assert data["total_amount"] == 200 * 3.20 + 4 * 410.00


def test_get_purchase_order_with_unknown_requester(m, tool):
    """Regression: an INNER JOIN hid orders whose requester is not in the directory, although the list showed them."""
    write_sql(m, "INSERT INTO purchase_orders VALUES ('PO-2026-0100','Orphan Ltd','approved','USD','2026-05-01','E7777')")
    listed = tool("list_purchase_orders")["purchase_orders"]
    assert "PO-2026-0100" in {po["po_id"] for po in listed}
    data = tool("get_purchase_order", {"po_id": "PO-2026-0100"})
    assert data["requester_id"] == "E7777" and data["requester_name"] is None and data["lines"] == []


def test_list_and_detail_totals_are_identical(m, tool):
    """Regression: the list summed unrounded line products while the detail summed rounded line totals."""
    write_sql(m, "INSERT INTO purchase_orders VALUES ('PO-2026-0101','Penny Co','approved','USD','2026-05-02','E1001')")
    for line_no, (qty, cents) in enumerate([(3, 335), (1, 1), (7, 2)], start=1):
        write_sql(m, "INSERT INTO po_lines VALUES ('PO-2026-0101', ?, 'SKU-X', 'x', ?, ?)", (line_no, qty, cents))
    listed = {po["po_id"]: po["total_amount"] for po in tool("list_purchase_orders")["purchase_orders"]}
    assert listed["PO-2026-0101"] == 10.2  # 10.05 + 0.01 + 0.14, exact
    for po_id, total in listed.items():
        detail = tool("get_purchase_order", {"po_id": po_id})
        assert detail["total_amount"] == total
        assert round(sum(ln["line_total"] for ln in detail["lines"]), 2) == total


@pytest.mark.parametrize("bad", ["", "PO-1", "po-26-1", "PO-2026-0001 OR 1=1", "2026-0001", "PO-2026-0001\n"])
def test_get_purchase_order_rejects_bad_id(tool_error, bad):
    assert "validation error" in tool_error("get_purchase_order", {"po_id": bad})


def test_get_purchase_order_not_found(tool_error):
    assert "purchase order PO-2026-9999 not found" in tool_error("get_purchase_order", {"po_id": "PO-2026-9999"})


# ----------------------------- inventory -----------------------------


def test_inventory_level_multi_warehouse(tool):
    data = tool("inventory_level", {"sku": "SKU-CBL-001"})
    assert data["total_available"] == (850 - 120) + 140
    by_wh = {w["warehouse"]: w for w in data["warehouses"]}
    assert by_wh["WH-HCM"]["needs_reorder"] is False
    assert by_wh["WH-HAN"]["needs_reorder"] is True  # 140 available <= reorder level 300


@pytest.mark.parametrize("bad", ["", "ab", "sku-ups-1k", "SKU_SWT_024", "SKU-SWT-024'; --", "X" * 30, "-SKU"])
def test_inventory_level_rejects_bad_sku(tool_error, bad):
    assert "validation error" in tool_error("inventory_level", {"sku": bad})


def test_inventory_level_unknown_sku(tool_error):
    assert "unknown SKU SKU-NOPE-1" in tool_error("inventory_level", {"sku": "SKU-NOPE-1"})
