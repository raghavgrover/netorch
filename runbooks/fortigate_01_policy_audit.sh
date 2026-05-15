#!/bin/bash

# RUNBOOK: FortiGate — Firewall Policy Audit
# Description : Enumerates all firewall policies, their hit counters,
#               disabled policies, and any policy with "all" source/
#               destination that may be overly permissive.
# Target      : FortiGate (FortiOS) — CLI via SSH
# Run via     : netorch runbook execution (SSH, exec mode)
# Author      : netorch runbook
#
# NOTE: Commands use FortiOS CLI syntax. Run from the global or target VDOM.
# =============================================================================

echo "============================================================"
echo " FortiGate Firewall Policy Audit"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "============================================================"

# ── 1. System Info ────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] System Information"
get system status

# ── 2. VDOM List ──────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] VDOM Configuration"
config global
    show system vdom
end

# ── 3. All Firewall Policies ──────────────────────────────────────────────────
echo ""
echo "[SECTION] Firewall Policy List (brief)"
config firewall policy
    show

# ── 4. Policy Hit Counts ─────────────────────────────────────────────────────
echo ""
echo "[SECTION] Policy Hit Counters"
diagnose firewall iprope show 00100004

# ── 5. Disabled Policies ─────────────────────────────────────────────────────
echo ""
echo "[SECTION] Disabled Policies"
config firewall policy
    show | grep -A5 "set status disable"

# ── 6. Policies with ANY source or destination ───────────────────────────────
echo ""
echo "[SECTION] Policies with 'all' Source or Destination (review for over-permission)"
config firewall policy
    show | grep -B5 -A5 "\"all\""

# ── 7. NAT Policies ───────────────────────────────────────────────────────────
echo ""
echo "[SECTION] NAT / VIP Policies"
config firewall vip
    show

# ── 8. Central SNAT Table ────────────────────────────────────────────────────
echo ""
echo "[SECTION] Central SNAT Table"
config firewall central-snat-map
    show

# ── 9. Security Profile Groups ────────────────────────────────────────────────
echo ""
echo "[SECTION] Security Profile Groups Applied to Policies"
config firewall profile-group
    show

# ── 10. Sessions Table Summary ───────────────────────────────────────────────
echo ""
echo "[SECTION] Current Session Table Summary"
diagnose sys session stat
diagnose sys session full-stat

echo ""
echo "============================================================"
echo " Firewall Policy Audit Complete"
echo "============================================================"