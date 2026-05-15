#!/bin/bash
# RUNBOOK: Linux — OS Patch and Package Audit
# Description : Reports installed kernel version, available updates,
#               security-specific updates, installed package inventory,
#               and recently installed/updated/removed packages.
#               Supports RHEL/CentOS (yum/dnf) and Ubuntu/Debian (apt).
# Target      : Linux servers (RHEL 9, CentOS, Ubuntu, Debian)
# Run via     : netorch runbook execution (SSH, bash)
# Author      : netorch runbook
# =============================================================================

set -uo pipefail

DIVIDER="============================================================"
header() { echo ""; echo "[SECTION] $1"; echo "------------------------------------------------------------"; }

echo "$DIVIDER"
echo " Linux OS Patch and Package Audit"
echo " Host      : $(hostname -f)"
echo " Timestamp : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "$DIVIDER"

# Detect package manager
if command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
elif command -v apt &>/dev/null; then
    PKG_MGR="apt"
else
    PKG_MGR="unknown"
fi
echo "Package Manager : $PKG_MGR"

# ── 1. Kernel Version ─────────────────────────────────────────────────────────
header "Running and Installed Kernels"
uname -r
echo "--- All installed kernels ---"
if [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
    rpm -q kernel | sort
elif [ "$PKG_MGR" = "apt" ]; then
    dpkg -l | grep linux-image | grep -v "^rc"
fi

# ── 2. OS Release ─────────────────────────────────────────────────────────────
header "OS Release Information"
cat /etc/os-release 2>/dev/null || cat /etc/redhat-release 2>/dev/null

# ── 3. Available Updates ─────────────────────────────────────────────────────
header "Available Updates (check only — no install)"
if [ "$PKG_MGR" = "dnf" ]; then
    dnf check-update 2>/dev/null; true
elif [ "$PKG_MGR" = "yum" ]; then
    yum check-update 2>/dev/null; true
elif [ "$PKG_MGR" = "apt" ]; then
    apt-get -s upgrade 2>/dev/null | grep -E "^(Inst|Remv)" | head -50
fi

# ── 4. Security-Only Updates ──────────────────────────────────────────────────
header "Security Updates Available"
if [ "$PKG_MGR" = "dnf" ]; then
    dnf updateinfo list security 2>/dev/null || true
elif [ "$PKG_MGR" = "yum" ]; then
    yum updateinfo list security 2>/dev/null || true
elif [ "$PKG_MGR" = "apt" ]; then
    apt-get -s dist-upgrade 2>/dev/null | grep "^Inst" | grep -i security | head -30
fi

# ── 5. OVAL / CVE Exposure ────────────────────────────────────────────────────
header "CVSS / CVE Information (if available)"
if [ "$PKG_MGR" = "dnf" ]; then
    dnf updateinfo list cves 2>/dev/null | head -40 || true
fi

# ── 6. Recently Installed Packages ───────────────────────────────────────────
header "Recently Installed / Updated Packages (last 30 days)"
if [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
    rpm -qa --queryformat '%{INSTALLTIME:date} %{NAME}-%{VERSION}\n' | \
        sort -r | head -40
elif [ "$PKG_MGR" = "apt" ]; then
    grep " install\| upgrade" /var/log/dpkg.log* 2>/dev/null | tail -40
fi

# ── 7. Recently Removed Packages ─────────────────────────────────────────────
header "Recently Removed Packages"
if [ "$PKG_MGR" = "dnf" ]; then
    dnf history list 2>/dev/null | head -30
elif [ "$PKG_MGR" = "apt" ]; then
    grep " remove\| purge" /var/log/dpkg.log 2>/dev/null | tail -20
fi

# ── 8. All Installed Packages (count) ────────────────────────────────────────
header "Installed Package Count"
if [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
    rpm -qa | wc -l
    echo "Top 10 largest RPMs:"
    rpm -qa --queryformat '%{SIZE} %{NAME}\n' | sort -rn | head -10
elif [ "$PKG_MGR" = "apt" ]; then
    dpkg -l | grep "^ii" | wc -l
fi

# ── 9. Package Integrity Check ────────────────────────────────────────────────
header "RPM Package Integrity Verification (changed files)"
if [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
    echo "Checking for modified system files (this may take a moment)..."
    rpm -Va --noscripts 2>/dev/null | grep -E "^S|^M|^5" | grep -v "\.pyc\|__pycache__" | head -30 || true
fi

# ── 10. AutoUpdate Configuration ─────────────────────────────────────────────
header "Automatic Update / Unattended Upgrade Status"
if systemctl is-active dnf-automatic &>/dev/null 2>&1; then
    echo "dnf-automatic: ACTIVE"
    cat /etc/dnf/automatic.conf 2>/dev/null | grep -v "^#" | grep -v "^$"
elif systemctl is-active unattended-upgrades &>/dev/null 2>&1; then
    echo "unattended-upgrades: ACTIVE"
else
    echo "No automatic update service detected as active."
fi

echo ""
echo "$DIVIDER"
echo " Patch and Package Audit Complete"
echo "$DIVIDER"