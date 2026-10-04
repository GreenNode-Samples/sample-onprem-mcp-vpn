"""HTTP-level tests: fail-closed API-key middleware, request robustness and the audit log."""

import asyncio
import hashlib
import json
import logging
import re
import runpy
import sys
import types
from pathlib import Path

import pytest
from conftest import ACCEPT, API_KEY, AUTH, SRC

REPO = Path(__file__).resolve().parents[1]
LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
OTHER_KEY = "b" * 32


def call_body(name, arguments=None) -> dict:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}


@pytest.fixture()
def audit_records(caplog):
    caplog.set_level(logging.INFO, logger="onprem-mcp.audit")

    def records() -> list[str]:
        return [rec.getMessage() for rec in caplog.records if rec.name == "onprem-mcp.audit"]

    return records


# ----------------------------- fail-closed authentication -----------------------------


def test_no_key_configured_is_503(m, client, monkeypatch):
    monkeypatch.setattr(m, "API_KEYS", [])
    assert client.post("/mcp", json=LIST_BODY, headers=ACCEPT).status_code == 503


def test_allow_anonymous_opens_mcp_when_no_key_is_set(m, client, monkeypatch):
    monkeypatch.setattr(m, "API_KEYS", [])
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", True)
    r = client.post("/mcp", json=LIST_BODY, headers=ACCEPT)
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 6


def test_a_configured_key_is_mandatory_even_with_allow_anonymous(m, client, monkeypatch):
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", True)
    r = client.post("/mcp", json=LIST_BODY, headers=ACCEPT)
    assert r.status_code == 401 and "www-authenticate" in r.headers
    assert client.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "X-Api-Key": "wrong"}).status_code == 401
    assert client.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "Authorization": "Basic abc"}).status_code == 401


def test_both_header_styles_and_two_keys_for_rotation(m, client, monkeypatch):
    monkeypatch.setattr(m, "API_KEYS", [API_KEY, OTHER_KEY])
    for headers in ({"X-Api-Key": API_KEY}, {"X-Api-Key": OTHER_KEY}, {"Authorization": f"Bearer {OTHER_KEY}"}):
        assert client.post("/mcp", json=LIST_BODY, headers={**ACCEPT, **headers}).status_code == 200, headers


# ----------------------------- placeholder / weak keys -----------------------------

REJECTED_KEYS = [
    "<openssl rand -hex 32>",
    "change-me-run-openssl-rand-hex-32",
    "CHANGE-ME-RUN-OPENSSL-RAND-HEX-32",
    "a" * 31,
    "k3y",
    "x" * 40 + "<",
    ">" + "x" * 40,
]


@pytest.mark.parametrize("bad", REJECTED_KEYS)
def test_placeholder_or_weak_key_is_rejected(m, client, monkeypatch, bad):
    monkeypatch.setattr(m, "API_KEYS", [bad])
    assert client.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "X-Api-Key": bad}).status_code == 503
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", True)  # a bad key never opens the server
    assert client.post("/mcp", json=LIST_BODY, headers=ACCEPT).status_code == 503


def test_one_bad_entry_locks_the_server_even_for_the_good_key(m, client, monkeypatch):
    monkeypatch.setattr(m, "API_KEYS", [API_KEY, "change-me-run-openssl-rand-hex-32"])
    assert client.post("/mcp", json=LIST_BODY, headers=AUTH).status_code == 503


@pytest.mark.parametrize("bad", REJECTED_KEYS)
def test_key_problem_report_never_contains_the_key(m, bad):
    problems = m._key_problems([bad])
    assert len(problems) == 1 and bad not in problems[0] and "entry #1" in problems[0]


def test_good_keys_have_no_problems(m):
    assert m._key_problems([API_KEY, OTHER_KEY, "0123456789abcdef" * 4]) == []


