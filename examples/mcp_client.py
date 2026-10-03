"""Minimal MCP client for this sample: list tools and optionally call one.

Works against the server directly (API key) or through an MCP Gateway (Bearer token).

    # Direct, for example on localhost or from a host inside the customer VPC:
    MCP_URL=http://127.0.0.1:8080/mcp MCP_API_KEY=<key> python examples/mcp_client.py
    MCP_URL=... MCP_API_KEY=<key> python examples/mcp_client.py --call find_employee --args '{"query":"nguyen"}'

    # Through the Private MCP Gateway (run from a network that can reach the gateway endpoint):
    MCP_URL=<gateway endpoint URL>/erp MCP_BEARER_TOKEN=<IAM token or JWT> python examples/mcp_client.py

Variables: MCP_URL (required), MCP_API_KEY (sent as X-Api-Key), MCP_BEARER_TOKEN (sent as
Authorization: Bearer), INSECURE_TLS=1 (skip certificate verification, lab use only).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def build_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if os.environ.get("MCP_API_KEY"):
        headers["X-Api-Key"] = os.environ["MCP_API_KEY"]
    if os.environ.get("MCP_BEARER_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['MCP_BEARER_TOKEN']}"
    return headers


def insecure_client_factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, verify=False, follow_redirects=True)


async def run(url: str, call: str | None, arguments: dict) -> int:
    kwargs = {"headers": build_headers(), "timeout": 20}
    if os.environ.get("INSECURE_TLS") == "1":
        kwargs["httpx_client_factory"] = insecure_client_factory
    async with streamablehttp_client(url, **kwargs) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            if call:
                result = await session.call_tool(call, arguments)
                text = result.content[0].text if result.content else ""
                try:
                    print(json.dumps(json.loads(text), indent=2))
                except ValueError:
                    print(text)
                return 1 if result.isError else 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--call", help="tool name to call (through a gateway use the exact name from tools/list)")
    parser.add_argument("--args", default="{}", help='tool arguments as JSON, for example \'{"query":"nguyen"}\'')
    ns = parser.parse_args()
    url = os.environ.get("MCP_URL")
    if not url:
        print("Set MCP_URL (see --help).", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(url, ns.call, json.loads(ns.args)))
    except Exception as exc:  # noqa: BLE001 - surface a readable message instead of a traceback
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:  # the SDK wraps errors in task groups
            exc = exc.exceptions[0]
        hint = ""
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            hint = {401: " (missing or invalid credentials)", 403: " (denied: check the Policy Group)",
                    404: " (wrong URL or connector path)", 503: " (server has no MCP_API_KEYS)"}.get(code, "")
        print(f"FAILED: {type(exc).__name__}: {exc}{hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
