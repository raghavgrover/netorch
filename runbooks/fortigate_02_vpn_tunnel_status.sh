#!/bin/bash

# RUNBOOK: FortiGate — VPN Tunnel Status Check (IPsec & SSL-VPN)
# Description : Audits all IPsec and SSL-VPN tunnels — phase1/phase2
#               negotiation state, bytes transferred, and any tunnels
#               that are down or flapping.
# Target      : FortiGate (FortiOS) — CLI via SSH
# Run via     : netorch runbook execution (SSH, exec mode)
# Author      : netorch runbook
# =============================================================================

echo "============================================================"
echo " FortiGate VPN Tunnel Status Check"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "============================================================"

# ── 1. System Info ────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] System Information"
get system status

# ── 2. IPsec Phase1 Tunnels ───────────────────────────────────────────────────
echo ""
echo "[SECTION] IPsec Phase1 — All Gateways"
get vpn ipsec tunnel summary

# ── 3. IPsec Phase1 Detailed Status ──────────────────────────────────────────
echo ""
echo "[SECTION] IPsec Phase1 Detailed (UP/DOWN state)"
diagnose vpn ike gateway list

# ── 4. IPsec Phase2 SAs ───────────────────────────────────────────────────────
echo ""
echo "[SECTION] IPsec Phase2 — Security Associations"
diagnose vpn tunnel list

# ── 5. Tunnels with 0 bytes (potentially dead) ───────────────────────────────
echo ""
echo "[SECTION] IPsec Phase2 Counters (bytes in/out — look for zeros)"
diagnose vpn tunnel list | grep -E "(name=|bytes_in|bytes_out|state=)"

# ── 6. IKE Error Logs ────────────────────────────────────────────────────────
echo ""
echo "[SECTION] IKE Negotiation Errors (recent)"
diagnose debug application ike -1
diagnose debug reset

# ── 7. SSL-VPN Status ────────────────────────────────────────────────────────
echo ""
echo "[SECTION] SSL-VPN Active Sessions"
get vpn ssl monitor

# ── 8. SSL-VPN Tunnel Configuration ──────────────────────────────────────────
echo ""
echo "[SECTION] SSL-VPN Settings"
config vpn ssl settings
    show

# ── 9. SSL-VPN User Sessions ─────────────────────────────────────────────────
echo ""
echo "[SECTION] SSL-VPN Connected Users"
diagnose vpn ssl list

# ── 10. Routing over VPN Tunnels ─────────────────────────────────────────────
echo ""
echo "[SECTION] Routes via VPN Interfaces"
get router info routing-table all | grep -E "(tun|vpn|ipsec)"

# ── 11. VPN Phase1 Configuration Snapshot ────────────────────────────────────
echo ""
echo "[SECTION] IPsec Phase1 Configuration"
config vpn ipsec phase1-interface
    show

echo ""
echo "============================================================"
echo " VPN Tunnel Status Check Complete"
echo "============================================================"