def test_load_api_keys_env(m, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", " k1 , ,k2 ")
    assert m._load_api_keys() == ["k1", "k2"]
    monkeypatch.delenv("MCP_API_KEYS")
    assert m._load_api_keys() == []


@pytest.mark.parametrize("env_file", [".env.example", "infra/onprem/.env.example"])
def test_example_env_files_ship_a_key_the_server_rejects(m, env_file):
    line = next(ln for ln in (REPO / env_file).read_text().splitlines() if ln.startswith("MCP_API_KEYS="))
    example_key = line.split("=", 1)[1].strip()
    assert example_key and m._key_problems([example_key])


def test_gateway_host_header_is_accepted(client):
    """The SDK's DNS-rebinding protection (localhost Host headers only) must stay off: the gateway and Caddy
    reach this server by an internal IP address or name."""
    r = client.post("/mcp", json=LIST_BODY, headers={**AUTH, "Host": "192.168.10.20:8443"})
    assert r.status_code == 200


# ----------------------------- /health -----------------------------


def test_health_is_open_and_reveals_nothing(m, client, monkeypatch):
    assert client.get("/health").json() == {"status": "ok"}
    monkeypatch.setattr(m, "API_KEYS", [])  # locked server: still just "ok"
    assert client.get("/health").json() == {"status": "ok"}
    monkeypatch.setattr(m, "API_KEYS", ["<placeholder>"])
    assert client.get("/health").json() == {"status": "ok"}


# ----------------------------- malformed request bodies -----------------------------

MALFORMED_BODIES = {
    "params is a list": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}',
    "params is a string": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":"x"}',
    "name is a number": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":5}}',
    "name is missing": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}',
    "list of numbers": b"[1,2,3]",
    "scalar": b"5",
    "null": b"null",
    "empty": b"",
    "not json": b"not json at all",
    "batch with junk": b'[{"method":"tools/call","params":null},"x",7,{"method":"tools/call"}]',
    "deeply nested array": b"[" * 200_000,
    "deeply nested object": b'{"a":' * 5_000 + b"1" + b"}" * 5_000,
    "invalid utf-8": b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"\xff\xfe"}}',
    "lone surrogate escape": b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"\\ud800"}}',
}


