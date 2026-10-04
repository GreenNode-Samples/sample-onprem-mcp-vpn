# Lab: test the whole path without a real data center

You can reproduce the on-prem scenario with **any Linux VM that is outside your customer VPC**. The VM plays the
data center: it runs strongSwan (the IPsec peer) and the MCP server, and exposes a private "LAN" address through a dummy
network interface. The GreenNode side (VPN, routes, Private MCP Gateway) is configured exactly as in production.

```
vm-test (customer VPC 10.20.2.0/24)           Private MCP Gateway (AgentBase VPC 172.30.0.0/16)
        \                                           /
         customer VPC 10.20.0.0/16 ---- GreenNode VPN gateway
                                              ||  IPsec (IKEv2) over the Internet
                                   lab VM "data center" (public IP, strongSwan)
                                     lan0 192.168.10.20  ->  MCP server :8080 / :8443
```

## What you need

| Item | Notes |
|---|---|
| Lab VM | Ubuntu 22.04 or 24.04, 1 vCPU and 1 GB RAM are enough, a public IPv4 address, Docker Engine installed. Use a vServer in **another VPC or project**, or a VM on another cloud. It must **not** be inside the customer VPC |
| Customer VPC and `vm-test` | Created as in [`../infra/greennode/README.md`](../infra/greennode/README.md), steps (a) and (b) |
| A GreenNode account that may create VPN Site-to-Site | A VPN is a billed resource: delete it when you finish |
| For the gateway part | GreenNode support has activated the private connection for your VPC (step f of the runbook) |

Lab values (the script defaults): lab LAN `192.168.10.0/24`, MCP host `192.168.10.20`, customer VPC `10.20.0.0/16`.
The lab LAN and the customer VPC must not overlap each other or `172.30.0.0/16`.

## Step by step

### 1. Prepare the lab VM

1. Create the VM and note its **public IP** (`LAB_PUBLIC_IP`). If the cloud NATs that address (the VM only sees a private
   address), also note the VM private address (`LOCAL_PRIVATE_IP`).
2. Allow inbound SSH from your address. You will open UDP 500 and 4500 for the GreenNode VPN IP in step 3, once it is known.
3. Clone this repository on the VM.

### 2. GreenNode side: VPC, VPN, route

Follow [`../infra/greennode/README.md`](../infra/greennode/README.md), steps (a) to (d), with these lab values:

| Console field | Lab value |
|---|---|
| Remote Public Gateway IP | `LAB_PUBLIC_IP` |
| Remote Private CIDR | `192.168.10.0/24` |
| Pre-shared key | your own, for example `openssl rand -base64 36` |
| Route table | Destination `192.168.10.0/24`, Target = the VPN Local Private Gateway |

When the VPN is **Active**, copy the **GreenNode VPN public IP** from the VPN detail page (`GN_VPN_IP`).

### 3. Open the IPsec ports on the lab VM

Allow UDP 500, UDP 4500 and ESP (IP protocol 50) **from `GN_VPN_IP` only** in the cloud security group or firewall of the
lab VM. If your cloud security groups cannot filter ESP, rely on UDP 4500 NAT-T and verify with GreenNode whether it is supported.

### 4. Bring up the lab "data center"

```bash
export GN_VPN_IP=<GreenNode VPN public IP>
export LAB_PUBLIC_IP=<lab VM public IP>
# export LOCAL_PRIVATE_IP=<VM private IP>      # only when the cloud NATs the public IP
# export PSK=<pre-shared key>                  # otherwise you are prompted
sudo -E ./lab/lab_setup_onprem.sh
```

The script installs strongSwan, renders [`../infra/onprem/strongswan/swanctl.conf`](../infra/onprem/strongswan/swanctl.conf) with
your values, creates `lan0` and loads the configuration (its `start_action=start` brings the tunnel up by itself, so
there is no manual initiate), then builds and runs the MCP server. The API key is generated into
`/root/.onprem-mcp-lab.env` and handed to the container through `--env-file`.

| `WITH_TLS` | What listens where |
|---|---|
| `1` (default) | Caddy on `192.168.10.20:8443` (HTTPS). The MCP server listens on `127.0.0.1:8080` only, so nothing but Caddy can reach it and its `X-Forwarded-For` header (`TRUST_FORWARDED_FOR=true`, trusted from loopback only) cannot be forged |
| `0` | Plain MCP server on `192.168.10.20:8080`, no Caddy |

If the IKE or ESP proposals do not match the VPN policy shown by GreenNode, `journalctl -u strongswan` shows
`NO_PROPOSAL_CHOSEN`: edit the proposals in the repo template (they come from the supported list in the GreenNode docs)
and run the script again.

