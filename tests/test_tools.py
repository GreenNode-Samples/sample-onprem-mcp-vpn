"""Tool tests: hermetic, run against a temporary SQLite database seeded with the demo data."""

import json
import sqlite3

import pytest


def parse(raw: str) -> dict:
    return json.loads(raw)


# ----------------------------- database -----------------------------


def test_seed_is_idempotent(m, temp_db):
    m.init_db(str(temp_db))  # second call must not duplicate rows
    con = sqlite3.connect(temp_db)
    assert con.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 8
    assert con.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 6
    con.close()


def test_tool_connection_is_read_only(m):
    with m._connect_ro() as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM employees")
    assert parse(m.find_employee("alice"))["count"] == 1


# ----------------------------- find_employee -----------------------------


def test_find_employee_by_name(m):
    data = parse(m.find_employee("nguyen"))
    assert data["count"] == 1
    assert data["employees"][0]["employee_id"] == "E1001"
    assert data["employees"][0]["department"] == "Finance"


def test_find_employee_by_department_case_insensitive(m):
    data = parse(m.find_employee("PROCUREMENT"))
    assert {e["employee_id"] for e in data["employees"]} == {"E1003", "E1004"}


def test_find_employee_no_match(m):
    assert parse(m.find_employee("zzzz-nobody"))["count"] == 0


@pytest.mark.parametrize("bad", ["", " ", "a", "x" * 65])
def test_find_employee_rejects_bad_length(m, bad):
    assert "error" in parse(m.find_employee(bad))


def test_find_employee_wildcards_are_literal(m):
    # '%' and '_' must not act as LIKE wildcards, otherwise "%%" would dump the whole directory
    assert parse(m.find_employee("%%"))["count"] == 0
    assert parse(m.find_employee("__"))["count"] == 0


def test_find_employee_sql_injection_is_inert(m):
    data = parse(m.find_employee("x' OR '1'='1"))
    assert data["count"] == 0


# ----------------------------- leave_balance -----------------------------


def test_leave_balance(m):
    data = parse(m.leave_balance("E1002"))
    assert data["name"] == "Brian Tran"
    assert data["annual"] == {"entitled_days": 14, "used_days": 9, "remaining_days": 5}
    assert data["sick"]["remaining_days"] == 28


def test_leave_balance_normalizes_case_and_whitespace(m):
    assert parse(m.leave_balance(" e1005 "))["employee_id"] == "E1005"


def test_leave_balance_fractional_days(m):
    assert parse(m.leave_balance("E1001"))["annual"]["remaining_days"] == 11.5


@pytest.mark.parametrize("bad", ["", "1001", "E100", "E10011", "E1001; DROP TABLE employees", "../etc/passwd"])
def test_leave_balance_rejects_bad_id(m, bad):
    assert "error" in parse(m.leave_balance(bad))


def test_leave_balance_unknown_employee(m):
    assert "no leave record" in parse(m.leave_balance("E9999"))["error"]


# ----------------------------- purchase orders -----------------------------


def test_list_purchase_orders_all_sorted_newest_first(m):
    data = parse(m.list_purchase_orders())
    assert data["count"] == 6
    dates = [po["created_on"] for po in data["purchase_orders"]]
    assert dates == sorted(dates, reverse=True)


def test_list_purchase_orders_filter_and_total(m):
    data = parse(m.list_purchase_orders("pending_approval"))
    assert {po["po_id"] for po in data["purchase_orders"]} == {"PO-2026-0002", "PO-2026-0004"}
    po4 = next(po for po in data["purchase_orders"] if po["po_id"] == "PO-2026-0004")
    assert po4["total_amount"] == 2 * 410.00 + 2 * 389.90


def test_list_purchase_orders_status_is_case_insensitive(m):
    assert parse(m.list_purchase_orders("APPROVED"))["count"] == 2


@pytest.mark.parametrize("bad", ["open", "approved'; --", "pending approval"])
def test_list_purchase_orders_rejects_bad_status(m, bad):
    assert "error" in parse(m.list_purchase_orders(bad))


def test_get_purchase_order(m):
    data = parse(m.get_purchase_order("PO-2026-0001"))
    assert data["vendor"] == "Northwind Components"
    assert data["requester"] == "David Pham"
    assert [ln["sku"] for ln in data["lines"]] == ["SKU-CBL-001", "SKU-SWT-024"]
    assert data["total_amount"] == 200 * 3.20 + 4 * 410.00


@pytest.mark.parametrize("bad", ["", "PO-1", "po-26-1", "PO-2026-0001 OR 1=1", "2026-0001"])
def test_get_purchase_order_rejects_bad_id(m, bad):
    assert "error" in parse(m.get_purchase_order(bad))


def test_get_purchase_order_not_found(m):
    assert "not found" in parse(m.get_purchase_order("PO-2026-9999"))["error"]


# ----------------------------- inventory -----------------------------


def test_inventory_level_multi_warehouse(m):
    data = parse(m.inventory_level("SKU-CBL-001"))
    assert data["total_available"] == (850 - 120) + 140
    by_wh = {w["warehouse"]: w for w in data["warehouses"]}
    assert by_wh["WH-HCM"]["needs_reorder"] is False
    assert by_wh["WH-HAN"]["needs_reorder"] is True  # 140 available <= reorder level 300


def test_inventory_level_lowercase_input(m):
    assert parse(m.inventory_level("sku-ups-1k"))["sku"] == "SKU-UPS-1K"


@pytest.mark.parametrize("bad", ["", "ab", "SKU_SWT_024", "SKU-SWT-024'; --", "X" * 30])
def test_inventory_level_rejects_bad_sku(m, bad):
    assert "error" in parse(m.inventory_level(bad))


def test_inventory_level_unknown_sku(m):
    assert "unknown SKU" in parse(m.inventory_level("SKU-NOPE-1"))["error"]


def test_tool_registry_matches_documented_names(m):
    import asyncio

    tools = asyncio.run(m.mcp.list_tools())
    assert sorted(t.name for t in tools) == sorted(m.TOOL_NAMES)
