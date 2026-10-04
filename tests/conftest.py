"""Pytest fixtures: import the MCP server module from src/onprem_mcp (no install needed).

All HTTP tests share ONE TestClient: the MCP session manager runs once per process and cannot be restarted,
so the ASGI lifespan is entered a single time for the whole session. Module settings (API keys, flags) are
patched per test with monkeypatch and are read by the server on every request.
"""

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

SRC = Path(__file__).resolve().parents[1] / "src" / "onprem_mcp"
sys.path.insert(0, str(SRC))

import main as server  # noqa: E402

API_KEY = "a" * 32
ACCEPT = {"Accept": "application/json, text/event-stream"}
AUTH = {**ACCEPT, "X-Api-Key": API_KEY}


@pytest.fixture()
def m():
    return server


@pytest.fixture(scope="session")
def client():
    with TestClient(server.app) as c:
        yield c


@pytest.fixture(autouse=True)
def temp_db(m, tmp_path, monkeypatch):
    """Every test works on its own throw-away SQLite file seeded with the demo data."""
    db = tmp_path / "erp.db"
    monkeypatch.setattr(m, "DB_PATH", str(db))
    m.init_db(str(db))
    return db


@pytest.fixture(autouse=True)
def server_settings(m, monkeypatch):
    """Default HTTP settings: one valid API key, no anonymous access, X-Forwarded-For not trusted."""
    monkeypatch.setattr(m, "API_KEYS", [API_KEY])
    monkeypatch.setattr(m, "ALLOW_ANONYMOUS", False)
    monkeypatch.setattr(m, "TRUST_FORWARDED_FOR", False)


@pytest.fixture()
def call_tool(client):
    """Call an MCP tool over HTTP and return the JSON-RPC `result` (CallToolResult as a dict)."""

    def call(name: str, arguments: dict | None = None) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}
        response = client.post("/mcp", json=body, headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    return call


@pytest.fixture()
def tool(call_tool):
    """Call a tool that must succeed and return its structured output."""

    def call(name: str, arguments: dict | None = None) -> dict:
        result = call_tool(name, arguments)
        assert result["isError"] is False, result
        return result["structuredContent"]

    return call


@pytest.fixture()
def tool_error(call_tool):
    """Call a tool that must fail and return the error message."""

    def call(name: str, arguments: dict | None = None) -> str:
        result = call_tool(name, arguments)
        assert result["isError"] is True, result
        assert "structuredContent" not in result or result["structuredContent"] is None
        return result["content"][0]["text"]

    return call
