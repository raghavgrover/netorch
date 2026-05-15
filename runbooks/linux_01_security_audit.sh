#!/bin/bash

# RUNBOOK: Linux — System Security Audit
# Description : Audits SSH hardening, user accounts, sudo rights, SUID
#               binaries, open ports, firewall rules, and failed logins.
#               Designed for RHEL/CentOS/Ubuntu servers.
# Target      : Linux servers (RHEL 9, CentOS, Ubuntu)
# Run via     : netorch runbook execution (SSH, bash)
# Author      : netorch runbook
# =============================================================================

set -euo pipefail

DIVIDER="============================================================"

header() { echo ""; echo "[SECTION] $1"; echo "------------------------------------------------------------"; }

echo "$DIVIDER"
echo " Linux System Security Audit"
echo " Host      : $(hostname -f)"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo " User      : $(whoami)"
echo "$DIVIDER"

# ── 1. OS and Kernel ──────────────────────────────────────────────────────────
header "OS and Kernel Version"
uname -a
cat /etc/os-release 2>/dev/null || cat /etc/redhat-release 2>/dev/null

# ── 2. Last Reboots ───────────────────────────────────────────────────────────
header "Last 10 Reboots / Shutdowns"
last reboot | head -10

# ── 3. Local User Accounts ────────────────────────────────────────────────────
header "Local User Accounts (UID >= 1000 or UID 0)"
awk -F: '$3 >= 1000 || $3 == 0 {print $1, "UID="$3, "Shell="$7}' /etc/passwd

# ── 4. Users with Empty Passwords ────────────────────────────────────────────
header "Accounts with Empty Password Hash"
sudo awk -F: '($2 == "" || $2 == "!") {print $1}' /etc/shadow 2>/dev/null || echo "(requires root)"

# ── 5. Sudo Rights ────────────────────────────────────────────────────────────
header "Sudo Configuration (sudoers)"
sudo cat /etc/sudoers | grep -v '^#' | grep -v '^$'
ls -la /etc/sudoers.d/ 2>/dev/null

# ── 6. SSH Daemon Configuration ──────────────────────────────────────────────
header "SSH Daemon Security Settings"
grep -E "^(PermitRootLogin|PasswordAuthentication|PubkeyAuthentication|X11Forwarding|PermitEmptyPasswords|Protocol|MaxAuthTries|AllowUsers|DenyUsers|Port)" \
    /etc/ssh/sshd_config 2>/dev/null

# ── 7. Authorised Keys ────────────────────────────────────────────────────────
header "Authorized SSH Public Keys (all home dirs)"
find /root /home -name "authorized_keys" 2>/dev/null | while read f; do
    echo "File: $f"
    cat "$f"
    echo "---"
done

# ── 8. SUID / SGID Binaries ──────────────────────────────────────────────────
header "SUID / SGID Binaries (non-standard paths)"
find / -xdev \( -perm -4000 -o -perm -2000 \) -type f 2>/dev/null | sort

# ── 9. Open Ports ─────────────────────────────────────────────────────────────
header "Open Network Ports"
ss -tulnp 2>/dev/null || netstat -tulnp 2>/dev/null

# ── 10. Firewall Rules ────────────────────────────────────────────────────────
header "Firewall Rules (firewalld / iptables)"
if command -v firewall-cmd &>/dev/null; then
    firewall-cmd --list-all 2>/dev/null
else
    iptables -L -n -v 2>/dev/null
fi

# ── 11. Failed Login Attempts ─────────────────────────────────────────────────
header "Recent Failed SSH Login Attempts (last 30 lines)"
grep -i "failed password\|invalid user" /var/log/secure 2>/dev/null | tail -30 \
    || grep -i "failed password\|invalid user" /var/log/auth.log 2>/dev/null | tail -30 \
    || journalctl -u sshd --no-pager -n 50 2>/dev/null | grep -i "failed\|invalid"

# ── 12. Cron Jobs ─────────────────────────────────────────────────────────────
header "System and User Cron Jobs"
for user in $(cut -f1 -d: /etc/passwd); do
    crontab -u "$user" -l 2>/dev/null | grep -v "^#" | grep -v "^$" | \
        while read line; do echo "[$user] $line"; done
done
ls -la /etc/cron.* /var/spool/cron/ 2>/dev/null

echo ""
echo "$DIVIDER"
echo " Security Audit Complete"
echo "$DIVIDER"