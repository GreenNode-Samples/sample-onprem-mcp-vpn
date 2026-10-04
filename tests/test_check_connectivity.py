"""infra/onprem/check_connectivity.sh against a real server process (and a stub curl for the argv check)."""

import os
import re
import stat
import subprocess
from pathlib import Path

from conftest import API_KEY, free_port

SCRIPT = Path(__file__).resolve().parents[1] / "infra" / "onprem" / "check_connectivity.sh"


def run_script(*args: str, env: dict | None = None, key: str | None = API_KEY) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("MCP_", "INSECURE", "TIMEOUT", "EXPECTED"))}
    base["TIMEOUT"] = "3"
    if key:
        base["MCP_API_KEY"] = key
    return subprocess.run(["bash", str(SCRIPT), *args], env={**base, **(env or {})},
                          capture_output=True, text=True, timeout=60)


def host_port(server_url: str) -> list[str]:
    host, port = server_url.removeprefix("http://").split(":")
    return [host, port]


def test_passes_against_a_healthy_server(server_url):
    done = run_script(*host_port(server_url))
    assert done.returncode == 0, done.stdout
    assert "RESULT: PASS (3/3)" in done.stdout and "(6 tools)" in done.stdout


def test_default_expected_tool_count_matches_the_server(m):
    import asyncio

    default = re.search(r'EXPECTED_TOOLS="\$\{EXPECTED_TOOLS:-(\d+)\}"', SCRIPT.read_text()).group(1)
    assert int(default) == len(asyncio.run(m.mcp.list_tools()))


def test_wrong_tool_count_fails(server_url):
    done = run_script(*host_port(server_url), env={"EXPECTED_TOOLS": "5"})
    assert done.returncode == 1 and "6 tools, expected 5" in done.stdout


def test_wrong_key_fails_with_a_hint(server_url):
    done = run_script(*host_port(server_url), key="z" * 32)
    assert done.returncode == 1
    assert "FAIL  [3/3] POST /mcp -> 401" in done.stdout and "API key missing or wrong" in done.stdout


def test_missing_key_fails(server_url):
    done = run_script(*host_port(server_url), key=None)
    assert done.returncode == 1 and "set MCP_API_KEY" in done.stdout


def test_unreachable_port_fails_the_first_step():
    done = run_script("127.0.0.1", str(free_port()))
    assert done.returncode == 1
    assert "FAIL  [1/3]" in done.stdout and "[2/3] GET /health skipped" in done.stdout


def test_wrong_scheme_reports_the_curl_error(server_url):
    done = run_script(*host_port(server_url), "https")
    assert done.returncode == 1
    assert "[2/3] GET /health -> no HTTP response" in done.stdout and "-> curl:" in done.stdout


def test_help_shows_only_the_header_comment():
    done = run_script("--help", key=None)
    assert done.returncode == 2
    assert done.stdout.startswith("Check the path to the on-prem MCP server")
    assert "set -u" not in done.stdout and "EXPECTED_TOOLS" in done.stdout


def test_host_is_never_interpreted_by_a_shell(tmp_path):
    marker = tmp_path / "pwned"
    run_script(f"127.0.0.1;touch {marker}", "1")
    run_script(f"$(touch {marker})", "1")
    assert not marker.exists()


def test_api_key_is_not_on_the_curl_command_line(server_url, tmp_path):
    """A stub curl records its arguments and its stdin, then runs the real curl: the key may only be in stdin."""
    real = subprocess.run(["which", "curl"], capture_output=True, text=True, check=True).stdout.strip()
    stub = tmp_path / "bin" / "curl"
    stub.parent.mkdir()
    stub.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{tmp_path}/argv"\n'
                    f'cat > "{tmp_path}/stdin"\nexec "{real}" "$@" < "{tmp_path}/stdin"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    key = 'k"e\\y' + "q" * 30  # quote and backslash must be escaped in curl's config syntax
    done = run_script(*host_port(server_url), env={"PATH": f"{stub.parent}:{os.environ['PATH']}"}, key=key)
    assert "POST /mcp -> 401" in done.stdout  # not the server's key: the server rejects it
    argv = (tmp_path / "argv").read_text()
    assert "q" * 30 not in argv and "X-Api-Key" not in argv
    assert (tmp_path / "stdin").read_text() == 'header = "X-Api-Key: k\\"e\\\\y' + "q" * 30 + '"\n'
