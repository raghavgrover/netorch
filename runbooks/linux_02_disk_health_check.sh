#!/bin/bash

# RUNBOOK: Linux — Disk and Filesystem Health Check
# Description : Checks disk usage, inode exhaustion, mount points,
#               filesystem errors, LVM state, and SMART data if available.
#               Alerts on filesystems above 80% capacity.
# Target      : Linux servers (RHEL 9, CentOS, Ubuntu)
# Run via     : netorch runbook execution (SSH, bash)
# Author      : netorch runbook
# =============================================================================

set -uo pipefail

WARN_THRESHOLD=80   # Alert if filesystem usage >= this %

DIVIDER="============================================================"
header() { echo ""; echo "[SECTION] $1"; echo "------------------------------------------------------------"; }

echo "$DIVIDER"
echo " Linux Disk and Filesystem Health Check"
echo " Host      : $(hostname -f)"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo " Warn at   : ${WARN_THRESHOLD}%"
echo "$DIVIDER"

# ── 1. Disk Usage ─────────────────────────────────────────────────────────────
header "Filesystem Usage (df -hT)"
df -hT

# ── 2. Filesystems Over Threshold ────────────────────────────────────────────
header "Filesystems OVER ${WARN_THRESHOLD}% (ACTION REQUIRED)"
df -hP | awk -v threshold="$WARN_THRESHOLD" 'NR>1 {
    gsub(/%/,"",$5)
    if ($5+0 >= threshold+0) print "[ALERT] "$0
}'

# ── 3. Inode Usage ────────────────────────────────────────────────────────────
header "Inode Usage (df -i)"
df -i
echo ""
echo "Inodes OVER ${WARN_THRESHOLD}%:"
df -iP | awk -v threshold="$WARN_THRESHOLD" 'NR>1 {
    gsub(/%/,"",$5)
    if ($5 ~ /^[0-9]+$/ && $5+0 >= threshold+0) print "[ALERT inode] "$0
}'

# ── 4. Mount Points ───────────────────────────────────────────────────────────
header "Active Mount Points"
mount | column -t

# ── 5. fstab Entries ─────────────────────────────────────────────────────────
header "/etc/fstab"
cat /etc/fstab | grep -v '^#' | grep -v '^$'

# ── 6. Block Devices ─────────────────────────────────────────────────────────
header "Block Device Layout (lsblk)"
lsblk -o NAME,MAJ:MIN,RM,SIZE,RO,TYPE,MOUNTPOINT

# ── 7. LVM State ─────────────────────────────────────────────────────────────
header "LVM Physical Volumes / Volume Groups / Logical Volumes"
if command -v pvs &>/dev/null; then
    echo "--- Physical Volumes ---"
    pvs 2>/dev/null
    echo "--- Volume Groups ---"
    vgs 2>/dev/null
    echo "--- Logical Volumes ---"
    lvs 2>/dev/null
else
    echo "(LVM tools not installed)"
fi

# ── 8. RAID / mdadm ───────────────────────────────────────────────────────────
header "Software RAID Status (mdadm)"
if [ -f /proc/mdstat ]; then
    cat /proc/mdstat
    mdadm --detail --scan 2>/dev/null || true
else
    echo "(No software RAID detected)"
fi

# ── 9. SMART Data ─────────────────────────────────────────────────────────────
header "Disk SMART Health (smartmontools)"
if command -v smartctl &>/dev/null; then
    for disk in $(lsblk -dno NAME | grep -E '^(sd|nvme|hd)'); do
        echo "--- /dev/$disk ---"
        smartctl -H /dev/"$disk" 2>/dev/null | grep -E "(SMART overall|result)"
    done
else
    echo "(smartmontools not installed — install: yum install smartmontools)"
fi

# ── 10. Largest Directories ───────────────────────────────────────────────────
header "Top 20 Largest Directories under /"
du -xh --max-depth=3 / 2>/dev/null | sort -rh | head -20

# ── 11. Large Files ───────────────────────────────────────────────────────────
header "Top 20 Largest Files (>100MB)"
find / -xdev -type f -size +100M 2>/dev/null | \
    xargs ls -lh 2>/dev/null | sort -k5 -rh | head -20

# ── 12. Journal Disk Usage ────────────────────────────────────────────────────
header "Systemd Journal Disk Usage"
journalctl --disk-usage 2>/dev/null || true

echo ""
echo "$DIVIDER"
echo " Disk Health Check Complete"
echo "$DIVIDER"