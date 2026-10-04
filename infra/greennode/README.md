# GreenNode side: console runbook

Ordered steps on the GreenNode side to let a **Private MCP Gateway** reach an MCP server in your data center
through a **VPN Site-to-Site (IPsec)** tunnel. Do the steps in order. The data-center side lives in
[`../onprem/`](../onprem/README.md); you need it between steps (d) and (f).

> All IP ranges and names are **examples**. Replace them with your own and never reuse `172.30.0.0/16`
> (the AgentBase VPC managed by GreenNode). Where the public GreenNode documentation does not state a value or a
> behavior, this runbook says **verify with GreenNode** instead of guessing.

Sources used: GreenNode docs for VPN Site-to-Site (overview, create, connect conditions, add tunnels, supported IPsec
configuration, packages, FAQ, pfSense demo), VPC Route Table, Network ACL, and AgentBase (Private Networking, Manage MCP Gateway).

Console entry points (HCM03): vNetwork `https://hcm-3-vnetwork.console.greennode.ai/overview`, vServer
`https://hcm-3.console.greennode.ai/vserver/overview`, AgentBase `https://aiplatform.console.greennode.ai/mcp-gateway`.

## (a) CIDR plan

| Network | Example CIDR | Notes |
|---|---|---|
| AgentBase VPC (managed by GreenNode) | `172.30.0.0/16` | Fixed. Agent Runtime and MCP Gateway run here. Nothing else may use this range |
| Customer VPC `vpc-agentbase-hybrid` | `10.20.0.0/16` | Must not overlap the data center (VPN condition: error 2017) |
| Subnet `snet-vpn` | `10.20.0.0/24` | Hosts the Private Gateway IP of the VPN (used as the route target in step d) |
| Subnet `snet-agentbase-gw` | `10.20.1.0/24` | Subnet selected when creating the Private MCP Gateway |
| Subnet `snet-test` | `10.20.2.0/24` | A test vServer that runs `check_connectivity.sh` |
| Data center LAN | `192.168.0.0/16` | The **Remote Private CIDR** of the VPN. Must be a private range and must not overlap the VPC (errors 2017, 2018, 2019, 2023) |
| MCP server host (data center) | `192.168.10.20` | Port `8443` (TLS) or `8080` (plain) |
| GreenNode VPN public IP | shown on the VPN detail page | Peer address for the data-center IPsec device |
| Data center public IP | your edge IP | Entered as **Remote Public Gateway IP** (must be a public IP: errors 2020, 2021) |

Rules: the three private ranges (`172.30.0.0/16`, the VPC, the data center) never overlap; if the data center has several
LANs, each becomes one more tunnel with its own non-overlapping CIDR (step c).

## (b) VPC and subnets

1. vServer, Network, **VPC**: create `vpc-agentbase-hybrid` with `10.20.0.0/16` (or use an existing VPC).
2. Create the three subnets from the table in (a). The GreenNode VPC page describes the exact form; its steps are not reproduced here.
3. **Enable DNS resolution (vDNS) on the VPC.** The Private MCP Gateway requires it (Manage MCP Gateway, prerequisites).
   Where the switch is located depends on the VPC console version: verify with GreenNode if you cannot find it.
4. (Optional) Launch a small vServer `vm-test` in `snet-test` for the connectivity checks in step (k).

## (c) Create the VPN Site-to-Site

vNetwork, **VPN Site To Site**, **Create new VPN Connection**.

| Console field | Value in this sample | Notes |
|---|---|---|
| VPN Name | `vpn-onprem-dc` | |
| Select VPN Package | **Standard** | 300 Mbps default, up to 4 tunnels. **Medium** allows up to 10 tunnels (VPN Packages) |
| VPC (Local) | `vpc-agentbase-hybrid` | The VPC whose CIDR is the local network of the tunnel |
| Subnet | `snet-vpn` | After provisioning, the VPN has a **Private Gateway IP** in this subnet |
| Remote Public Gateway IP | data-center public IP, for example `198.51.100.20` | Public IP of the WAN side of your IPsec device |
| Remote Private CIDR | `192.168.0.0/16` | The data-center LAN |
| Used Your Pre-shared Key | on, with your own key | Generate with `openssl rand -base64 36`. Switch it off to let GreenNode generate the PSK. The key must be identical on both sides |
| Algorithm Configuration (IKE Policy, IPsec Policy) | see the table below | Site = phase 1, Tunnel = phase 2 |

