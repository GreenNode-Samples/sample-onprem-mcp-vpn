#!/usr/bin/env bash
# LAB ONLY: turn an Ubuntu VM that is NOT in your GreenNode customer VPC into a simulated "data center".
#
#   - installs strongSwan and brings up the IKEv2 tunnel to the GreenNode VPN (from infra/onprem/strongswan/swanctl.conf;
#     the child SA has start_action=start, so loading the configuration starts the tunnel: no manual initiate)
#   - creates a dummy interface `lan0` that plays the on-prem LAN (default 192.168.10.20/24)
#   - runs the MCP server and Caddy TLS in Docker with host networking:
#       WITH_TLS=1 (default)  server on 127.0.0.1:8080 (only Caddy can reach it), Caddy on <LAN_IP>:8443
#       WITH_TLS=0            plain server on <LAN_IP>:8080
#
# Usage (as root):
#   export GN_VPN_IP=<GreenNode VPN public IP>        # from the VPN detail page
#   export LAB_PUBLIC_IP=<this VM public IP>           # = "Remote Public Gateway IP" entered in the console
#   export PSK=<pre-shared key>                        # optional: prompted if unset
#   # optional: LOCAL_PRIVATE_IP (when the public IP is NAT-ed by the cloud), LAN_CIDR, LAN_IP, VPC_CIDR, WITH_TLS=0|1
#   sudo -E ./lab/lab_setup_onprem.sh
#   sudo -E ./lab/lab_setup_onprem.sh teardown         # undo everything this script changed (safe to repeat)
#
# Requires: Docker Engine already installed. Re-running the script is safe (idempotent).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo -E $0" >&2; exit 2; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAN_CIDR="${LAN_CIDR:-192.168.10.0/24}"
LAN_IP="${LAN_IP:-192.168.10.20}"
VPC_CIDR="${VPC_CIDR:-10.20.0.0/16}"
WITH_TLS="${WITH_TLS:-1}"
KEY_FILE="/root/.onprem-mcp-lab.env"
SWANCTL_CONF="/etc/swanctl/swanctl.conf"
SECRETS_CONF="/etc/swanctl/conf.d/greennode.secrets.conf"
SYSCTL_CONF="/etc/sysctl.d/99-ipsec.conf"
MCP_IMAGE="onprem-mcp-server:lab"
CADDY_IMAGE="caddy:2.11.6"   # keep in sync with infra/onprem/docker-compose.yml

if [[ "${1:-}" == "teardown" ]]; then
  echo "Removing containers, volumes and the image built by this script"
  docker rm -f onprem-mcp onprem-caddy >/dev/null 2>&1 || true
  docker volume rm onprem_mcp_lab_data onprem_caddy_lab_data >/dev/null 2>&1 || true
  docker image rm "$MCP_IMAGE" >/dev/null 2>&1 || true

  echo "Stopping the tunnel and restoring the strongSwan configuration"
  swanctl --terminate --ike greennode-vpn >/dev/null 2>&1 || true
  rm -f "$SECRETS_CONF"
  if [[ -f "${SWANCTL_CONF}.pre-lab" ]]; then
    mv -f "${SWANCTL_CONF}.pre-lab" "$SWANCTL_CONF"
  else
    rm -f "$SWANCTL_CONF"
  fi
  swanctl --load-all --clear --noprompt >/dev/null 2>&1 || true   # forgets the lab credentials and connection

  echo "Removing kernel settings and the simulated LAN"
  if [[ -f "$SYSCTL_CONF" ]]; then
    rm -f "$SYSCTL_CONF"
    sysctl --system >/dev/null   # values changed at runtime return to their defaults at the next reboot
  fi
  ip link del lan0 >/dev/null 2>&1 || true

  echo "Lab removed. Left in place: ${KEY_FILE} (delete it if you do not need the key), the strongSwan packages and service."
  exit 0
fi

: "${GN_VPN_IP:?export GN_VPN_IP=<GreenNode VPN public IP>}"
: "${LAB_PUBLIC_IP:?export LAB_PUBLIC_IP=<public IP of this VM>}"
LOCAL_PRIVATE_IP="${LOCAL_PRIVATE_IP:-$LAB_PUBLIC_IP}"

# The values end up in sed expressions and in the strongSwan configuration: accept IPv4 addresses and CIDRs only.
require_match() {   # require_match <name> <value> <extended regex>
  [[ "$2" =~ $3 ]] || { echo "$1 must be an IPv4 address or CIDR (got: $2)" >&2; exit 2; }
}
IPV4='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
CIDR='^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$'
require_match GN_VPN_IP "$GN_VPN_IP" "$IPV4"
require_match LAB_PUBLIC_IP "$LAB_PUBLIC_IP" "$IPV4"
require_match LOCAL_PRIVATE_IP "$LOCAL_PRIVATE_IP" "$IPV4"
require_match LAN_IP "$LAN_IP" "$IPV4"
require_match LAN_CIDR "$LAN_CIDR" "$CIDR"
require_match VPC_CIDR "$VPC_CIDR" "$CIDR"

if [[ -z "${PSK:-}" ]]; then
  read -rsp "Pre-shared key (same as in the GreenNode VPN): " PSK
  echo