@pytest.mark.parametrize("body", MALFORMED_BODIES.values(), ids=MALFORMED_BODIES.keys())
def test_malformed_bodies_never_cause_a_server_error(client, body, audit_records):
    r = client.post("/mcp", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code < 500, r.text
    assert all(line.startswith("audit tool=") for line in audit_records())


@pytest.mark.parametrize("body", [b"[" * 200_000, b'{"a":"\xff"}', b'{"a":"\\ud800"}', b""],
                         ids=["deep", "utf-8", "surrogate", "empty"])
def test_unparseable_bodies_get_a_jsonrpc_parse_error(client, body):
    r = client.post("/mcp", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json() == {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error: body is not valid JSON"}}


def test_oversized_body_is_rejected(m, client):
    r = client.post("/mcp", content=b"x" * (m.MAX_BODY_BYTES + 1), headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_parse_json_normalises_errors(m):
    for body in (b"[" * 200_000, b"\xff", b"{", b'"\\ud800"'):
        with pytest.raises(ValueError, match=r"."):
            m._parse_json(body)
    assert m._parse_json(b'{"a": [1]}') == {"a": [1]}


# ----------------------------- audit log -----------------------------


def test_audit_entry_has_tool_caller_key_fingerprint_status_and_latency(client, audit_records, caplog):
    r = client.post("/mcp", json=call_body("leave_balance", {"employee_id": "E1002"}), headers=AUTH)
    assert r.status_code == 200 and r.json()["result"]["structuredContent"]["employee_id"] == "E1002"
    (entry,) = audit_records()
    fingerprint = hashlib.sha256(API_KEY.encode()).hexdigest()[:8]
    assert re.fullmatch(rf"audit tool=leave_balance caller=testclient key={fingerprint} status=200 ms=\d+", entry)
    assert API_KEY not in caplog.text and "E1002" not in entry


def test_audit_fingerprint_tells_rotated_keys_apart(m, client, audit_records, monkeypatch):
    monkeypatch.setattr(m, "API_KEYS", [API_KEY, OTHER_KEY])
    for key in (API_KEY, OTHER_KEY):
        client.post("/mcp", json=call_body("inventory_level", {"sku": "SKU-UPS-1K"}),
                    headers={**ACCEPT, "X-Api-Key": key})
    fingerprints = {entry.split("key=")[1].split()[0] for entry in audit_records()}
    assert fingerprints == {hashlib.sha256(k.encode()).hexdigest()[:8] for k in (API_KEY, OTHER_KEY)}


def test_audit_entry_for_anonymous_access_has_no_key(m, client, audit_records, monkeypatch):
    monkeypatch.setattr(m, "API_KEYS", [])
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", True)
    client.post("/mcp", json=call_body("inventory_level", {"sku": "SKU-UPS-1K"}), headers=ACCEPT)
    assert " key=- " in audit_records()[0]


def test_unauthenticated_calls_leave_no_audit_entry(client, audit_records):
    assert client.post("/mcp", json=call_body("find_employee", {"query": "alice"}), headers=ACCEPT).status_code == 401
    assert audit_records() == []


def test_tools_list_is_not_audited(client, audit_records):
    client.post("/mcp", json=LIST_BODY, headers=AUTH)
    assert audit_records() == []


def test_audit_reports_the_http_status_of_the_response(client, audit_records):
    client.post("/mcp", content=json.dumps(call_body("find_employee")).encode() + b"\xff",
                headers={**AUTH, "Content-Type": "application/json"})
    # Not valid JSON: answered with a parse error and no tool call to audit
    assert audit_records() == []
    client.post("/mcp", json=call_body("find_employee"), headers={**AUTH, "Content-Type": "text/plain"})
    assert " status=400 " in audit_records()[0]  # the SDK rejects the content type


@pytest.mark.parametrize("name", [
    "x\n2026-10-04 INFO onprem-mcp.audit: audit tool=leave_balance caller=10.0.0.1",
    "find_employee\n",
    "find_employee caller=10.0.0.1",
    "a" * 65,
    "",
    "<script>",
    "удар",
])
def test_audit_log_injection_is_blocked(client, audit_records, caplog, name):
    client.post("/mcp", json=call_body(name), headers=AUTH)
    (entry,) = audit_records()
    assert entry.startswith("audit tool=<invalid> caller=testclient key=")
    assert not any("\n" in rec.getMessage() for rec in caplog.records if rec.name == "onprem-mcp.audit")


@pytest.mark.parametrize("params", [[1], "x", None, 5, {}, {"name": 5}, {"name": None}, {"name": ["find_employee"]}])
def test_audit_reports_odd_params_as_invalid_tool(client, audit_records, params):
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, headers=AUTH)
    assert audit_records()[0].startswith("audit tool=<invalid> ")


def test_audit_logs_every_tool_call_of_a_batch(client, audit_records):
    batch = [call_body("find_employee", {"query": "alice"}), LIST_BODY, "junk", call_body("inventory_level"), 7]
    client.post("/mcp", json=batch, headers=AUTH)
    assert [e.split()[1] for e in audit_records()] == ["tool=find_employee", "tool=inventory_level"]


def test_log_lines_cannot_be_forged_through_request_data(m, client, caplog):
    """The MCP SDK itself logs the requested name of an unknown tool; the formatter keeps that on one line."""
    forged = "x\n2026-10-04 INFO onprem-mcp.audit: audit tool=leave_balance caller=10.0.0.1"
    client.post("/mcp", json=call_body(forged), headers=AUTH)
    formatter = m._SingleLineFormatter("%(name)s: %(message)s")
    lines = [formatter.format(rec) for rec in caplog.records]
    assert any("not listed" in line for line in lines)
    assert all("\n" not in line for line in lines)


def test_audit_middleware_forwards_the_status_and_duration(m, caplog):
    caplog.set_level(logging.INFO, logger="onprem-mcp.audit")

    async def teapot(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 418, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = asyncio.run(post_asgi(m.AuditMiddleware(teapot), [chunk(json.dumps(call_body("find_employee")).encode())]))
    assert sent[0]["status"] == 418
    assert re.search(r"audit tool=find_employee caller=10\.0\.0\.1 key=- status=418 ms=\d+", caplog.text)


def test_audit_middleware_logs_500_and_reraises_when_the_app_crashes(m, caplog):
    caplog.set_level(logging.INFO, logger="onprem-mcp.audit")

    async def crash(scope, receive, send):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(post_asgi(m.AuditMiddleware(crash), [chunk(json.dumps(call_body("find_employee")).encode())]))
    assert " status=500 " in caplog.text


# ----------------------------- client address / X-Forwarded-For -----------------------------


def scope_for(peer: str, forwarded: str | None = None) -> tuple[dict, dict[str, str]]:
    headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
    return {"client": (peer, 40000)}, headers


@pytest.mark.parametrize(("peer", "forwarded", "expected"), [
    ("127.0.0.1", "198.51.100.1, 192.168.10.5", "192.168.10.5"),  # last entry = what the proxy itself saw
    ("::1", "192.168.10.5", "192.168.10.5"),
    ("192.168.10.7", "203.0.113.9", "192.168.10.7"),  # direct connection (not loopback): header is forged
    ("172.18.0.2", "203.0.113.9", "172.18.0.2"),
    ("testclient", "203.0.113.9", "testclient"),  # not even an IP address
    ("127.0.0.1", "not-an-ip\nforged", "127.0.0.1"),  # junk from the proxy is never logged
    ("127.0.0.1", "", "127.0.0.1"),
    ("127.0.0.1", None, "127.0.0.1"),
])
def test_forwarded_for_is_trusted_only_from_loopback_by_default(m, monkeypatch, peer, forwarded, expected):
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", True)
    monkeypatch.setattr(m, "TRUSTED_PROXIES", m._parse_networks("127.0.0.1,::1"))
    assert m._caller_ip(*scope_for(peer, forwarded)) == expected


def test_forwarded_for_is_trusted_from_configured_proxies(m, monkeypatch):
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", True)
    monkeypatch.setattr(m, "TRUSTED_PROXIES", m._parse_networks("172.29.200.2, 10.1.0.0/16"))
    assert m._caller_ip(*scope_for("172.29.200.2", "192.168.10.5")) == "192.168.10.5"
    assert m._caller_ip(*scope_for("10.1.7.7", "192.168.10.5")) == "192.168.10.5"
    assert m._caller_ip(*scope_for("172.29.200.3", "192.168.10.5")) == "172.29.200.3"
    assert m._caller_ip(*scope_for("127.0.0.1", "192.168.10.5")) == "127.0.0.1"  # no longer implied


def test_trusted_proxies_syntax(m):
    assert [str(n) for n in m._parse_networks(" 127.0.0.1 ,::1,172.29.200.0/24,,")] == [
        "127.0.0.1/32", "::1/128", "172.29.200.0/24"]
    assert m._parse_networks("") == []
    with pytest.raises(ValueError, match="not-an-ip"):
        m._parse_networks("127.0.0.1,not-an-ip")


def test_forwarded_for_is_ignored_unless_enabled(m, monkeypatch):
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", False)
    monkeypatch.setattr(m, "TRUSTED_PROXIES", m._parse_networks("127.0.0.1"))
    assert m._caller_ip(*scope_for("127.0.0.1", "192.168.10.5")) == "127.0.0.1"


def test_forwarded_for_header_from_a_non_loopback_client_is_not_logged(m, client, audit_records, monkeypatch):
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", True)
    client.post("/mcp", json=call_body("inventory_level", {"sku": "SKU-UPS-1K"}),
                headers={**AUTH, "X-Forwarded-For": "203.0.113.9"})  # the test client connects as "testclient"
    assert "203.0.113.9" not in audit_records()[0] and "caller=testclient" in audit_records()[0]


def test_caller_without_peer_information(m):
    assert m._caller_ip({}, {}) == "unknown"


# ----------------------------- ASGI details -----------------------------


def chunk(body: bytes, more: bool = False) -> dict:
    return {"type": "http.request", "body": body, "more_body": more}


async def post_asgi(app, messages: list[dict], path: str = "/mcp") -> list[dict]:
    """Drive an ASGI app directly with a scripted sequence of receive() messages; returns what it sent."""
    sent: list[dict] = []
    incoming = iter(messages)

    async def receive():
        return next(incoming, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "client": ("10.0.0.1", 1234)}
    await app(scope, receive, send)
    return sent


def test_partial_body_on_disconnect_is_not_forwarded(m):
    forwarded = []

    async def downstream(scope, receive, send):
        forwarded.append(await receive())

    asyncio.run(post_asgi(m.AuditMiddleware(downstream), [chunk(b'{"jsonrpc":"2.0"', more=True),
                                                           {"type": "http.disconnect"}]))
    assert forwarded == []


def test_body_split_over_several_chunks_is_forwarded_whole(m):
    forwarded = []

    async def downstream(scope, receive, send):
        forwarded.append(await receive())

    body = json.dumps(call_body("find_employee")).encode()
    asyncio.run(post_asgi(m.AuditMiddleware(downstream), [chunk(body[:10], more=True), chunk(body[10:])]))
    assert forwarded == [{"type": "http.request", "body": body, "more_body": False}]


# ----------------------------- process entry point -----------------------------


def test_entry_point_starts_uvicorn_without_proxy_header_handling(tmp_path, monkeypatch):
    """`python main.py`: initialises the database and runs uvicorn with proxy_headers=False."""
    calls = []
    fake_uvicorn = types.SimpleNamespace(run=lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "data" / "erp.db"))
    monkeypatch.setenv("HOST", "192.168.10.20")
    monkeypatch.setenv("PORT", "9090")
    runpy.run_path(str(SRC / "main.py"), run_name="__main__")
    assert calls == [{"host": "192.168.10.20", "port": 9090, "proxy_headers": False}]
    assert (tmp_path / "data" / "erp.db").exists()
