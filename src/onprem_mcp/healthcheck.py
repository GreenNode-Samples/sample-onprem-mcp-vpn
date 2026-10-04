"""Container health probe: GET /health on the address the server is bound to; exit status 0 = healthy.

Reads the same HOST and PORT variables as main.py. A server bound to all interfaces answers on loopback too;
one bound to a single address (HOST=192.168.10.20, or 127.0.0.1 behind Caddy) only answers on that address.
Used by the Dockerfile HEALTHCHECK.
"""

import os
import sys
import urllib.request


def probe_url(env=os.environ) -> str:
    """Probe 127.0.0.1 when the server listens on all interfaces, otherwise the configured HOST."""
    host = env.get("HOST", "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"  # IPv6 literal
    return f"http://{host}:{env.get('PORT', '8080')}/health"


if __name__ == "__main__":
    try:
        healthy = urllib.request.urlopen(probe_url(), timeout=3).status == 200
    except OSError:  # URLError / HTTPError / timeouts
        healthy = False
    sys.exit(0 if healthy else 1)
