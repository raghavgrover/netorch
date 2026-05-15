#!/bin/bash
# RUNBOOK: Linux — Service and Process Audit
# Description : Lists all enabled/disabled/failed systemd services,
#               running processes, zombie processes, high-CPU/memory
#               consumers, and network-facing daemons.
# Target      : Linux servers (RHEL 9, CentOS, Ubuntu)
# Run via     : netorch runbook execution (SSH, bash)
# Author      : netorch runbook
# =============================================================================

set -uo pipefail

DIVIDER="============================================================"
header() { echo ""; echo "[SECTION] $1"; echo "------------------------------------------------------------"; }

echo "$DIVIDER"
echo " Linux Service and Process Audit"
echo " Host      : $(hostname -f)"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "$DIVIDER"

# ── 1. System Uptime ──────────────────────────────────────────────────────────
header "System Uptime and Load"
uptime
echo "Load averages (1m 5m 15m):"
cat /proc/loadavg

# ── 2. All Enabled Services ───────────────────────────────────────────────────
header "Enabled Systemd Services"
systemctl list-unit-files --type=service --state=enabled 2>/dev/null

# ── 3. Failed Services ────────────────────────────────────────────────────────
header "Failed Systemd Services (ACTION REQUIRED)"
systemctl --failed --type=service 2>/dev/null

# ── 4. All Running Services ───────────────────────────────────────────────────
header "Currently Running Services"
systemctl list-units --type=service --state=running 2>/dev/null

# ── 5. Recently Failed / Restarted ────────────────────────────────────────────
header "Recently Restarted or Crashed Units (last 24h)"
journalctl -p err..alert --since "24 hours ago" --no-pager -n 50 2>/dev/null || true

# ── 6. Top CPU Consumers ─────────────────────────────────────────────────────
header "Top 20 Processes by CPU"
ps aux --sort=-%cpu | head -21

# ── 7. Top Memory Consumers ───────────────────────────────────────────────────
header "Top 20 Processes by Memory"
ps aux --sort=-%mem | head -21

# ── 8. Zombie Processes ───────────────────────────────────────────────────────
header "Zombie Processes"
ZOMBIES=$(ps aux | awk '$8 == "Z" {print}')
if [ -z "$ZOMBIES" ]; then
    echo "No zombie processes found."
else
    echo "[ALERT] Zombie processes detected:"
    echo "$ZOMBIES"
fi

# ── 9. Processes Listening on Network ────────────────────────────────────────
header "Processes Listening on Network Ports"
ss -tulnp 2>/dev/null || netstat -tulnp 2>/dev/null

# ── 10. Unexpected Network Connections ───────────────────────────────────────
header "All Established Network Connections"
ss -tnp state established 2>/dev/null | head -40

# ── 11. Process Tree ──────────────────────────────────────────────────────────
header "Process Tree (pstree)"
if command -v pstree &>/dev/null; then
    pstree -p | head -80
else
    ps -ejH | head -80
fi

# ── 12. Systemd Timers ────────────────────────────────────────────────────────
header "Active Systemd Timers"
systemctl list-timers --all 2>/dev/null

# ── 13. Services Not Managed by Systemd ──────────────────────────────────────
header "Init.d Scripts (legacy services)"
ls /etc/init.d/ 2>/dev/null || echo "(none found)"

echo ""
echo "$DIVIDER"
echo " Service and Process Audit Complete"
echo "$DIVIDER"