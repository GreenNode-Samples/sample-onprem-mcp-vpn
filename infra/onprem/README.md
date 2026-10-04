# Data-center side (customer on-premises)

This folder contains everything that runs or is configured **inside the customer data center**. The GreenNode
side (VPC, VPN Site-to-Site, routes, gateway, connector) is in [`../greennode/README.md`](../greennode/README.md).
Do the GreenNode steps up to the VPN creation first, because you need the VPN public IP and the pre-shared key.

```
MCP Gateway (AgentBase VPC 172.30.0.0/16)
   -> customer VPC (10.20.0.0/16, example)  -> GreenNode VPN gateway
   =========== IPsec tunnel (IKEv2) ===========
   -> [this folder] IPsec peer (strongSwan or your firewall)  -> firewall  -> MCP server :8443 / :8080
```

| Path | Purpose |
|---|---|
| [`strongswan/`](strongswan/README.md) | IPsec peer on Linux: `swanctl.conf`, secrets template, install and verify |
| [`firewall/nftables.conf`](firewall/nftables.conf) | Example ruleset: IKE / NAT-T / ESP from the GreenNode VPN IP, MCP port only from `172.30.0.0/16` and the VPC CIDR |
| [`docker-compose.yml`](docker-compose.yml), [`Caddyfile`](Caddyfile), [`.env.example`](.env.example) | Run the MCP server bound to an internal IP, with an optional Caddy TLS profile |
| [`check_connectivity.sh`](check_connectivity.sh) | TCP, `/health` and authenticated `tools/list` check, run from a host in the customer VPC |

## Checklist

1. **CIDR plan.** The on-prem CIDR does not overlap the customer VPC CIDR or `172.30.0.0/16`.
2. **Tunnel.** Bring up the IPsec peer ([`strongswan/README.md`](strongswan/README.md)); `swanctl --list-sas` shows `ESTABLISHED` and `INSTALLED`.
3. **Routing back.** The MCP host (or the data-center router) has routes for **both** the customer VPC CIDR and
   `172.30.0.0/16` pointing at the IPsec peer. Without the `172.30.0.0/16` route, requests from the gateway arrive but
   the replies never return.
4. **Firewall.** Allow IKE (UDP 500), NAT-T (UDP 4500) and ESP only from the GreenNode VPN public IP, and the MCP port
   only from `172.30.0.0/16` and the customer VPC CIDR ([`firewall/nftables.conf`](firewall/nftables.conf)). Whether the
   gateway source address is kept or source-NATed inside the VPC is unconfirmed: verify with GreenNode and open exactly that range.
5. **MCP server.**
   ```bash
   cp .env.example .env && chmod 600 .env          # MCP_API_KEYS (openssl rand -hex 32), bind addresses
   docker compose up -d --build                    # plain HTTP :8080, or
   docker compose --profile tls up -d --build      # Caddy TLS :8443 (set MCP_BIND_ADDR=127.0.0.1 in .env first)
   docker compose ps                               # mcp: healthy
   docker compose logs mcp                         # startup: "MCP_API_KEYS entry #1 ..." means the key was rejected
   ```
   The example `.env` ships a placeholder key that the server rejects: until you replace it `/mcp` answers `503`
   (`/health` stays `ok`, it only reports that the process is alive). With the TLS profile also set
   `TRUST_FORWARDED_FOR=true` in `.env`, so the audit log shows the caller address that Caddy saw; the compose file
   restricts that to requests coming from Caddy, so port 8080 cannot be used to forge the address.
   The connector URL documented by GreenNode is an HTTPS URL, so prefer the TLS profile. The certificate must be issued
   by a CA the gateway trusts. How to supply a custom CA to a connector is not in the public docs: verify with GreenNode.
6. **Check from the customer VPC** (a vServer in the VPC, before creating the connector):
   ```bash
   MCP_API_KEY=<key> ./check_connectivity.sh 192.168.10.20 8080 http
   MCP_API_KEY=<key> INSECURE=1 ./check_connectivity.sh 192.168.10.20 8443 https    # internal CA
   ```
   `PASS (3/3)` proves the VPC to data-center path. The gateway path (source `172.30.0.0/16`) additionally depends on
   the Route CIDRs of the gateway, the return route and the firewall rule above.

> Docker on the same host as strongSwan: published ports are DNAT-ed, and replies must still match the IPsec
> policy after NAT. If tunnel traffic to the container misbehaves, run the MCP server natively or on a separate
> host (the lab script uses host networking for this reason).

## Other gateways (pfSense, FortiGate, Palo Alto)

Any IKEv2 / IPsec route-based or policy-based peer works. Map the same parameters that
[`strongswan/swanctl.conf`](strongswan/swanctl.conf) uses; the GreenNode docs demonstrate pfSense end to end
(see "Demo Site-to-Site VPN" in the GreenNode VPN docs).

| Parameter | Value in this sample | Source in the GreenNode docs |
|---|---|---|
| IKE version | IKEv2 | Demo page, phase 1 |
| Peer (remote gateway) | GreenNode VPN public IP | VPN detail page |
| Authentication | Pre-shared key, identical on both sides | Create VPN, "Pre-shared Key" |
| Phase 1 encryption | AES-256-GCM, 128-bit ICV (`aes256gcm128`) | Supported IPsec configuration (default AEAD); demo (AES256-GCM, key length 128) |
| Phase 1 hash / PRF | SHA-256 (`sha2_256`) | Supported IPsec configuration (default); demo (hash 256) |
| Phase 1 DH group | 3072-bit (`modp3072`, group 15); 2048-bit (`modp2048`, group 14) as fallback | Demo uses 3072; the keyword table lists 2048 as default: use what the VPN detail page shows |
| Phase 1 lifetime | 4 hours | Demo (the page writes `144000`, which is a typo for 14400 seconds: verify with GreenNode) |
| Phase 2 encryption / hash | AES-256-GCM, or AES-256 with SHA-256 | Supported IPsec configuration; demo uses AES256 + SHA256 |
| Phase 2 lifetime | 16 hours | Demo (57600 seconds) |
| Local network (your side) | On-prem LAN CIDR = **Remote Private CIDR** entered in the console | Create VPN |
| Remote network (GreenNode side) | Customer VPC CIDR (and `172.30.0.0/16`, see the note in `swanctl.conf`) | Create VPN; Interconnect docs; verify the `172.30.0.0/16` handling with GreenNode |
| Dead peer detection | 30 s interval, restart on failure | Not specified in the docs |
| NAT traversal | UDP 4500 allowed | Not specified in the docs: verify with GreenNode |

Avoid the algorithms the docs flag as weak (`md5`, `sha`, `modp1024`, `modp1536`, `modp768`).

| Device | Where to enter the values |
|---|---|
| **pfSense** | VPN > IPsec: Phase 1 (IKEv2, AES256-GCM key length 128, SHA256, DH 3072) and Phase 2 (local = LAN subnet, remote = VPC CIDR), then add a firewall rule on the IPsec tab for TCP 8443 / 8080 |
| **FortiGate** | VPN > IPsec Wizard / Tunnels: custom tunnel, IKEv2, remote gateway = GreenNode VPN IP, Phase 1 / Phase 2 proposals as above; add static routes and a firewall policy IPsec to LAN |
| **Palo Alto** | Network > IKE Crypto / IPsec Crypto profiles, IKE Gateway, IPsec Tunnel with proxy IDs (local = on-prem CIDR, remote = VPC CIDR and `172.30.0.0/16`); security policy and a route for both remote ranges |