Then review the price on the right side, click **Create A VPN Connection**, and complete the checkout. The VPN shows
**Provisioning** (about 3 to 5 minutes) and then **Active**. Open the VPN name to see the details, including the
**Local Private Gateway** address you need in step (d), and the **log** tab for connection logs.

To connect another data-center LAN or another site, open the VPN and **add a Site/Tunnel** (add, edit, rename,
disconnect and delete are described in "Add/Update/Delete more Site And Tunnel"). Each remote CIDR must not overlap
any other remote CIDR (error 2023).

### IKE and IPsec parameters selected

The docs say GreenNode currently offers a default configuration in the console, with customization announced for
later. The values below come only from the **Support IPSEC Configuration** page and the **pfSense demo**; both ends must
use the same values. If the Algorithm Configuration section or the VPN detail page shows something different, the
console wins: edit [`../onprem/strongswan/swanctl.conf`](../onprem/strongswan/swanctl.conf) to match it.

| Parameter | Selected value | Where it comes from |
|---|---|---|
| IKE version | IKEv2 | Demo, "Config IPSec Phase 1" |
| Authentication | Pre-shared key | Create VPN ("Pre-shared Key") |
| Phase 1 encryption | AES-256-GCM with 128-bit ICV (`aes256gcm128`) | Supported IPsec configuration: marked default value; demo: AES256-GCM, key length 128 |
| Phase 1 hash / PRF | SHA-256 (`sha2_256`) | Supported IPsec configuration: default; "hash is ignored with GCM" (strongSwan still needs a PRF, `prfsha256`); demo: hash 256 |
| Phase 1 DH group | `modp3072` (group 15), with `modp2048` (group 14) as fallback | Demo: DH group 3072. Supported list: `modp2048` is the default value. The two pages differ: verify with GreenNode which one the console applies |
| Phase 1 lifetime | 4 hours | Demo (the page prints `144000`, a typo for 14400 s: verify with GreenNode) |
| Phase 2 encryption / hash | AES-256-GCM (`aes256gcm128`) or AES-256 (`aes256`) with SHA-256 | Supported list (default) and demo (AES256 + SHA256) |
| Phase 2 PFS group | not described | Verify with GreenNode (the strongSwan sample offers proposals with and without `modp3072`) |
| Phase 2 lifetime | 16 hours | Demo (57600 s) |
| Dead peer detection, NAT-T | not described | Verify with GreenNode |

Do not select the algorithms the docs flag as weak: `md5`, `sha`, `modp1024`, `modp1536`, `modp768`.

## (d) Route table entries

Without these routes the tunnel can be established but nothing flows (FAQ: "VPN Tunnel is ESTABLISHED, but cannot ping").

vServer, Network, **Route table**. Select the route table of the VPC (create one for the VPC if there is none: name
5 to 50 characters, letters, digits, `_` and `-`), click **Edit Routes**, add:

| Destination | Target | Purpose |
|---|---|---|
| `192.168.0.0/16` (the data-center CIDR) | the VPN **Local Private Gateway** address, for example `10.20.0.5` | Sends traffic for the data center through the VPN |

Click **Save**. The route must be effective for the subnets that originate traffic: `snet-agentbase-gw` (the
gateway attachment) and `snet-test`. Whether AgentBase traffic uses this route table when it enters your VPC through the
private connection, and whether a route for `172.30.0.0/16` is needed in your VPC, is not described in the docs:
verify with GreenNode (question list in step f).

