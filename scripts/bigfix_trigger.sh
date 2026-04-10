#!/bin/bash
# =============================================================================
# bigfix_trigger.sh — BigFix agent integration script for netorch
#
# Usage (called by BigFix action):
#   bash bigfix_trigger.sh \
#     --mode audit|remediate \
#     --devices "host_or_group_per_line" \
#     --commands "one_command_per_line" \
#     [--remediation-commands "one_command_per_line"] \
#     [--config-file "/path/to/staged/file"]
#
# Arguments can also be passed via environment variables:
#   NETORCH_MODE, NETORCH_DEVICES, NETORCH_COMMANDS,
#   NETORCH_REMEDIATION_COMMANDS, NETORCH_CONFIG_FILE
#
# Exit codes:
#   0 — job completed or partial_failure (inspect result log)
#   1 — fatal error (API unreachable, job failed, timeout, bad args)
# =============================================================================
set -euo pipefail

echo "Received execution call $(date) with ARGS $@" >> /var/netorch/results/execution.log
# ---------------------------------------------------------------------------
# Configuration — override via environment if needed
# ---------------------------------------------------------------------------
ORCH_URL="${NETORCH_URL:-http://localhost:64321}"
ORCH_TOKEN="${NETORCH_TOKEN:-ab76f9ba6d712a6941e74eca990780a1d5d9a59d9f6f09b51506d258aacea636}"   # Must match netorch.toml auth_token
RESULTS_DIR="${NETORCH_RESULTS_DIR:-/var/netorch/results}"
POLL_INTERVAL="${NETORCH_POLL_INTERVAL:-5}"
MAX_POLLS="${NETORCH_MAX_POLLS:-120}"    # 120 × 5s = 10 min max wait

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE="${NETORCH_MODE:-}"
DEVICES_RAW="${NETORCH_DEVICES:-}"
COMMANDS_RAW="${NETORCH_COMMANDS:-}"
REMEDIATION_COMMANDS_RAW="${NETORCH_REMEDIATION_COMMANDS:-}"
CONFIG_FILE="${NETORCH_CONFIG_FILE:-}"
FILE_TRANSFERS_RAW="${NETORCH_FILE_TRANSFERS:-}"

usage() {
    cat >&2 <<EOF
Usage: $0 --mode audit|remediate --devices DEVICES --commands COMMANDS
          [--remediation-commands CMDS] [--config-file PATH]
          [--file-transfers "lpath1:rpath1:cmd1,cmd2|lpath2:rpath2:"]

  --mode                  audit or remediate (required)
  --devices               Newline-separated list of hostnames, IPs, or group names (required)
  --commands              Newline-separated list of commands to run (required)
  --remediation-commands  Newline-separated config commands (required when mode=remediate)
  --config-file           Path to a single file staged on the relay (legacy; prefer --file-transfers)
  --file-transfers        Pipe-delimited file transfer entries.
                          Each entry: local_path:remote_path:post_cmd1,post_cmd2
                          (post-transfer commands are optional; omit or leave blank)
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)                  MODE="$2";                    shift 2 ;;
        --devices)               DEVICES_RAW="$2";             shift 2 ;;
        --commands)              COMMANDS_RAW="$2";            shift 2 ;;
        --remediation-commands)  REMEDIATION_COMMANDS_RAW="$2"; shift 2 ;;
        --config-file)           CONFIG_FILE="$2";             shift 2 ;;
        --file-transfers)        FILE_TRANSFERS_RAW="$2";      shift 2 ;;
        -h|--help)               usage ;;
        *) echo "[netorch] ERROR: Unknown argument: $1" >&2; usage ;;
    esac
done

