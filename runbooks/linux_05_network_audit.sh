#!/bin/bash
# RUNBOOK: Linux — Network Configuration and Connectivity Audit
# Description : Audits IP addressing, routing table, DNS resolution,
#               network interfaces, bonding/teaming state, firewall rules,
#               and tests connectivity to key infrastructure targets.
# Target      : Linux servers (RHEL 9, CentOS, Ubuntu)
# Run via     : netorch runbook execution (SSH, bash)
# Author      : netorch runbook
#
# EDIT THESE TARGETS to match your environment:
DNS_SERVERS=("8.8.8.8" "1.1.1.1")
PING_TARGETS=("10.0.0.1" "10.0.0.10")   # Default gateway, NTP server
TEST_FQDN="google.com"
# =============================================================================

set -uo pipefail

DIVIDER="============================================================"
header() { echo ""; echo "[SECTION] $1"; echo "------------------------------------------------------------"; }

echo "$DIVIDER"
echo " Linux Network Configuration and Connectivity Audit"
echo " Host      : $(hostname -f)"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "$DIVIDER"

# ── 1. Hostname and DNS ───────────────────────────────────────────────────────
header "Hostname and DNS Configuration"
echo "Short hostname : $(hostname)"
echo "FQDN           : $(hostname -f 2>/dev/null || echo '(not set)')"
echo "DNS domain     : $(dnsdomainname 2>/dev/null || echo '(not set)')"
echo ""
echo "/etc/resolv.conf:"
cat /etc/resolv.conf
echo ""
echo "/etc/hosts:"
cat /etc/hosts | grep -v '^#' | grep -v '^$'

# ── 2. Network Interfaces ─────────────────────────────────────────────────────
header "Network Interfaces and IP Addresses"
ip addr show

# ── 3. Interface Link State ───────────────────────────────────────────────────
header "Interface Link State"
ip link show

# ── 4. Routing Table ─────────────────────────────────────────────────────────
header "IP Routing Table"
ip route show table all | grep -v "fe80\|ff00"

# ── 5. Default Gateway ────────────────────────────────────────────────────────
header "Default Gateway"
ip route show default

# ── 6. ARP / Neighbour Table ──────────────────────────────────────────────────
header "ARP / Neighbour Cache"
ip neigh show | sort

# ── 7. Bonding / Teaming ─────────────────────────────────────────────────────
header "Network Bonding / Teaming Status"
if [ -d /proc/net/bonding ]; then
    for bond in /proc/net/bonding/*; do
        echo "--- $bond ---"
        cat "$bond"
    done
else
    echo "(No bonding interfaces detected)"
fi

if command -v teamdctl &>/dev/null; then
    echo "--- Teaming ---"
    for team in $(teamdctl list 2>/dev/null); do
        teamdctl "$team" state
    done
fi

# ── 8. NIC Statistics ─────────────────────────────────────────────────────────
header "NIC Error Statistics"
for nic in $(ip link show | awk -F': ' '/^[0-9]+:/{print $2}' | grep -v lo); do
    echo "--- $nic ---"
    ip -s link show "$nic" 2>/dev/null | grep -A4 "RX:\|TX:"
done

# ── 9. Firewall / iptables ────────────────────────────────────────────────────
header "Firewall Rules"
if command -v firewall-cmd &>/dev/null; then
    echo "--- firewalld ---"
    firewall-cmd --list-all 2>/dev/null
    firewall-cmd --list-all-zones 2>/dev/null
elif command -v iptables &>/dev/null; then
    echo "--- iptables ---"
    iptables -L -n -v --line-numbers 2>/dev/null
    echo "--- ip6tables ---"
    ip6tables -L -n -v --line-numbers 2>/dev/null
fi

# ── 10. SELinux Status ────────────────────────────────────────────────────────
header "SELinux Status"
if command -v getenforce &>/dev/null; then
    getenforce
    sestatus 2>/dev/null || true
else
    echo "(SELinux not installed or not applicable)"
fi

# ── 11. Connectivity Tests ────────────────────────────────────────────────────
header "Connectivity Tests"
for target in "${PING_TARGETS[@]}"; do
    if ping -c 3 -W 2 "$target" &>/dev/null; then
        echo "[OK]    Ping to $target — reachable"
    else
        echo "[FAIL]  Ping to $target — UNREACHABLE"
    fi
done

# ── 12. DNS Resolution Test ───────────────────────────────────────────────────
header "DNS Resolution Test"
for dns in "${DNS_SERVERS[@]}"; do
    result=$(dig +short "$TEST_FQDN" @"$dns" 2>/dev/null | head -1)
    if [ -n "$result" ]; then
        echo "[OK]    DNS $dns resolves $TEST_FQDN -> $result"
    else
        echo "[FAIL]  DNS $dns FAILED to resolve $TEST_FQDN"
    fi
done

# ── 13. Traceroute to Default Gateway ────────────────────────────────────────
header "Traceroute to Default Gateway (first 5 hops)"
GW=$(ip route show default | awk '/default/{print $3}' | head -1)
if [ -n "$GW" ]; then
    traceroute -m 5 -w 2 "$GW" 2>/dev/null || true
fi

echo ""
echo "$DIVIDER"
echo " Network Audit Complete"
echo "$DIVIDER"