At this point configure the data center: [`../onprem/strongswan/README.md`](../onprem/strongswan/README.md) and
[`../onprem/firewall/nftables.conf`](../onprem/firewall/nftables.conf). The data center must route **both** the customer VPC
CIDR and `172.30.0.0/16` back into the tunnel, and its firewall must allow source `172.30.0.0/16` and the VPC CIDR to the
MCP port. Whether the gateway source address reaches the data center unchanged or is source-NATed to a VPC address
is not confirmed: verify with GreenNode (the audit log of the MCP server also shows the real source, step k).

## (e) Network ACL and security group rules

Optional hardening. The defaults of the platform already work; tighten only after step (k) passes.

A **Network ACL** works at subnet level and is **stateless**: allow the request and the reply explicitly. It is associated
to subnets (a subnet can have one ACL), rules are evaluated from the lowest priority number, and each ACL contains a
default allow-any rule (priority 0, editable) and a default deny rule (priority 2000, fixed). vServer, Network,
**Network ACLs**, create `acl-agentbase-gw`, edit the rules, then associate `snet-agentbase-gw` (and, if desired,
`snet-test`). Do **not** associate it with `snet-vpn` until GreenNode confirms how the VPN data path interacts with
subnet ACLs (verify with GreenNode).

Example rules (priorities are examples). Rules are evaluated from the lowest number, so the default allow-any rule at
priority 0 matches first and makes the ACL restrict nothing: add the explicit rules, test, then delete that default rule.
(The ACL docs also say a new ACL denies all traffic until rules are added; check what your console shows and
verify with GreenNode if the two statements disagree.)

| Direction | Priority | Protocol | Port range | Source / Destination | Action | Purpose |
|---|---|---|---|---|---|---|
| Inbound | 100 | TCP | 8443 (and 8080 if used) | `172.30.0.0/16` | Allow | Gateway requests entering the subnet |
| Inbound | 110 | TCP | 1024-65535 | `192.168.0.0/16` | Allow | Replies from the MCP server |
| Inbound | 120 | ICMP | all | `192.168.0.0/16` | Allow | Ping from the data center |
| Outbound | 100 | TCP | 8443 (and 8080 if used) | `192.168.0.0/16` | Allow | Requests to the MCP server |
| Outbound | 110 | TCP | 1024-65535 | `172.30.0.0/16` | Allow | Replies to the gateway |
| Outbound | 120 | ICMP | all | `192.168.0.0/16` | Allow | Ping to the data center |

How the "Port range" field is matched (destination port only, or also source port) is not spelled out. If replies are
blocked, widen the reply rules to all ports and test again. If the gateway source turns out to be a VPC address instead
of `172.30.0.0/16`, use that range as the inbound source.

**Security group** of `vm-test`: inbound SSH only from your admin address; outbound TCP 8443 and 8080 to `192.168.0.0/16`
(plus ICMP). Security group rules are stateful (replies are automatic). The exact console fields for security groups are
documented in the GreenNode vServer docs: verify with GreenNode if your default outbound policy is not allow-all.

## (f) Request the AgentBase private connection

A Private gateway lists only VPCs that are **already privately connected to AgentBase** (documented as VPC Peering between
your VPC and the AgentBase VPC). You cannot create it yourself: **contact GreenNode support** to activate it for
`vpc-agentbase-hybrid`.

Include in the request: region, project / account, VPC name and ID, VPC CIDR, and the purpose
("Private MCP Gateway reaching an on-prem MCP server through VPN Site-to-Site; on-prem CIDR `192.168.0.0/16`").
Ask these questions in the same ticket (all are **verify with GreenNode** items):