### 5. Verify the tunnel

On the lab VM:

```bash
sudo swanctl --list-sas        # IKE SA ESTABLISHED, child greennode-vpc INSTALLED
sudo tcpdump -ni any 'esp or udp port 4500'   # encrypted packets while you run the next step
```

On `vm-test` (inside the customer VPC):

```bash
ping -c3 192.168.10.20         # needs the route from step 2 and the tunnel
```

### 6. Verify from the customer VPC

Copy [`../infra/onprem/check_connectivity.sh`](../infra/onprem/check_connectivity.sh) to `vm-test` and run it with the key from
the lab VM (`sudo cat /root/.onprem-mcp-lab.env`):

```bash
MCP_API_KEY=<key> INSECURE=1 ./check_connectivity.sh 192.168.10.20 8443 https   # WITH_TLS=1 (default)
MCP_API_KEY=<key> ./check_connectivity.sh 192.168.10.20 8080 http                # WITH_TLS=0
```

Run the line that matches your `WITH_TLS` choice (the HTTPS one by default); it must end with `RESULT: PASS (3/3)`.
Typical failures:

| Failing step | Likely cause |
|---|---|
| 1/3 TCP | Tunnel down, missing route in the VPC, container not running, lab VM firewall |
| 2/3 `/health` | Wrong scheme or port; TLS error (use `INSECURE=1` for the Caddy internal CA) |
| 3/3 `tools/list` 401 / 503 | Wrong key / server without `MCP_API_KEYS` |

### 7. Call the MCP server through the gateway

1. Ask GreenNode support to activate the private connection for the VPC (runbook step f), if not done yet.
2. Complete runbook steps (g) to (j): Access Control provider `onprem-mcp-key` with the lab key, the Private gateway with
   Route CIDRs `192.168.10.0/24`, connector `erp` with `https://192.168.10.20:8443/mcp`, and a Policy Group.
3. Call a tool through the gateway (see runbook step k.5):
   ```bash
   MCP_URL=<gateway Endpoint URL>/erp MCP_BEARER_TOKEN=<IAM token or JWT> python examples/mcp_client.py \
     --call erp__find_employee --args '{"query":"nguyen"}'
   ```
4. On the lab VM, confirm that the request really crossed the tunnel and see its source address:
   ```bash
   docker logs onprem-mcp 2>&1 | grep audit        # audit tool=find_employee caller=<gateway source>
   ```
   Record whether `caller` is in `172.30.0.0/16` or a VPC address: this answers the NAT question for your environment.

If the gateway returns an error although `check_connectivity.sh` passes from `vm-test`, the problem is specific to the
gateway path: Route CIDRs, the return route for `172.30.0.0/16` on the lab VM, the certificate trust of the connector
(Caddy `tls internal` issues a private CA), or the HTTP/HTTPS rule. See the root README troubleshooting table and
the **verify with GreenNode** list.

### 8. Clean up

```bash
sudo ./lab/lab_setup_onprem.sh teardown     # on the lab VM; safe to run twice
```

It removes the MCP and Caddy containers with their volumes and the image it built, stops the tunnel, restores the
strongSwan `swanctl.conf` you had before (`swanctl.conf.pre-lab`), removes the pre-shared key file from `/etc/swanctl/conf.d`, removes
`/etc/sysctl.d/99-ipsec.conf` (the forwarding values go back to their defaults at the next reboot) and deletes `lan0`.
It leaves the strongSwan packages and `/root/.onprem-mcp-lab.env` in place. If you applied the firewall file,
`sudo nft delete table inet onprem_mcp` removes it.

Then delete the gateway and connector, the Access Control provider, the VPN (it is billed), the route table entry and the lab VM.

## Limits of the lab

- The lab "data center" has no corporate firewall or router: apply [`../infra/onprem/firewall/nftables.conf`](../infra/onprem/firewall/nftables.conf)
  to practice the source restriction (the script does not install it). Edit the `define` lines first and **set `ADMIN_NET`
  to your own IP address**: the input policy is drop and SSH is allowed only from `ADMIN_NET`, so with the example value
  you lock yourself out of the VM. If you do not want to risk that, skip the firewall in the lab.
- Docker uses host networking in the lab so that IPsec policies see the real addresses; a production host with published
  ports is described in [`../infra/onprem/README.md`](../infra/onprem/README.md).
- A VM with a public IP behind cloud NAT depends on NAT-T behavior: verify with GreenNode if the tunnel does not come up.
