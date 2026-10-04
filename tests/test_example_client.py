"""End-to-end test of `python main.py` and examples/mcp_client.py: a real server process on a local port."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import API_KEY, free_port

CLIENT = Path(__file__).resolve().parents[1] / "examples" / "mcp_client.py"


def run_client(url: str, *args: str, key: str | None = API_KEY) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env["MCP_URL"] = url
    if key:
        env["MCP_API_KEY"] = key
    return subprocess.run([sys.executable, str(CLIENT), *args], env=env, capture_output=True, text=True, timeout=60)


def test_lists_tools(server_url):
    done = run_client(f"{server_url}/mcp")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("tools: find_employee, leave_balance, sick_leave_balance")


def test_prints_content_and_structured_content(server_url):
    done = run_client(f"{server_url}/mcp", "--call", "leave_balance", "--args", '{"employee_id": "E1002"}')
    assert done.returncode == 0, done.stderr
    assert "structuredContent:" in done.stdout
    structured = json.loads(done.stdout.split("structuredContent:", 1)[1])
    assert structured["employee_id"] == "E1002" and structured["remaining_days"] == 5


def test_tool_error_exits_1_and_reports_on_stderr(server_url):
    done = run_client(f"{server_url}/mcp", "--call", "leave_balance", "--args", '{"employee_id": "E9999"}')
    assert done.returncode == 1
    assert "no annual leave record for employee E9999" in done.stderr


def test_wrong_key_shows_a_hint(server_url):
    done = run_client(f"{server_url}/mcp", key="z" * 32)
    assert done.returncode == 1
    assert "HTTPStatusError" in done.stderr and "missing or invalid credentials" in done.stderr


def test_wrong_path_shows_a_404_hint(server_url):
    """The SDK turns an HTTP 404 into McpError("Session terminated"); the client explains it."""
    done = run_client(f"{server_url}/wrong-path")
    assert done.returncode == 1
    assert "Session terminated" in done.stderr and "wrong URL or connector path" in done.stderr
    assert "Traceback" not in done.stderr


def test_connection_refused_is_reported_without_a_traceback():
    done = run_client(f"http://127.0.0.1:{free_port()}/mcp")
    assert done.returncode == 1
    assert done.stderr.startswith("FAILED: ConnectError") and "Traceback" not in done.stderr


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", '"text"', "5"])
def test_args_must_be_a_json_object(bad):
    done = run_client("http://127.0.0.1:1/mcp", "--call", "find_employee", "--args", bad)
    assert done.returncode == 2
    assert "--args" in done.stderr


def test_missing_url_is_a_usage_error():
    env = {k: v for k, v in os.environ.items() if k != "MCP_URL"}
    done = subprocess.run([sys.executable, str(CLIENT)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "Set MCP_URL" in done.stderr
