#!/usr/bin/env bash
# Check the path to the on-prem MCP server from a host in the customer VPC (for example a vServer).
# Requires only bash and curl.
#
# Usage:
#   MCP_API_KEY=<key> ./check_connectivity.sh <host> [port] [scheme]
#   MCP_API_KEY=<key> ./check_connectivity.sh 192.168.10.20 8443 https
#
# Environment: MCP_API_KEY (required for step 3; sent to curl through stdin, never on its command line),
#              INSECURE=1 (skip certificate verification, for an internal CA you have not installed yet),
#              TIMEOUT (seconds, default 5),
#              EXPECTED_TOOLS (number of tools the server must list, default 6)
#
# What a PASS proves: the VPC -> VPN -> data center path works. It does NOT prove the MCP Gateway
# (source 172.30.0.0/16) path; that also needs the Route CIDRs on the gateway, the return route in the
# data center and the firewall rule for 172.30.0.0/16 (see README, troubleshooting).
set -u

HOST="${1:-}"
PORT="${2:-8080}"
SCHEME="${3:-http}"
TIMEOUT="${TIMEOUT:-5}"
EXPECTED_TOOLS="${EXPECTED_TOOLS:-6}"

if [[ -z "$HOST" || "$HOST" == "-h" || "$HOST" == "--help" ]]; then
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required"; exit 2; }

BASE="${SCHEME}://${HOST}:${PORT}"
CURL_OPTS=(-sS --max-time "$TIMEOUT")
[[ "${INSECURE:-0}" == "1" ]] && CURL_OPTS+=(-k)

fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '      -> %s\n' "$2"; fails=$((fails + 1)); }

# request <path> [curl options...]: sets `code` (000 = no HTTP response), `body` and `err` (curl's error message).
# Extra curl settings, such as a secret header, go in $curl_config and reach curl on stdin (`-K -`, printf is a
# shell builtin), so they never appear on a command line where `ps` could show them.
request() {
  local errfile
  errfile=$(mktemp)
  body=$(printf '%s\n' "${curl_config:-}" | curl "${CURL_OPTS[@]}" -K - "${@:2}" -w '\n%{http_code}' "${BASE}${1}" 2>"$errfile")
  code=${body##*$'\n'}
  body=${body%$'\n'*}
  err=$(<"$errfile")
  rm -f "$errfile"
}

echo "Target: ${BASE}/mcp"
echo "-----------------------------------------------"

# ---- Step 1: TCP reachability (bash /dev/tcp, no nc needed) ----
tcp_ok=0
if command -v timeout >/dev/null 2>&1; then
  # host and port are passed as arguments, never spliced into the command string
  # shellcheck disable=SC2016  # $1 and $2 are meant to expand inside the inner bash, not here
  timeout "$TIMEOUT" bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$HOST" "$PORT" 2>/dev/null && tcp_ok=1
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
  request /health
  if [[ "$code" == "200" ]]; then
    pass "[2/3] GET /health -> 200"
  elif [[ "$code" == "000" ]]; then
    fail "[2/3] GET /health -> no HTTP response" \
         "${err:-no error text from curl}; for TLS errors set INSECURE=1 or install the internal CA; check scheme and port (${SCHEME}:${PORT})"
  else
    fail "[2/3] GET /health -> ${code}" "check scheme and port (${SCHEME}:${PORT}) and that this is the MCP server"
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
  # curl config syntax: inside double quotes a backslash and a double quote must be escaped
  key=${MCP_API_KEY//\\/\\\\}
  key=${key//\"/\\\"}
  curl_config="header = \"X-Api-Key: ${key}\""
  request /mcp -X POST \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
  curl_config=
  case "$code" in
    200)
      if [[ "$body" == *'"tools"'* ]]; then
        # every tool object has exactly one "inputSchema" key
        n=$(printf '%s' "$body" | grep -o '"inputSchema"' | wc -l | tr -d ' ')
        if [[ "$n" == "$EXPECTED_TOOLS" ]]; then
          pass "[3/3] POST /mcp tools/list -> 200 (${n} tools)"
        else
          fail "[3/3] POST /mcp tools/list -> 200 but ${n} tools, expected ${EXPECTED_TOOLS}" \
               "wrong server or version? set EXPECTED_TOOLS if you added or removed tools"
        fi
      else
        fail "[3/3] POST /mcp -> 200 but no \"tools\" in the response" "${body:0:200}"
      fi ;;
    401) fail "[3/3] POST /mcp -> 401" "API key missing or wrong: compare with MCP_API_KEYS on the server" ;;
    503) fail "[3/3] POST /mcp -> 503" "the server has no usable MCP_API_KEYS (not set, or a placeholder / too short: see its log)" ;;
    000) fail "[3/3] POST /mcp -> no HTTP response" "${err:-no error text from curl}" ;;
    *)   fail "[3/3] POST /mcp -> ${code}" "${body:0:200}" ;;
  esac
fi

echo "-----------------------------------------------"
if [[ $fails -eq 0 ]]; then
  echo "RESULT: PASS (3/3)"
  exit 0
fi
echo "RESULT: FAIL (${fails} failed step(s))"
exit 1