1. After activation, is a route for `172.30.0.0/16` needed in my VPC route table, and is the route to my VPC CIDR added on the AgentBase side automatically?
2. When the gateway calls an address behind my VPN, is the source seen by the data center `172.30.0.0/16` or a NATed VPC address?
3. Can the VPN tunnel selectors include `172.30.0.0/16`, or must the data center see only the VPC CIDR?
4. Does a connector accept `http://` endpoints, or HTTPS only (the docs say "Full HTTPS URL")? How is a custom or internal CA supplied?
5. Is the Private gateway endpoint reachable only from privately connected networks? Which network must the agent runtime use (Private mode in the same VPC)?

Then click the refresh icon in the gateway form until the VPC appears (step g).

## (g) Create the Private MCP Gateway

AgentBase, **MCP Governance**, **MCP Gateway**, **Create Gateway**. Create the Access Control secret (step h) **before**
filling the MCP server section, because the connector selects it.

| Section | Field | Value |
|---|---|---|
| Basic Configuration | Gateway name | `onprem-erp-gw` (letters, digits, hyphen; 5 to 50 characters; unique in the organization) |
| Inbound Identity | Inbound Auth type | **IAM Permissions** (agents on AgentBase authenticate with their IAM token). JWT is the alternative when you use your own IdP |
| MCP Servers | | Step (i) |
| Policy group | | Step (j) (optional at creation, but **without a Policy Group every `tools/call` returns 403**) |
| Network & Compute | Network mode | **Private** |
| | VPC | `vpc-agentbase-hybrid` (listed only after step f; DNS resolution must be on) |
| | Subnet | `snet-agentbase-gw` (shown as `<subnet-name> - Zone: <zone>` with its CIDR) |
| | Route CIDRs | **`192.168.0.0/16`** (the on-prem CIDR). Add `10.20.0.0/16` only if the gateway must also reach other VPC subnets |
| | Flavor | for example `general-2x4` (2 CPU, 4 GB) |
| | Replicas | 1 to 10 (default 1); use 2 for availability |

Click **Create Gateway**. Status goes from **Creating** to **Active** (within about 30 seconds) and the service account
`sa-gateway-<id>` is created automatically. The gateway detail page shows the **Endpoint URL** that agents call.

Keep Private versus Public in mind: use Private here because the MCP server is only reachable through your VPC and VPN.
Treat the network mode as fixed at creation; changing it later is not documented (verify with GreenNode).

## (h) Access Control: API key provider

Console, AgentBase, **Access Control**: create an **API Key** provider named `onprem-mcp-key` whose value is
exactly the key set in `MCP_API_KEYS` on the on-prem server.

```bash
openssl rand -hex 32     # use the output both in the data center (.env) and here
```

The server refuses keys shorter than 32 characters and any key that contains `<`, `>` or `change-me` (the placeholder of the example files); `/mcp` then answers `503`.

The gateway attaches this key when it calls the server; agents never see it. For rotation, give the server two keys
(`MCP_API_KEYS=old,new`), change the provider value to the new key, then remove the old key from the server.

## (i) Connector `erp`

In the gateway form, section **MCP Servers** (the console may label the entry "MCP Server"; this repo calls it a connector),
or later in the gateway detail page, tab **MCP Servers**:

| Field | Value |
|---|---|
| MCP Server name | `erp` |
| MCP endpoint URL | `https://192.168.10.20:8443/mcp` (TLS through Caddy, recommended). Plain `http://192.168.10.20:8080/mcp` only if GreenNode confirms HTTP is accepted (the docs describe a full HTTPS URL) |
| Outbound Auth | **API Key** |
| Mode | **2LO (machine to machine)** |
| Secret Provider | `onprem-mcp-key` |
| Header key | `X-Api-Key` (the default is `Authorization`) |
| Header value prefix | empty (the default is `Bearer `, which must be cleared for `X-Api-Key`) |

The same configuration through the gateway API (`targets` is a **full replacement**: send every existing connector too):

```json
{
  "name": "erp",
  "type": "MCP",
  "endpoint": "https://192.168.10.20:8443/mcp",
  "outboundAuth": {
    "type": "APIKEY",
    "flow": "2LO",
    "headerName": "X-Api-Key",
    "headerValuePrefix": "",
    "providerName": "onprem-mcp-key"
  }
}
```

