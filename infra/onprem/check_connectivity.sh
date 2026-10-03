#!/usr/bin/env bash
# Check the path to the on-prem MCP server from a host in the customer VPC (for example a vServer).
# Requires only bash and curl.
#
# Usage:
#   MCP_API_KEY=<key> ./check_connectivity.sh <host> [port] [scheme]
#   MCP_API_KEY=<key> ./check_connectivity.sh 192.168.10.20 8443 https
#
# Environment: MCP_API_KEY (required for step 3), INSECURE=1 (skip certificate verification, for an
#              internal CA you have not installed yet), TIMEOUT (seconds, default 5)
#
# What a PASS proves: the VPC -> VPN -> data center path works. It does NOT prove the MCP Gateway
# (source 172.30.0.0/16) path; that also needs the Route CIDRs on the gateway, the return route in the
# data center and the firewall rule for 172.30.0.0/16 (see README, troubleshooting).
set -u

HOST="${1:-}"
PORT="${2:-8080}"
SCHEME="${3:-http}"
TIMEOUT="${TIMEOUT:-5}"

if [[ -z "$HOST" || "$HOST" == "-h" || "$HOST" == "--help" ]]; then
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required"; exit 2; }

BASE="${SCHEME}://${HOST}:${PORT}"
CURL_OPTS=(-sS --max-time "$TIMEOUT")
[[ "${INSECURE:-0}" == "1" ]] && CURL_OPTS+=(-k)

fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '      -> %s\n' "$2"; fails=$((fails + 1)); }

echo "Target: ${BASE}/mcp"
echo "-----------------------------------------------"

# ---- Step 1: TCP reachability (bash /dev/tcp, no nc needed) ----
tcp_ok=0
if command -v timeout >/dev/null 2>&1; then
  timeout "$TIMEOUT" bash -c "exec 3<>/dev/tcp/${HOST}/${PORT}" 2>/dev/null && tcp_ok=1
else
  # macOS has no `timeout`: let curl attempt the TCP handshake instead
  curl -sS --max-time "$TIMEOUT" --connect-timeout "$TIMEOUT" -o /dev/null "http://${HOST}:${PORT}/" 2>/dev/null
  rc=$?
  # rc 7 (refused) / 28 (timeout) / 6 (DNS) = unreachable; any other code means TCP connected
  [[ $rc -ne 7 && $rc -ne 28 && $rc -ne 6 ]] && tcp_ok=1
fi
if [[ $tcp_ok -eq 1 ]]; then
  pass "[1/3] TCP ${HOST}:${PORT} reachable"
else
  fail "[1/3] TCP ${HOST}:${PORT} not reachable" \
       "check: VPN tunnel state, VPC route table (on-prem CIDR -> VPN gateway), data-center firewall, service listening"
fi

# ---- Step 2: GET /health (no key needed) ----
if [[ $tcp_ok -eq 1 ]]; then
  body=$(curl "${CURL_OPTS[@]}" -o - -w '\n%{http_code}' "${BASE}/health" 2>&1)
  code=$(printf '%s' "$body" | tail -n1)
  if [[ "$code" == "200" ]]; then
    pass "[2/3] GET /health -> 200"
  else
    fail "[2/3] GET /health -> ${code:-no response}" \
         "for TLS errors set INSECURE=1 or install the internal CA; check scheme and port (${SCHEME}:${PORT})"
  fi
else
  fail "[2/3] GET /health skipped (step 1 failed)"
fi

# ---- Step 3: authenticated tools/list ----
if [[ -z "${MCP_API_KEY:-}" ]]; then
  fail "[3/3] tools/list skipped" "set MCP_API_KEY=<key> and run again"
elif [[ $tcp_ok -ne 1 ]]; then
  fail "[3/3] tools/list skipped (step 1 failed)"
else
  resp=$(curl "${CURL_OPTS[@]}" -X POST "${BASE}/mcp" \
    -H "X-Api-Key: ${MCP_API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    -w '\n%{http_code}' 2>&1)
  code=$(printf '%s' "$resp" | tail -n1)
  out=$(printf '%s' "$resp" | sed '$d')
  case "$code" in
    200)
      if printf '%s' "$out" | grep -q '"tools"'; then
        n=$(printf '%s' "$out" | grep -o '"name"' | wc -l | tr -d ' ')
        pass "[3/3] POST /mcp tools/list -> 200 (${n} tool names; expected 5)"
      else
        fail "[3/3] POST /mcp -> 200 but no \"tools\" in the response" "${out:0:200}"
      fi ;;
    401) fail "[3/3] POST /mcp -> 401" "API key missing or wrong: compare with MCP_API_KEYS on the server" ;;
    503) fail "[3/3] POST /mcp -> 503" "the server has no MCP_API_KEYS configured (fail-closed)" ;;
    *)   fail "[3/3] POST /mcp -> ${code:-no response}" "${out:0:200}" ;;
  esac
fi

echo "-----------------------------------------------"
if [[ $fails -eq 0 ]]; then
  echo "RESULT: PASS (3/3)"
  exit 0
fi
echo "RESULT: FAIL (${fails} failed step(s))"
exit 1
