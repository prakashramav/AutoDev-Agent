#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_sandbox_network.sh
#
# Creates the isolated Docker network used by sandbox containers and applies
# iptables rules to enforce an egress allowlist.
#
# Allowed outbound destinations (sandbox containers may ONLY reach):
#   • api.github.com, github.com, objects.githubusercontent.com  (GitHub)
#   • pypi.org, files.pythonhosted.org                           (pip)
#   • registry.npmjs.org                                          (npm)
#   • proxy.golang.org, sum.golang.org                           (Go modules)
#   • crates.io, static.crates.io                                 (Cargo)
#
# All other outbound traffic from sandbox_net is DROPPED.
#
# Usage:
#   sudo bash scripts/setup_sandbox_network.sh
#
# Run once on the Docker host before starting docker-compose.
# Idempotent — safe to re-run.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

NETWORK_NAME="${SANDBOX_NETWORK:-sandbox_net}"
SUBNET="172.30.0.0/24"
CHAIN="AUTODEV_SANDBOX"

echo "==> Setting up Docker network: $NETWORK_NAME ($SUBNET)"

# ── 1. Create the Docker network if it doesn't exist ─────────────────────────
if ! docker network inspect "$NETWORK_NAME" &>/dev/null; then
    docker network create \
        --driver bridge \
        --subnet "$SUBNET" \
        --opt "com.docker.network.bridge.name=br-autodev-sbx" \
        "$NETWORK_NAME"
    echo "    Created Docker network: $NETWORK_NAME"
else
    echo "    Docker network '$NETWORK_NAME' already exists — skipping create"
fi

# ── 2. Get the bridge interface name ─────────────────────────────────────────
BRIDGE_IF=$(docker network inspect "$NETWORK_NAME" \
    --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null \
    || echo "br-autodev-sbx")

echo "==> Bridge interface: $BRIDGE_IF"

# ── 3. Set up iptables allowlist chain ───────────────────────────────────────
# Flush and recreate the custom chain so this script is idempotent.
if iptables -L "$CHAIN" &>/dev/null 2>&1; then
    iptables -F "$CHAIN"
    echo "    Flushed existing $CHAIN chain"
else
    iptables -N "$CHAIN"
    echo "    Created iptables chain: $CHAIN"
fi

# Hook into FORWARD chain if not already done
if ! iptables -C FORWARD -i "$BRIDGE_IF" -j "$CHAIN" 2>/dev/null; then
    iptables -I FORWARD -i "$BRIDGE_IF" -j "$CHAIN"
    echo "    Hooked $CHAIN into FORWARD chain for $BRIDGE_IF"
fi

# ── 4. Allow established/related connections (return traffic) ─────────────────
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# ── 5. Allow intra-network traffic (container-to-container) ──────────────────
iptables -A "$CHAIN" -s "$SUBNET" -d "$SUBNET" -j ACCEPT

# ── 6. Resolve and allowlist egress destinations ─────────────────────────────
allowlist_hosts=(
    "api.github.com"
    "github.com"
    "objects.githubusercontent.com"
    "pypi.org"
    "files.pythonhosted.org"
    "registry.npmjs.org"
    "proxy.golang.org"
    "sum.golang.org"
    "crates.io"
    "static.crates.io"
)

echo "==> Allowlisting egress destinations..."
for host in "${allowlist_hosts[@]}"; do
    # Resolve all IPs for the hostname
    ips=$(dig +short "$host" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)
    if [[ -z "$ips" ]]; then
        echo "    WARNING: Could not resolve $host — skipping"
        continue
    fi
    for ip in $ips; do
        iptables -A "$CHAIN" -d "$ip" -j ACCEPT
        echo "    ALLOW $host -> $ip"
    done
done

# Allow DNS (needed for the above hosts to actually resolve inside containers)
iptables -A "$CHAIN" -p udp --dport 53 -j ACCEPT
iptables -A "$CHAIN" -p tcp --dport 53 -j ACCEPT

# ── 7. Drop everything else ───────────────────────────────────────────────────
iptables -A "$CHAIN" -j DROP
echo "==> DROP all other sandbox egress"

echo ""
echo "✅ Sandbox network '$NETWORK_NAME' configured."
echo "   Containers on this network can ONLY reach: ${allowlist_hosts[*]}"
echo ""
echo "NOTE: iptables rules are not persistent across reboots."
echo "      Install iptables-persistent or add this script to rc.local / systemd."