# ---------------------------------------------------------------------------
# Normalise line separators: BigFix inline substitution may deliver multiline
# values as literal \n escape sequences rather than real newlines.  Convert
# them here so the Python payload builder always sees real newlines.
# ---------------------------------------------------------------------------
DEVICES_RAW="${DEVICES_RAW//$'\\n'/$'\n'}"
COMMANDS_RAW="${COMMANDS_RAW//$'\\n'/$'\n'}"
REMEDIATION_COMMANDS_RAW="${REMEDIATION_COMMANDS_RAW//$'\\n'/$'\n'}"

# ---------------------------------------------------------------------------
# Validate required arguments
# ---------------------------------------------------------------------------
[[ -z "$MODE" ]]        && echo "[netorch] ERROR: --mode is required." >&2        && usage
[[ -z "$DEVICES_RAW" ]] && echo "[netorch] ERROR: --devices is required." >&2     && usage

if [[ "$MODE" != "audit" && "$MODE" != "remediate" ]]; then
    echo "[netorch] ERROR: --mode must be 'audit' or 'remediate', got: $MODE" >&2
    exit 1
fi

if [[ -z "$COMMANDS_RAW" && -z "$FILE_TRANSFERS_RAW" && -z "$CONFIG_FILE" ]]; then
    echo "[netorch] ERROR: at least one of --commands or --file-transfers must be provided." >&2
    exit 1
fi

if [[ "$MODE" == "remediate" && -z "$REMEDIATION_COMMANDS_RAW" && -z "$FILE_TRANSFERS_RAW" ]]; then
    echo "[netorch] ERROR: mode=remediate requires either --remediation-commands or --file-transfers." >&2
    exit 1
fi

if [[ -n "$CONFIG_FILE" && ! -f "$CONFIG_FILE" ]]; then
    echo "[netorch] ERROR: --config-file not found: $CONFIG_FILE" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Build JSON payload with python3 (handles quoting/escaping correctly)
# ---------------------------------------------------------------------------
PAYLOAD=$(MODE="$MODE" DEVICES_RAW="$DEVICES_RAW" COMMANDS_RAW="$COMMANDS_RAW" \
  REM_RAW="$REMEDIATION_COMMANDS_RAW" CONFIG_FILE="$CONFIG_FILE" \
  FILE_TRANSFERS_RAW="$FILE_TRANSFERS_RAW" \
  python3 - <<'PYEOF'
import json, os

mode               = os.environ['MODE']
devices_raw        = os.environ['DEVICES_RAW']
commands_raw       = os.environ['COMMANDS_RAW']
rem_raw            = os.environ['REM_RAW']
config_file        = os.environ['CONFIG_FILE']
file_transfers_raw = os.environ['FILE_TRANSFERS_RAW']

def parse_lines(raw):
    """Split on newlines, strip whitespace, drop empty lines."""
    return [line.strip() for line in raw.splitlines() if line.strip()]

# Build devices list: each token is either a bare IP/hostname (-> host entry)
# or a name that looks like a group (no dots/colons -> group entry).
# BigFix passes one entry per line; a token with dots/colons is treated as a
# host, anything else as a group name.
def classify(token):
    if '.' in token or ':' in token:
        return {"host": token}
    else:
        return {"group": token}

devices  = [classify(t) for t in parse_lines(devices_raw)]
commands = parse_lines(commands_raw)

payload = {
    "mode":    mode,
    "devices": devices,
}
if commands:
    payload["commands"] = commands

if rem_raw:
    payload["remediation_commands"] = parse_lines(rem_raw)

# Build file_transfers list.  Two sources:
#   1. Legacy --config-file: single local path, empty remote (operator fills later).
#   2. --file-transfers: pipe-delimited entries in format
#      local_path:remote_path:post_cmd1,post_cmd2
#      (split with maxsplit=2 so Linux paths with no extra colons are safe)
transfers = []

if config_file:
    transfers.append({
        "local_path":             config_file,
        "remote_path":            "",
        "post_transfer_commands": None,
    })

if file_transfers_raw:
    for entry in file_transfers_raw.split('|'):
        entry = entry.strip()
        if not entry:
            continue
        parts   = entry.split(':', 2)
        lp      = parts[0].strip() if len(parts) > 0 else ''
        rp      = parts[1].strip() if len(parts) > 1 else ''
        cmds_s  = parts[2].strip() if len(parts) > 2 else ''
        cmds    = [c.strip() for c in cmds_s.split(',') if c.strip()] or None
        if lp:
            transfers.append({
                "local_path":             lp,
                "remote_path":            rp,
                "post_transfer_commands": cmds,
            })

if transfers:
    payload["file_transfers"] = transfers

print(json.dumps(payload))
PYEOF
) || {
    echo "[netorch] ERROR: Failed to build JSON payload." >&2
    exit 1
}

