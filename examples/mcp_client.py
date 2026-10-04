"""Minimal MCP client for this sample: list tools and optionally call one.

Works against the server directly (API key) or through an MCP Gateway (Bearer token).

    # Direct, for example on localhost or from a host inside the customer VPC:
    MCP_URL=http://127.0.0.1:8080/mcp MCP_API_KEY=<key> python examples/mcp_client.py
    MCP_URL=... MCP_API_KEY=<key> python examples/mcp_client.py --call find_employee --args '{"query":"nguyen"}'

    # Through the Private MCP Gateway (run from a network that can reach the gateway endpoint):
    MCP_URL=<gateway endpoint URL>/erp MCP_BEARER_TOKEN=<IAM token or JWT> python examples/mcp_client.py

Variables: MCP_URL (required), MCP_API_KEY (sent as X-Api-Key), MCP_BEARER_TOKEN (sent as
Authorization: Bearer), INSECURE_TLS=1 (skip certificate verification, lab use only).

The tool's result is printed as the server sent it: every content block, then the structured content.
Exit status: 0 on success, 1 when the call failed or the tool reported an error (`isError`), 2 on bad usage.
"""

import argparse
import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession, McpError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

HTTP_STATUS_HINTS = {
    401: "missing or invalid credentials",
    403: "denied: check the Policy Group",
    404: "wrong URL or connector path",
    503: "server has no usable MCP_API_KEYS",
}


def build_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if os.environ.get("MCP_API_KEY"):
        headers["X-Api-Key"] = os.environ["MCP_API_KEY"]
    if os.environ.get("MCP_BEARER_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['MCP_BEARER_TOKEN']}"
    return headers


def print_result(result: CallToolResult) -> None:
    """Print every content block, then the structured content (to stderr when the tool reported an error)."""
    out = sys.stderr if result.isError else sys.stdout
    for block in result.content:
        print(block.text if block.type == "text" else f"[{block.type} content block]", file=out)
    if result.structuredContent is not None:
        print("structuredContent:", json.dumps(result.structuredContent, indent=2, ensure_ascii=False), file=out)


async def run(url: str, call: str | None, arguments: dict) -> int:
    verify = os.environ.get("INSECURE_TLS") != "1"
    async with (
        httpx.AsyncClient(headers=build_headers(), timeout=20, verify=verify) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))
        if not call:
            return 0
        result = await session.call_tool(call, arguments)
        print_result(result)
        return 1 if result.isError else 0


def leaf_errors(exc: BaseException) -> list[BaseException]:
    """Flatten (nested) exception groups: the SDK runs its HTTP transport in task groups that wrap every failure."""
    members = getattr(exc, "exceptions", None)  # ExceptionGroup, or the `exceptiongroup` backport on Python 3.10
    if members is None:
        return [exc]
    return [leaf for member in members for leaf in leaf_errors(member)]


def describe(exc: BaseException) -> str:
    hint = ""
    if isinstance(exc, httpx.HTTPStatusError):
        hint = HTTP_STATUS_HINTS.get(exc.response.status_code, "")
    elif isinstance(exc, McpError) and str(exc) == "Session terminated":
        # The SDK reports an HTTP 404 on a request as this error instead of raising httpx.HTTPStatusError.
        hint = HTTP_STATUS_HINTS[404]
    return f"{type(exc).__name__}: {exc}" + (f" ({hint})" if hint else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--call", help="tool name to call (through a gateway use the exact name from tools/list)")
    parser.add_argument("--args", default="{}", help='tool arguments as a JSON object, for example \'{"query":"nguyen"}\'')
    ns = parser.parse_args()
    try:
        arguments = json.loads(ns.args)
    except ValueError:
        parser.error("--args is not valid JSON")
    if not isinstance(arguments, dict):
        parser.error("--args must be a JSON object, for example '{\"query\":\"nguyen\"}'")
    url = os.environ.get("MCP_URL")
    if not url:
        print("Set MCP_URL (see --help).", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(url, ns.call, arguments))
    except Exception as exc:  # show a readable message instead of a traceback
        for line in dict.fromkeys(describe(leaf) for leaf in leaf_errors(exc)):
            print(f"FAILED: {line}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
