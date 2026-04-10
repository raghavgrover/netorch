#!/bin/bash
# =============================================================================
# git-sync.sh — Commit and push netorch changes to GitHub
#
# Usage:
#   bash /opt/netorch/scripts/git-sync.sh
#   bash /opt/netorch/scripts/git-sync.sh "your custom commit message"
#
# First-time setup: run  bash /opt/netorch/scripts/git-sync.sh --setup
# =============================================================================
set -euo pipefail

REPO_DIR="/opt/netorch"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

info()  { echo -e "${GREEN}[git-sync]${NC} $*"; }
warn()  { echo -e "${YELLOW}[git-sync]${NC} $*"; }
error() { echo -e "${RED}[git-sync]${NC} $*" >&2; exit 1; }

# --setup flag: one-time GitHub wiring
if [[ "${1:-}" == "--setup" ]]; then
    echo ""
    echo "=== netorch GitHub one-time setup ==="
    echo ""
    read -rp "GitHub remote URL (e.g. https://github.com/yourname/netorch.git): " REMOTE_URL
    read -rp "Your name for git commits: " GIT_NAME
    read -rp "Your email for git commits: " GIT_EMAIL

    cd "$REPO_DIR"

    git init
    git config user.name  "$GIT_NAME"
    git config user.email "$GIT_EMAIL"

    # Copy .gitignore into place if not already there
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ ! -f "$REPO_DIR/.gitignore" ]]; then
        cp "$SCRIPT_DIR/../.gitignore" "$REPO_DIR/.gitignore" 2>/dev/null || \
            warn ".gitignore not found alongside script — you may need to add it manually."
    fi

    git remote add origin "$REMOTE_URL" 2>/dev/null || \
        git remote set-url origin "$REMOTE_URL"

    git add .
    git commit -m "Initial commit: netorch project skeleton"
    git branch -M main
    git push -u origin main

    echo ""
    info "Setup complete. From now on, just run:  bash scripts/git-sync.sh"
    echo ""
    exit 0
fi

# --- Regular sync ---
cd "$REPO_DIR"

# Make sure we're inside a git repo
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || \
    error "Not a git repo. Run this first:  bash scripts/git-sync.sh --setup"

# Check for any changes at all
if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
    info "Nothing to commit — working tree is clean."
    exit 0
fi

# Show what's changing
echo ""
info "Changes to be committed:"
git status --short
echo ""

# Build commit message
if [[ -n "${1:-}" ]]; then
    MSG="$1"
else
    # Auto-generate a message from changed files
    CHANGED=$(git diff --name-only; git ls-files --others --exclude-standard | head -5)
    FILE_LIST=$(echo "$CHANGED" | tr '\n' ' ' | sed 's/ $//')
    MSG="update: ${FILE_LIST}"
fi

TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
FULL_MSG="${MSG} [${TIMESTAMP}]"

git add .
git commit -m "$FULL_MSG"
git push origin main

echo ""
info "Pushed: \"${FULL_MSG}\""
echo ""
