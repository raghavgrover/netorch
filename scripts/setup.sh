#!/bin/bash
# =============================================================================
# setup.sh — One-shot installer for netorch v1.3.0 on RHEL 9
#
# Run as root:  sudo bash setup.sh
#
# Changes from v1.2.0
# ────────────────────
# - Creates /opt/netorch/workflows/ directory
# - Installs netorch_exec and netorch_once helpers to /usr/local/bin/
# - Bumped version references to 1.3.0
# =============================================================================
set -euo pipefail

INSTALL_DIR="/opt/netorch"
VENV_DIR="$INSTALL_DIR/venv"
SERVICE_USER="netorch"
SERVICE_NAME="netorch"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[setup]${NC} $*"; }
error() { echo -e "${RED}[setup]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash scripts/setup.sh"

# --- 1. System dependencies ---
info "[1/9] Installing system dependencies via dnf..."
dnf install -y python3 python3-pip python3-venv curl \
               policycoreutils-python-utils 2>/dev/null || \
dnf install -y python3 python3-pip curl policycoreutils-python-utils

# --- 2. Create service user ---
info "[2/9] Creating '$SERVICE_USER' system user..."
id "$SERVICE_USER" &>/dev/null || \
    useradd --system --no-create-home --shell /sbin/nologin \
            --comment "netorch service account" "$SERVICE_USER"

# --- 3. Deploy files ---
info "[3/9] Deploying files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR/"

# --- 4. Python venv ---
info "[4/9] Creating Python venv and installing dependencies..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# --- 5. Directories and inventory ---
info "[5/9] Setting up directories..."
mkdir -p "$INSTALL_DIR/logs/jobs"
mkdir -p "$INSTALL_DIR/runbooks"
mkdir -p "$INSTALL_DIR/workflows"    # ← NEW: workflow scripts directory
mkdir -p "/var/netorch/results"

if [[ ! -f "$INSTALL_DIR/inventory.ini" ]]; then
    cp "$INSTALL_DIR/inventory.ini.example" "$INSTALL_DIR/inventory.ini"
    warn "Copied inventory.ini.example → inventory.ini. Edit it with real credentials."
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "/var/netorch"
chmod 640 "$INSTALL_DIR/inventory.ini"
chmod 640 "$INSTALL_DIR/netorch.toml"
chmod 750 "$INSTALL_DIR/workflows"

# --- 6. Install workflow helper scripts --- ← NEW
info "[6/9] Installing workflow helpers to /usr/local/bin/..."

# netorch_exec — called from workflow scripts to run SSH commands via netorch
if [[ -f "$INSTALL_DIR/scripts/netorch_exec" ]]; then
    cp "$INSTALL_DIR/scripts/netorch_exec" /usr/local/bin/netorch_exec
    chmod +x /usr/local/bin/netorch_exec
    info "  Installed: /usr/local/bin/netorch_exec"
else
    warn "  scripts/netorch_exec not found — skipping. Copy it manually."
fi

# netorch_once — called from workflow scripts for once-per-job coordination
if [[ -f "$INSTALL_DIR/scripts/netorch_once" ]]; then
    cp "$INSTALL_DIR/scripts/netorch_once" /usr/local/bin/netorch_once
    chmod +x /usr/local/bin/netorch_once
    info "  Installed: /usr/local/bin/netorch_once"
else
    warn "  scripts/netorch_once not found — skipping. Copy it manually."
fi

# --- 7. SELinux ---
info "[7/9] Configuring SELinux..."
if command -v getenforce &>/dev/null && [[ "$(getenforce)" != "Disabled" ]]; then
    semanage port -l | grep -q "64321" || \
        semanage port -a -t http_port_t -p tcp 64321 2>/dev/null || \
        warn "Could not label port 64321. Run manually: semanage port -a -t http_port_t -p tcp 64321"
    semanage fcontext -a -t var_t "${INSTALL_DIR}(/.*)?" 2>/dev/null || true
    restorecon -R "$INSTALL_DIR" 2>/dev/null || true
    # Allow netorch service to execute scripts in /usr/local/bin
    restorecon -R /usr/local/bin/netorch_exec /usr/local/bin/netorch_once 2>/dev/null || true
    info "SELinux contexts applied."
else
    info "SELinux is disabled — skipping."
fi

# --- 8. Firewalld ---
info "[8/9] Opening port 64321/tcp in firewalld..."
if systemctl is-active --quiet firewalld 2>/dev/null; then
    firewall-cmd --permanent --add-port=64321/tcp
    firewall-cmd --reload
    info "firewalld: port 64321/tcp opened."
else
    warn "firewalld not running — open port 64321/tcp manually if needed."
fi

# --- 9. Systemd service ---
info "[9/9] Installing systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE
[Unit]
Description=netorch Network Configuration Orchestrator v1.3.0
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONPATH=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/python -m uvicorn main:app --host 0.0.0.0 --port 64321 --workers 1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=netorch
NoNewPrivileges=yes
ProtectSystem=full
ReadWritePaths=${INSTALL_DIR}/logs /var/netorch /tmp

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo -e "${GREEN}======================================================"
echo " netorch v1.3.0 installed on RHEL 9"
echo -e "======================================================${NC}"
echo ""
echo -e "${YELLOW}  REQUIRED before starting:${NC}"
echo ""
echo "  1. Set auth token (generate one with: openssl rand -hex 32):"
echo "     nano $INSTALL_DIR/netorch.toml"
echo "     → [server] auth_token"
echo ""
echo "  2. Add device credentials:"
echo "     nano $INSTALL_DIR/inventory.ini"
echo "     → Replace all CHANGE_ME values"
echo ""
echo "  3. Start:"
echo "     sudo systemctl start netorch"
echo "     sudo systemctl status netorch"
echo "     curl http://localhost:64321/health"
echo ""
echo "  4. Run tests (uses mock devices, no real SSH needed):"
echo "     bash $INSTALL_DIR/scripts/run_tests.sh -v"
echo ""
echo "  New in v1.3.0 — Workflow engine:"
echo "  - Place workflow scripts in: $INSTALL_DIR/workflows/"
echo "  - Helpers available at:      /usr/local/bin/netorch_exec"
echo "                                /usr/local/bin/netorch_once"
echo "  - BigFix trigger:            $INSTALL_DIR/scripts/bigfix_workflow_trigger.sh"
echo "  - API:                        GET/POST /workflows"
echo ""
echo "  Logs: journalctl -u netorch -f"
echo ""
