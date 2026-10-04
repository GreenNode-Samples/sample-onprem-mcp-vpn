"""Container health probe: GET /health on the address the server is bound to; exit status 0 = healthy.

Reads the same HOST and PORT variables as main.py. A server bound to 0.0.0.0 answers on loopback too; one bound
to a single address (HOST=192.168.10.20, or 127.0.0.1 behind Caddy) only answers on that address.
"""

import os
import sys
import urllib.request

host = os.environ.get("HOST", "0.0.0.0")
if host == "0.0.0.0":
    host = "127.0.0.1"
port = os.environ.get("PORT", "8080")

try:
    urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3).close()  # non-2xx raises
except Exception:
    sys.exit(1)
