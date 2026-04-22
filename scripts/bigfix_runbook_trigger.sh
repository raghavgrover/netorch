#!/usr/bin/env bash
# =============================================================================
# bigfix_runbook_trigger.sh — BigFix trigger for netorch runbook execution.
#
# Usage (from a BigFix Fixlet action script):
#   bash /opt/netorch/scripts/bigfix_runbook_trigger.sh \
#       --runbook   job_acl_update.sh \
#       --devices   "cisco_switches" \
#       --incident  INC12345
#
# Multiple devices/groups (comma-separated or newline-separated):
#   bash /opt/netorch/scripts/bigfix_runbook_trigger.sh \
#       --runbook   job_acl_update.sh \
#       --devices   "cisco_switches,core_routers" \
#       --incident  INC12345
#
# The runbook must exist in /opt/netorch/runbooks/ on the relay server.
# Non-blank, non-comment lines from the runbook are submitted as commands
# in audit mode to all targeted devices.
#
# The job result log is saved under:
#   /opt/netorch/logs/jobs/<incident>/<job_id>.json
#
# Exit codes: 0=success, 1=error
# =============================================================================
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
NETORCH_API="http://localhost:64321"
NETORCH_CONF="/opt/netorch/netorch.toml"
RUNBOOKS_DIR="/opt/netorch/runbooks"
LOGS_BASE="/opt/netorch/logs/jobs"
POLL_INTERVAL=5    # seconds between status polls
MAX_POLL=360       # max polls before giving up (360 * 5s = 30 min)

# ─── Parse arguments ─────────────────────────────────────────────────────────
RUNBOOK_NAME=""
DEVICES_RAW=""
INCIDENT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runbook)   RUNBOOK_NAME="$2"; shift 2 ;;
        --devices)   DEVICES_RAW="$2";  shift 2 ;;
        --incident)  INCIDENT="$2";     shift 2 ;;
        *) echo "[netorch-runbook] WARNING: Unknown argument: $1" >&2; shift ;;
    esac
done

# ─── Validate required args ───────────────────────────────────────────────────
if [[ -z "$RUNBOOK_NAME" ]]; then
    echo "[netorch-runbook] ERROR: --runbook is required." >&2
    exit 1
fi
if [[ -z "$DEVICES_RAW" ]]; then
    echo "[netorch-runbook] ERROR: --devices is required." >&2
    exit 1
fi
if [[ -z "$INCIDENT" ]]; then
    echo "[netorch-runbook] ERROR: --incident is required." >&2
    exit 1
fi

# ─── Resolve auth token from netorch.toml ─────────────────────────────────────
if [[ ! -f "$NETORCH_CONF" ]]; then
    echo "[netorch-runbook] ERROR: netorch config not found at $NETORCH_CONF" >&2
    exit 1
fi
AUTH_TOKEN=$(grep -E '^\s*auth_token\s*=' "$NETORCH_CONF" \
    | head -1 | sed 's/.*=\s*"//' | sed 's/".*//')
if [[ -z "$AUTH_TOKEN" ]]; then
    echo "[netorch-runbook] ERROR: Could not parse auth_token from $NETORCH_CONF" >&2
    exit 1
fi

# ─── Verify runbook exists ────────────────────────────────────────────────────
RUNBOOK_PATH="$RUNBOOKS_DIR/$RUNBOOK_NAME"
if [[ ! -f "$RUNBOOK_PATH" ]]; then
    echo "[netorch-runbook] ERROR: Runbook not found: $RUNBOOK_PATH" >&2
    exit 1
fi
echo "[netorch-runbook] Runbook: $RUNBOOK_NAME"

# ─── Normalise devices (convert literal \n to real newlines, split on comma/newline) ──
DEVICES_RAW="${DEVICES_RAW//$'\\n'/$'\n'}"
DEVICES_RAW="${DEVICES_RAW//,/$'\n'}"

# Build JSON array of DeviceEntry objects
DEVICES_JSON=""
while IFS= read -r device; do
    device="$(echo "$device" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$device" ]] && continue
    # Detect IP address vs group name
    if echo "$device" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(:[0-9]+)?$'; then
        ENTRY="{\"host\":\"$device\"}"
    else
        ENTRY="{\"group\":\"$device\"}"
    fi
    DEVICES_JSON="${DEVICES_JSON:+$DEVICES_JSON,}$ENTRY"