mkdir -p "$RESULTS_DIR"

# ---------------------------------------------------------------------------
# Submit job
# ---------------------------------------------------------------------------
echo "[netorch] Submitting $MODE job ($(echo "$DEVICES_RAW" | grep -c . || true) device entries)..."

RESPONSE=$(curl -sf -X POST "$ORCH_URL/jobs" \
    -H "Authorization: Bearer $ORCH_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "$PAYLOAD") || {
    echo "[netorch] ERROR: Could not reach netorch API at $ORCH_URL" >&2
    exit 1
}

JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['job_id'])") || {
    echo "[netorch] ERROR: Unexpected API response: $RESPONSE" >&2
    exit 1
}

echo "[netorch] Job submitted: $JOB_ID"

# ---------------------------------------------------------------------------
# Poll until terminal state
# ---------------------------------------------------------------------------
POLLS=0
STATUS="queued"

while [[ "$STATUS" == "queued" || "$STATUS" == "running" ]]; do
    if [[ $POLLS -ge $MAX_POLLS ]]; then
        echo "[netorch] ERROR: Timed out after $((MAX_POLLS * POLL_INTERVAL))s waiting for job $JOB_ID" >&2
        exit 1
    fi

    sleep "$POLL_INTERVAL"
    POLLS=$((POLLS + 1))

    POLL_RESP=$(curl -sf "$ORCH_URL/jobs/$JOB_ID" \
        -H "Authorization: Bearer $ORCH_TOKEN") || {
        echo "[netorch] WARNING: Poll $POLLS failed, retrying..."
        continue
    }

    STATUS=$(echo "$POLL_RESP" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['status'])") || {
        echo "[netorch] WARNING: Could not parse poll response on attempt $POLLS, retrying..."
        STATUS="running"
        continue
    }

    SUMMARY=$(echo "$POLL_RESP" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())['summary']
print(f\"total={d['total']} success={d['success']} failed={d['failed']} in_progress={d['in_progress']}\")
") || SUMMARY="(summary unavailable)"

    echo "[netorch] Poll $POLLS — status=$STATUS $SUMMARY"
done

# ---------------------------------------------------------------------------
# Download result log
# ---------------------------------------------------------------------------
RESULT_FILE="$RESULTS_DIR/$JOB_ID.json"
if curl -sf "$ORCH_URL/logs/$JOB_ID/raw" \
        -H "Authorization: Bearer $ORCH_TOKEN" \
        -o "$RESULT_FILE"; then
    echo "[netorch] Result log saved: $RESULT_FILE"
else
    echo "[netorch] WARNING: Could not save result log for job $JOB_ID"
fi

# ---------------------------------------------------------------------------
# Exit with correct code per CLAUDE.md contract
# ---------------------------------------------------------------------------
echo "[netorch] Done — status=$STATUS"

case "$STATUS" in
    completed|partial_failure)
        exit 0
        ;;
    failed|cancelled)
        echo "[netorch] Job did not complete successfully (status=$STATUS)" >&2
        exit 1
        ;;
    *)
        echo "[netorch] ERROR: Unexpected terminal status: $STATUS" >&2
        exit 1
        ;;
esac
