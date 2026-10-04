"""Pytest fixtures: import the MCP server module from src/onprem_mcp (no install needed).

All HTTP tests share ONE TestClient: the MCP session manager runs once per process and cannot be restarted,
so the ASGI lifespan is entered a single time for the whole session. Module settings (API keys, flags) are
patched per test with monkeypatch and are read by the server on every request.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    """Base URL of a real `python main.py` process (own database, one valid API key) for end-to-end tests."""
    port = free_port()
    own = ("MCP_", "ALLOW_ANONYMOUS", "TRUST", "DB_PATH", "HOST", "PORT")
    env = {k: v for k, v in os.environ.items() if not k.startswith(own)}
    env.update(HOST="127.0.0.1", PORT=str(port), MCP_API_KEYS=API_KEY,
               DB_PATH=str(tmp_path_factory.mktemp("db") / "erp.db"))
    proc = subprocess.Popen([sys.executable, str(SRC / "main.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as response:
                    assert json.load(response) == {"status": "ok", "tools": 6}
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the server process did not start")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)
