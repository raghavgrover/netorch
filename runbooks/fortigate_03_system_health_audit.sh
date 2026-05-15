#!/bin/bash
# RUNBOOK: FortiGate — System Health & Resource Audit
# Description : Collects CPU, memory, disk, HA status, hardware sensors,
#               interface stats, and log disk usage. Useful for capacity
#               planning and detecting degraded appliance state.
# Target      : FortiGate (FortiOS) — CLI via SSH
# Run via     : netorch runbook execution (SSH, exec mode)
# Author      : netorch runbook
# =============================================================================

echo "============================================================"
echo " FortiGate System Health and Resource Audit"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "============================================================"

# ── 1. System Status ──────────────────────────────────────────────────────────
echo ""
echo "[SECTION] System Status"
get system status

# ── 2. Hardware Performance ───────────────────────────────────────────────────
echo ""
echo "[SECTION] CPU and Memory Usage"
get system performance status

# ── 3. Top Processes ─────────────────────────────────────────────────────────
echo ""
echo "[SECTION] Top Processes by CPU"
diagnose sys top 3 20

# ── 4. Memory Detail ─────────────────────────────────────────────────────────
echo ""
echo "[SECTION] Memory Utilisation Detail"
diagnose hardware sysinfo memory

# ── 5. Disk Usage ─────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] Disk Partitions and Usage"
diagnose sys flash list
diagnose disk usage

# ── 6. CPU Cores ─────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] CPU Info"
diagnose hardware sysinfo cpu

# ── 7. HA Status ──────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] High Availability Status"
get system ha status
diagnose sys ha status

# ── 8. HA Checksums (config sync) ────────────────────────────────────────────
echo ""
echo "[SECTION] HA Configuration Sync Checksums"
diagnose sys ha checksum show

# ── 9. Interface Statistics ───────────────────────────────────────────────────
echo ""
echo "[SECTION] Interface Statistics"
diagnose hardware deviceinfo nic

# ── 10. Environmental Sensors ────────────────────────────────────────────────
echo ""
echo "[SECTION] Hardware Sensors (temperature / fan / power)"
diagnose hardware sysinfo conserve
diagnose hardware sysinfo fanspeed

# ── 11. Log Usage ─────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] Log Disk Usage"
diagnose log test
execute log display

# ── 12. Firmware / License ───────────────────────────────────────────────────
echo ""
echo "[SECTION] Firmware Version and License Status"
get system status | grep -E "(Version|License|Serial)"
diagnose debug vm-print-license

# ── 13. NTP ───────────────────────────────────────────────────────────────────
echo ""
echo "[SECTION] NTP Sync Status"
diagnose sys ntp status

echo ""
echo "============================================================"
echo " System Health Audit Complete"
echo "============================================================"