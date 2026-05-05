#!/usr/bin/env bash
# =============================================================================
# bigfix_workflow_trigger.sh — BigFix trigger for netorch workflow execution.
#
# Usage (from a BigFix Fixlet action script):
#   bash /opt/netorch/scripts/bigfix_workflow_trigger.sh \
#       --workflow   ise_add_nad_tacacs.sh \
#       --devices    "cisco_switches" \
#       --incident   INC12345 \
#       --param      ISE_HOSTNAME=10.0.0.5 \
#       --param      TACACS_KEY=sharedsecret \
#       --param      AAA_GROUP_NAME=ISE_GROUP \
#       --timeout    300 \
#       --workers    5
#
# Multiple --param KEY=VALUE flags are accepted.
# --devices accepts comma-separated group names or IPs, or newline-separated.
#
# The workflow must exist in /opt/netorch/workflows/ on the relay server.
#
# Exit codes: 0=success, 1=error (partial_failure also exits 1)
# =============================================================================
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
NETORCH_API="http://localhost:64321"
NETORCH_CONF="/opt/netorch/netorch.toml"
WORKFLOWS_DIR="/opt/netorch/workflows"
LOGS_BASE="/opt/netorch/logs/jobs"
POLL_INTERVAL=5      # seconds between status polls
MAX_POLL=360         # max polls before giving up (360 * 5s = 30 min)

# ─── Parse arguments ─────────────────────────────────────────────────────────
WORKFLOW_NAME=""
DEVICES_RAW=""
INCIDENT=""
TIMEOUT_PER_DEVICE=300
MAX_WORKERS=10
declare -A PARAMS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workflow)  WORKFLOW_NAME="$2";           shift 2 ;;
        --devices)   DEVICES_RAW="$2";             shift 2 ;;
        --incident)  INCIDENT="$2";                shift 2 ;;
        --timeout)   TIMEOUT_PER_DEVICE="$2";      shift 2 ;;
        --workers)   MAX_WORKERS="$2";             shift 2 ;;
        --param)
            # Accept KEY=VALUE
            KEY="${2%%=*}"
            VALUE="${2#*=}"
            PARAMS["$KEY"]="$VALUE"
            shift 2 ;;
        *) echo "[netorch-workflow] WARNING: Unknown argument: $1" >&2; shift ;;
    esac
done

# ─── Validate required args ───────────────────────────────────────────────────
if [[ -z "$WORKFLOW_NAME" ]]; then
    echo "[netorch-workflow] ERROR: --workflow is required." >&2
    exit 1
fi
if [[ -z "$DEVICES_RAW" ]]; then
    echo "[netorch-workflow] ERROR: --devices is required." >&2
    exit 1
fi
if [[ -z "$INCIDENT" ]]; then
    echo "[netorch-workflow] ERROR: --incident is required." >&2
    exit 1
fi

# ─── Resolve auth token ────────────────────────────────────────────────────────
if [[ ! -f "$NETORCH_CONF" ]]; then
    echo "[netorch-workflow] ERROR: netorch config not found at $NETORCH_CONF" >&2
    exit 1
fi
AUTH_TOKEN=$(grep -E '^\s*auth_token\s*=' "$NETORCH_CONF" \
    | head -1 | sed 's/.*=\s*"//' | sed 's/".*//')
if [[ -z "$AUTH_TOKEN" ]]; then
    echo "[netorch-workflow] ERROR: Could not parse auth_token from $NETORCH_CONF" >&2
    exit 1
fi

# ─── Verify workflow exists ───────────────────────────────────────────────────
WORKFLOW_PATH="$WORKFLOWS_DIR/$WORKFLOW_NAME"
if [[ ! -f "$WORKFLOW_PATH" ]]; then
    echo "[netorch-workflow] ERROR: Workflow not found: $WORKFLOW_PATH" >&2
    exit 1
fi
echo "[netorch-workflow] Workflow: $WORKFLOW_NAME"
echo "[netorch-workflow] Incident: $INCIDENT"

# ─── Build devices JSON array ─────────────────────────────────────────────────
# Normalise separators
DEVICES_RAW="${DEVICES_RAW//$'\\n'/$'\n'}"
DEVICES_RAW="${DEVICES_RAW//,/$'\n'}"

DEVICES_JSON=""
while IFS= read -r device; do
    device="$(echo "$device" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$device" ]] && continue
    if echo "$device" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(:[0-9]+)?$'; then
        ENTRY="{\"host\":\"$device\"}"
    else
        ENTRY="{\"group\":\"$device\"}"
    fi
    DEVICES_JSON="${DEVICES_JSON:+$DEVICES_JSON,}$ENTRY"
done <<< "$DEVICES_RAW"

if [[ -z "$DEVICES_JSON" ]]; then
    echo "[netorch-workflow] ERROR: No valid devices parsed from: $DEVICES_RAW" >&2
    exit 1
fi
echo "[netorch-workflow] Devices: $DEVICES_JSON"