The TLS certificate presented by the server must be issued by a CA the gateway trusts. With Caddy `tls internal` or an
enterprise CA, ask GreenNode how to provide that CA to the connector (verify with GreenNode).

## (j) Policy Group

The gateway denies by default. Actions have the form `<connector>__<tool>`, so the tools of the `erp` connector are
`erp__find_employee`, `erp__leave_balance`, `erp__sick_leave_balance`, `erp__list_purchase_orders`,
`erp__get_purchase_order` and `erp__inventory_level`. List the actions explicitly (least privilege). `["*"]` is the only
wildcard and cannot be mixed with specific entries. Policies work per tool, which is why sick leave is a tool of its own:
the HR example below can allow annual leave without exposing sick leave.

Create a Policy Group `onprem-erp-policy` (AgentBase, Policy Groups) with one policy per agent and attach it to the
gateway (gateway **Edit**, section Policy group; the change applies within about 30 seconds).

```json
{
  "effect": "allow",
  "principal": "iam:<hr-agent-principal-id>",
  "actions": ["erp__find_employee", "erp__leave_balance"],
  "resources": ["gateway:onprem-erp-gw"]
}
```

```json
{
  "effect": "allow",
  "principal": "iam:<procurement-agent-principal-id>",
  "actions": ["erp__list_purchase_orders", "erp__get_purchase_order", "erp__inventory_level"],
  "resources": ["gateway:onprem-erp-gw"]
}
```

To allow one agent every tool of the connector, list all six actions in a single policy. If you do not know the exact
principal id of the agent, call a tool once, read the denial and the gateway audit log, then write the rule. Verify the
principal syntax against your gateway version.

## (k) Verification

1. **VPN**: status `Active` in the VPN list; on the data center `swanctl --list-sas` shows `ESTABLISHED` and `INSTALLED`.
2. **Route**: the route table has `192.168.0.0/16` to the VPN Local Private Gateway.
3. **Path from the VPC** (on `vm-test`):
   ```bash
   MCP_API_KEY=<key> ./infra/onprem/check_connectivity.sh 192.168.10.20 8443 https   # add INSECURE=1 for an internal CA
   ```
   Expect `RESULT: PASS (3/3)`. This proves VPC to data center only.
4. **Gateway**: status `Active`; detail page, tab **MCP Servers** lists `erp`; copy the Endpoint URL.
5. **Through the gateway** (from a network that can reach the Private endpoint, with an IAM token or JWT for the inbound auth):
   ```bash
   MCP_URL=<gateway Endpoint URL>/erp MCP_BEARER_TOKEN=<token> python examples/mcp_client.py
   MCP_URL=<gateway Endpoint URL>/erp MCP_BEARER_TOKEN=<token> python examples/mcp_client.py \
     --call erp__find_employee --args '{"query":"nguyen"}'
   ```
   `tools/list` is always allowed. Use the exact tool names printed by `tools/list` (the connector prefix can differ by gateway version).
   `tools/call` returns 403 until the Policy Group allows the principal.
6. **Evidence on the data center**: the audit log shows the tool and the real source address of each call, which also
   answers the "is the gateway source NATed" question:
   ```bash
   docker compose logs mcp | grep audit      # audit tool=find_employee caller=172.30.x.x key=1a2b3c4d status=200 ms=4
   ```
   With the Caddy TLS profile set `TRUST_FORWARDED_FOR=true` so the caller is read from `X-Forwarded-For`
   (the compose file already limits that to requests coming from Caddy). `key` identifies which API key was used
   (first 8 hex digits of its SHA-256), which helps during key rotation.
7. **From an agent**: attach the gateway to the agent (see the AgentBase docs for Agent Runtime, Private mode in the same
   VPC when the gateway endpoint is private) and ask a question that needs HR or procurement data.

Troubleshooting is in the root [README](../../README.md#troubleshooting).