done <<< "$DEVICES_RAW"

if [[ -z "$DEVICES_JSON" ]]; then
    echo "[netorch-runbook] ERROR: No valid devices parsed from: $DEVICES_RAW" >&2
    exit 1
fi

# ─── Submit the runbook job via /runbooks/{name}/run ──────────────────────────
PAYLOAD="{\"devices\":[$DEVICES_JSON]}"

echo "[netorch-runbook] Incident:  $INCIDENT"
echo "[netorch-runbook] Submitting runbook job to netorch API…"
echo "[netorch-runbook] Devices JSON: $DEVICES_JSON"

RESPONSE=$(curl -sf \
    -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    -d "$PAYLOAD" \
    "$NETORCH_API/runbooks/$RUNBOOK_NAME/run") || {
    echo "[netorch-runbook] ERROR: Failed to reach netorch API at $NETORCH_API" >&2
    exit 1
}

JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['job_id'])") || {
    echo "[netorch-runbook] ERROR: Unexpected API response: $RESPONSE" >&2
    exit 1
}
DEVICE_COUNT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('device_count','?'))")

echo "[netorch-runbook] Job submitted: $JOB_ID (targeting $DEVICE_COUNT device(s))"

# ─── Poll for completion ───────────────────────────────────────────────────────
echo "[netorch-runbook] Polling for job completion…"

POLLS=0
FINAL_STATUS=""
while [[ $POLLS -lt $MAX_POLL ]]; do
    sleep "$POLL_INTERVAL"
    POLLS=$((POLLS + 1))

    STATUS_RESP=$(curl -sf \
        -H "Authorization: Bearer $AUTH_TOKEN" \
        "$NETORCH_API/jobs/$JOB_ID") || {
        echo "[netorch-runbook] WARNING: Poll $POLLS failed (API unreachable). Retrying…" >&2
        continue
    }

    JOB_STATUS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['status'])")
    SUMMARY=$(echo "$STATUS_RESP" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
s = d.get('summary', {})
print(f\"total={s.get('total',0)} success={s.get('success',0)} failed={s.get('failed',0)}\")
")

    echo "[netorch-runbook] [$POLLS] Status: $JOB_STATUS | $SUMMARY"

    case "$JOB_STATUS" in
        completed|partial_failure|failed|cancelled)
            FINAL_STATUS="$JOB_STATUS"
            break
            ;;
        queued|running)
            ;;  # keep polling
        *)
            echo "[netorch-runbook] WARNING: Unknown job status '$JOB_STATUS'. Continuing to poll." >&2
            ;;
    esac
done

if [[ -z "$FINAL_STATUS" ]]; then
    echo "[netorch-runbook] ERROR: Job $JOB_ID did not complete within $((MAX_POLL * POLL_INTERVAL)) seconds." >&2
    exit 1
fi

# ─── Save result log under incident directory ──────────────────────────────────
LOG_DIR="$LOGS_BASE/$INCIDENT"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$JOB_ID.json"

if curl -sf \
        -H "Authorization: Bearer $AUTH_TOKEN" \
        "$NETORCH_API/logs/$JOB_ID" \
        -o "$LOG_FILE"; then
    echo "[netorch-runbook] Result log saved: $LOG_FILE"
else
    echo "[netorch-runbook] WARNING: Could not save result log for job $JOB_ID" >&2
fi

# ─── Exit with correct code ────────────────────────────────────────────────────
case "$FINAL_STATUS" in
    completed)
        echo "[netorch-runbook] ✓ Runbook job completed successfully."
        exit 0
        ;;
    partial_failure)
        echo "[netorch-runbook] ⚠ Runbook job completed with partial failures. ($SUMMARY)" >&2
        exit 1
        ;;
    failed|cancelled)
        echo "[netorch-runbook] ✗ Runbook job $FINAL_STATUS. ($SUMMARY)" >&2
        exit 1
        ;;
esac