fi
[[ -n "$PSK" ]] || { echo "PSK must not be empty" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "Docker Engine is required: https://docs.docker.com/engine/install/ubuntu/" >&2; exit 2; }

echo "[1/5] Installing strongSwan"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq strongswan-swanctl charon-systemd openssl >/dev/null

echo "[2/5] Creating the simulated on-prem LAN (lan0 ${LAN_IP})"
ip link show lan0 >/dev/null 2>&1 || ip link add lan0 type dummy
ip addr replace "${LAN_IP}/${LAN_CIDR#*/}" dev lan0
ip link set lan0 up   # not persistent across reboots: re-run this script after a reboot

echo "[3/5] Kernel settings"
cat > "$SYSCTL_CONF" <<'EOF_SYSCTL'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_redirects = 0
EOF_SYSCTL
sysctl --system >/dev/null

echo "[4/5] Writing the strongSwan configuration and starting the tunnel"
if [[ -f "$SWANCTL_CONF" && ! -f "${SWANCTL_CONF}.pre-lab" ]]; then
  cp "$SWANCTL_CONF" "${SWANCTL_CONF}.pre-lab"   # restored by `teardown`
fi
mkdir -p /etc/swanctl/conf.d
# Substitute the example values of the repo template (placeholder IPs are RFC 5737 / example ranges).
sed -E \
    -e "s|203\.0\.113\.10|${GN_VPN_IP}|g" \
    -e "s|198\.51\.100\.20|${LAB_PUBLIC_IP}|g" \
    -e "s|^([[:space:]]*local_addrs[[:space:]]*=[[:space:]]*)[^[:space:]#]+|\1${LOCAL_PRIVATE_IP}|" \
    -e "s|^([[:space:]]*local_ts[[:space:]]*=[[:space:]]*)[^[:space:]#]+|\1${LAN_CIDR}|" \
    -e "s|10\.20\.0\.0/16|${VPC_CIDR}|g" \
    "${REPO_DIR}/infra/onprem/strongswan/swanctl.conf" > "$SWANCTL_CONF"
# The key is written hex encoded (0x...): a PSK that contains quotes, backslashes or $ needs no escaping.
PSK_HEX="$(printf '%s' "$PSK" | od -An -v -tx1 | tr -d ' \n')"
(
  umask 077
  cat > "$SECRETS_CONF" <<EOF_SECRET
secrets {
    ike-greennode {
        id-1 = ${GN_VPN_IP}
        secret = 0x${PSK_HEX}
    }
}
EOF_SECRET
)
systemctl enable --now strongswan >/dev/null 2>&1 || systemctl enable --now strongswan-starter >/dev/null 2>&1 || true
# start_action=start in swanctl.conf: loading the configuration starts the tunnel (no `swanctl --initiate`, which would
# race with it and create a duplicate SA). It retries by itself while the GreenNode VPN is not Active yet.
swanctl --load-all --clear --noprompt
sleep 3
swanctl --list-sas || true

echo "[5/5] Starting the MCP server"
if [[ ! -f "$KEY_FILE" ]]; then
  (umask 077; echo "MCP_API_KEYS=$(openssl rand -hex 32)" > "$KEY_FILE")
fi
docker build -q -t "$MCP_IMAGE" "$REPO_DIR" >/dev/null
docker rm -f onprem-mcp onprem-caddy >/dev/null 2>&1 || true
if [[ "$WITH_TLS" == "1" ]]; then
  # Only Caddy (same host, loopback) may reach the server, so X-Forwarded-For can be trusted and cannot be forged.
  BIND_HOST="127.0.0.1"
  TRUST_XFF="true"
else
  BIND_HOST="$LAN_IP"
  TRUST_XFF="false"
fi
# The API key comes from a file, so it never shows up in `ps` or in the container's command line.
docker run -d --name onprem-mcp --restart unless-stopped --network host \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -v onprem_mcp_lab_data:/data \
  --env-file "$KEY_FILE" \
  -e HOST="$BIND_HOST" -e PORT=8080 -e TRUST_FORWARDED_FOR="$TRUST_XFF" \
  "$MCP_IMAGE" >/dev/null
if [[ "$WITH_TLS" == "1" ]]; then
  docker run -d --name onprem-caddy --restart unless-stopped --network host \
    -e CADDY_SITE="$LAN_IP" -e CADDY_BIND="$LAN_IP" -e MCP_UPSTREAM="127.0.0.1:8080" \
    -v "${REPO_DIR}/infra/onprem/Caddyfile:/etc/caddy/Caddyfile:ro" -v onprem_caddy_lab_data:/data \
    "$CADDY_IMAGE" >/dev/null
fi

sleep 3
echo
echo "Lab ready."
if [[ "$WITH_TLS" == "1" ]]; then
  echo "  MCP server : https://${LAN_IP}:8443/mcp   (Caddy TLS; the server itself listens on 127.0.0.1:8080 only)"
else
  echo "  MCP server : http://${LAN_IP}:8080/mcp"
fi
echo "  API key    : stored in ${KEY_FILE} (use it as the Access Control secret and for check_connectivity.sh)"
echo "  Tunnel     : swanctl --list-sas   |   MCP audit log: docker logs onprem-mcp 2>&1 | grep audit"
echo "Next: follow lab/README.md, step 'Verify from the customer VPC'."