# ─── Build parameters JSON object ─────────────────────────────────────────────
PARAMS_JSON=$(python3 - <<PYEOF
import json, os

params = {}
# Read params from environment (set by the loop above)
for key, value in os.environ.items():
    if key.startswith("_WFPARAM_"):
        real_key = key[len("_WFPARAM_"):]
        params[real_key] = value

print(json.dumps(params))
PYEOF
)

# Re-export params with prefix so Python can read them
for KEY in "${!PARAMS[@]}"; do
    export "_WFPARAM_${KEY}=${PARAMS[$KEY]}"
done

PARAMS_JSON=$(python3 - <<PYEOF
import json, os
params = {}
for key, value in os.environ.items():
    if key.startswith("_WFPARAM_"):
        real_key = key[len("_WFPARAM_"):]
        params[real_key] = value
print(json.dumps(params))
PYEOF
)

echo "[netorch-workflow] Parameters: $(echo "$PARAMS_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(', '.join(d.keys()) or '(none)')")"

# ─── Build full payload ────────────────────────────────────────────────────────
PAYLOAD=$(python3 - <<PYEOF
import json, os
print(json.dumps({
    "devices": json.loads(os.environ["DEVICES_JSON"]),
    "parameters": json.loads(os.environ["PARAMS_JSON"]),
    "options": {
        "timeout_per_device": int(os.environ["TIMEOUT_PER_DEVICE"]),
        "max_workers":        int(os.environ["MAX_WORKERS"]),
    },
    "incident": os.environ["INCIDENT"],
}))
PYEOF
)

# ─── Submit workflow job ───────────────────────────────────────────────────────
echo "[netorch-workflow] Submitting workflow job..."

RESPONSE=$(curl -sf \
    -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -d "$PAYLOAD" \
    "$NETORCH_API/workflows/$WORKFLOW_NAME/run") || {
    echo "[netorch-workflow] ERROR: Failed to reach netorch API at $NETORCH_API" >&2
    exit 1
}

JOB_ID=$(echo "$RESPONSE" | python3 -c \
    "import sys,json; print(json.loads(sys.stdin.read())['job_id'])") || {
    echo "[netorch-workflow] ERROR: Unexpected API response: $RESPONSE" >&2
    exit 1
}
DEVICE_COUNT=$(echo "$RESPONSE" | python3 -c \
    "import sys,json; print(json.loads(sys.stdin.read()).get('device_count','?'))")

echo "[netorch-workflow] Job submitted: $JOB_ID (targeting $DEVICE_COUNT device(s))"

# ─── Poll for completion ───────────────────────────────────────────────────────
echo "[netorch-workflow] Polling for job completion..."

POLLS=0
FINAL_STATUS=""
while [[ $POLLS -lt $MAX_POLL ]]; do
    sleep "$POLL_INTERVAL"
    POLLS=$((POLLS + 1))

    STATUS_RESP=$(curl -sf \
        -H "Authorization: Bearer $AUTH_TOKEN" \
        "$NETORCH_API/jobs/$JOB_ID") || {
        echo "[netorch-workflow] WARNING: Poll $POLLS failed. Retrying..." >&2
        continue
    }

    JOB_STATUS=$(echo "$STATUS_RESP" | python3 -c \
        "import sys,json; print(json.loads(sys.stdin.read())['status'])")
    SUMMARY=$(echo "$STATUS_RESP" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
s = d.get('summary', {})
print(f\"total={s.get('total',0)} success={s.get('success',0)} failed={s.get('failed',0)}\")
")

    echo "[netorch-workflow] [$POLLS] Status: $JOB_STATUS | $SUMMARY"

    case "$JOB_STATUS" in
        completed|partial_failure|failed|cancelled)
            FINAL_STATUS="$JOB_STATUS"
            break ;;
        queued|running) ;;
        *) echo "[netorch-workflow] WARNING: Unknown status '$JOB_STATUS'." >&2 ;;
    esac
done

if [[ -z "$FINAL_STATUS" ]]; then
    echo "[netorch-workflow] ERROR: Job $JOB_ID did not complete within $((MAX_POLL * POLL_INTERVAL))s." >&2
    exit 1
fi

# ─── Save result log ───────────────────────────────────────────────────────────
LOG_DIR="$LOGS_BASE/$INCIDENT"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$JOB_ID.json"

if curl -sf \
        -H "Authorization: Bearer $AUTH_TOKEN" \
        "$NETORCH_API/logs/$JOB_ID" \
        -o "$LOG_FILE"; then
    echo "[netorch-workflow] Result log saved: $LOG_FILE"
else
    echo "[netorch-workflow] WARNING: Could not save result log for $JOB_ID" >&2
fi

# ─── Exit with correct code ────────────────────────────────────────────────────
case "$FINAL_STATUS" in
    completed)
        echo "[netorch-workflow] ✓ Workflow job completed successfully."
        exit 0 ;;
    partial_failure)
        echo "[netorch-workflow] ⚠ Workflow completed with partial failures. ($SUMMARY)" >&2
        exit 1 ;;
    failed|cancelled)
        echo "[netorch-workflow] ✗ Workflow job $FINAL_STATUS. ($SUMMARY)" >&2
        exit 1 ;;
esac
