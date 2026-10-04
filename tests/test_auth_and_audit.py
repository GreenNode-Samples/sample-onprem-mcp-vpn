"""HTTP-level tests: fail-closed API key middleware and audit logging.

The MCP session manager cannot be restarted inside one process, so everything that needs the ASGI
lifespan lives in ONE test that uses the shared `client` fixture (one TestClient for the whole session).
"""

import json
import logging

ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def call_body(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_auth_fail_closed_and_audit(m, client, monkeypatch, caplog):
    key_a, key_b = "a" * 32, "b" * 32

    c = client  # one lifespan for the whole scenario
    # 1. No key configured -> 503, never open
    monkeypatch.setattr(m, "API_KEYS", [])
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", False)
    assert c.post("/mcp", json=LIST_BODY, headers=ACCEPT).status_code == 503
    assert c.get("/health").json()["mcp_auth"].startswith("locked")

    # 2. ALLOW_ANONYMOUS (local development) -> open
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", True)
    r = c.post("/mcp", json=LIST_BODY, headers=ACCEPT)
    assert r.status_code == 200
    assert len(r.json()["result"]["tools"]) == len(m.TOOL_NAMES)

    # 3. With keys configured the key is mandatory, even if ALLOW_ANONYMOUS is true
    monkeypatch.setattr(m, "API_KEYS", [key_a, key_b])
    r = c.post("/mcp", json=LIST_BODY, headers=ACCEPT)
    assert r.status_code == 401 and "www-authenticate" in r.headers
    assert c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "X-Api-Key": "wrong"}).status_code == 401
    assert c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "Authorization": "Basic abc"}).status_code == 401

    # 4. Both header styles are accepted and two keys work at once (rotation)
    for hdr in ({"X-Api-Key": key_a}, {"X-Api-Key": key_b}, {"Authorization": f"Bearer {key_b}"}):
        assert c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, **hdr}).status_code == 200, hdr

    # 5. /health stays open and reports the auth mode
    assert c.get("/health").status_code == 200
    assert c.get("/health").json()["mcp_auth"] == "api-key (2 key)"

    # 6. A real tool call through HTTP returns data, and the audit log has tool + caller, no secrets
    caplog.set_level(logging.INFO, logger="onprem-mcp.audit")
    r = c.post("/mcp", json=call_body("leave_balance", {"employee_id": "E1002"}),
               headers={**ACCEPT, "X-Api-Key": key_a})
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["employee_id"] == "E1002"

    audit = [rec.getMessage() for rec in caplog.records if rec.name == "onprem-mcp.audit"]
    assert len(audit) == 1
    assert "tool=leave_balance" in audit[0] and "caller=" in audit[0]
    assert key_a not in caplog.text and "E1002" not in audit[0]

    # 7. Unauthenticated tool calls are rejected before the audit layer and leave no audit entry
    caplog.clear()
    r = c.post("/mcp", json=call_body("find_employee", {"query": "alice"}), headers=ACCEPT)
    assert r.status_code == 401
    assert not [rec for rec in caplog.records if rec.name == "onprem-mcp.audit"]

    # 8. tools/list is not a tool call and is not audited
    caplog.clear()
    c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "X-Api-Key": key_a})
    assert not [rec for rec in caplog.records if rec.name == "onprem-mcp.audit"]

    # 9. X-Forwarded-For is ignored unless TRUST_FORWARDED_FOR is set
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", False)
    caplog.clear()
    c.post("/mcp", json=call_body("inventory_level", {"sku": "SKU-UPS-1K"}),
           headers={**ACCEPT, "X-Api-Key": key_a, "X-Forwarded-For": "203.0.113.9"})
    assert "203.0.113.9" not in caplog.text
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", True)
    caplog.clear()
    c.post("/mcp", json=call_body("inventory_level", {"sku": "SKU-UPS-1K"}),
           headers={**ACCEPT, "X-Api-Key": key_a, "X-Forwarded-For": "198.51.100.1, 192.168.10.5"})
    assert "caller=192.168.10.5" in caplog.text

    # 10. Oversized bodies are rejected
    r = c.post("/mcp", content=b"x" * (m.MAX_BODY_BYTES + 1),
               headers={**ACCEPT, "X-Api-Key": key_a, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_load_api_keys_env(m, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", " k1 , ,k2 ")
    assert m._load_api_keys() == ["k1", "k2"]
    monkeypatch.delenv("MCP_API_KEYS")
    assert m._load_api_keys() == []


def test_tool_calls_parser(m):
    batch = json.dumps([call_body("a", {}), LIST_BODY, call_body("b", {"x": 1})]).encode()
    assert m._tool_calls(batch) == ["a", "b"]
    assert m._tool_calls(b"not json") == []
    assert m._tool_calls(json.dumps({"method": "tools/call", "params": {"name": 5}}).encode()) == []
