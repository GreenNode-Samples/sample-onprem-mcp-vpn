#!/usr/bin/env bash
# LAB ONLY: turn an Ubuntu VM that is NOT in your GreenNode customer VPC into a simulated "data center".
#
#   - installs strongSwan and brings up the IKEv2 tunnel to the GreenNode VPN (from infra/onprem/strongswan/swanctl.conf)
#   - creates a dummy interface `lan0` that plays the on-prem LAN (default 192.168.10.20/24)
#   - runs the MCP server (and optionally Caddy TLS) in Docker with host networking, bound to the LAN address
#
# Usage (as root):
#   export GN_VPN_IP=<GreenNode VPN public IP>        # from the VPN detail page
#   export LAB_PUBLIC_IP=<this VM public IP>           # = "Remote Public Gateway IP" entered in the console
#   export PSK=<pre-shared key>                        # optional: prompted if unset
#   # optional: LOCAL_PRIVATE_IP (when the public IP is NAT-ed by the cloud), LAN_CIDR, LAN_IP, VPC_CIDR, WITH_TLS=0|1
#   sudo -E ./lab/lab_setup_onprem.sh
#   sudo -E ./lab/lab_setup_onprem.sh teardown         # remove everything this script created
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

if [[ "${1:-}" == "teardown" ]]; then
  docker rm -f onprem-mcp onprem-caddy >/dev/null 2>&1 || true
  swanctl --terminate --ike greennode-vpn >/dev/null 2>&1 || true
  rm -f /etc/swanctl/swanctl.conf /etc/swanctl/conf.d/greennode.secrets.conf
  swanctl --load-all >/dev/null 2>&1 || true
  ip link del lan0 >/dev/null 2>&1 || true
  echo "Lab removed (the API key file ${KEY_FILE} and strongSwan packages were left in place)."
  exit 0
fi

: "${GN_VPN_IP:?export GN_VPN_IP=<GreenNode VPN public IP>}"
: "${LAB_PUBLIC_IP:?export LAB_PUBLIC_IP=<public IP of this VM>}"
LOCAL_PRIVATE_IP="${LOCAL_PRIVATE_IP:-$LAB_PUBLIC_IP}"
if [[ -z "${PSK:-}" ]]; then
  read -rsp "Pre-shared key (same as in the GreenNode VPN): " PSK
  echo
fi
[[ -n "$PSK" ]] || { echo "PSK must not be empty" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "Docker Engine is required: https://docs.docker.com/engine/install/ubuntu/" >&2; exit 2; }

echo "[1/5] Installing strongSwan"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq strongswan-swanctl charon-systemd curl >/dev/null

echo "[2/5] Creating the simulated on-prem LAN (lan0 ${LAN_IP})"
ip link show lan0 >/dev/null 2>&1 || ip link add lan0 type dummy
ip addr replace "${LAN_IP}/${LAN_CIDR#*/}" dev lan0
ip link set lan0 up   # not persistent across reboots: re-run this script after a reboot

echo "[3/5] Kernel settings"
cat > /etc/sysctl.d/99-ipsec.conf <<'EOF_SYSCTL'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_redirects = 0
EOF_SYSCTL
sysctl --system >/dev/null

echo "[4/5] Writing the strongSwan configuration and starting the tunnel"
[[ -f /etc/swanctl/swanctl.conf && ! -f /etc/swanctl/swanctl.conf.pre-lab ]] && cp /etc/swanctl/swanctl.conf /etc/swanctl/swanctl.conf.pre-lab
mkdir -p /etc/swanctl/conf.d
# Substitute the example values of the repo template (placeholder IPs are RFC 5737 / example ranges).
sed -E \
    -e "s|203\.0\.113\.10|${GN_VPN_IP}|g" \
    -e "s|198\.51\.100\.20|${LAB_PUBLIC_IP}|g" \
    -e "s|^([[:space:]]*local_addrs[[:space:]]*=[[:space:]]*)[^[:space:]#]+|\1${LOCAL_PRIVATE_IP}|" \
    -e "s|^([[:space:]]*local_ts[[:space:]]*=[[:space:]]*)[^[:space:]#]+|\1${LAN_CIDR}|" \
    -e "s|10\.20\.0\.0/16|${VPC_CIDR}|g" \
    "${REPO_DIR}/infra/onprem/strongswan/swanctl.conf" > /etc/swanctl/swanctl.conf
umask 077
cat > /etc/swanctl/conf.d/greennode.secrets.conf <<EOF_SECRET
secrets {
    ike-greennode {
        id-1 = ${GN_VPN_IP}
        secret = "${PSK}"
    }
}
EOF_SECRET
umask 022
systemctl enable --now strongswan >/dev/null 2>&1 || systemctl enable --now strongswan-starter >/dev/null 2>&1 || true
swanctl --load-all
swanctl --initiate --child greennode-vpc || echo "Initiate failed or GreenNode will initiate: check 'swanctl --list-sas' and journalctl -u strongswan"
swanctl --list-sas || true

echo "[5/5] Starting the MCP server on ${LAN_IP}"
if [[ ! -f "$KEY_FILE" ]]; then
  umask 077
  echo "MCP_API_KEYS=$(openssl rand -hex 32)" > "$KEY_FILE"
  umask 022
fi
# shellcheck disable=SC1090
source "$KEY_FILE"
docker build -q -t onprem-mcp-server:lab "$REPO_DIR" >/dev/null
docker rm -f onprem-mcp onprem-caddy >/dev/null 2>&1 || true
docker run -d --name onprem-mcp --restart unless-stopped --network host \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -v onprem_mcp_lab_data:/data \
  -e HOST="$LAN_IP" -e PORT=8080 -e MCP_API_KEYS="$MCP_API_KEYS" \
  -e TRUST_FORWARDED_FOR="$([[ "$WITH_TLS" == "1" ]] && echo true || echo false)" \
  onprem-mcp-server:lab >/dev/null
if [[ "$WITH_TLS" == "1" ]]; then
  docker run -d --name onprem-caddy --restart unless-stopped --network host \
    -e CADDY_SITE="$LAN_IP" -e MCP_UPSTREAM="${LAN_IP}:8080" \
    -v "${REPO_DIR}/infra/onprem/Caddyfile:/etc/caddy/Caddyfile:ro" -v onprem_caddy_lab_data:/data \
    caddy:2 >/dev/null
fi

sleep 3
echo
echo "Lab ready."
echo "  MCP server : http://${LAN_IP}:8080/mcp$([[ "$WITH_TLS" == "1" ]] && echo "   and   https://${LAN_IP}:8443/mcp")"
echo "  API key    : stored in ${KEY_FILE} (use it as the Access Control secret and for check_connectivity.sh)"
echo "  Tunnel     : swanctl --list-sas   |   MCP audit log: docker logs onprem-mcp 2>&1 | grep audit"
echo "Next: follow lab/README.md, step 'Verify from the customer VPC'."
