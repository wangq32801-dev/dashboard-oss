#!/usr/bin/env bash
set -u

DASH_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATUS_FILE="${DASH_DIR}/.weekly-draft-job.json"
STARTED_AT="$(date -Iseconds)"
RESPONSE_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_FILE"' EXIT

if /usr/bin/curl --fail --silent --show-error --max-time 120 \
  --request POST --header 'Content-Type: application/json' --data '{}' \
  http://127.0.0.1:8787/api/review/draft/refresh >"$RESPONSE_FILE"; then
  RESULT="success"
  EXIT_CODE=0
else
  EXIT_CODE=$?
  RESULT="failed"
fi

FINISHED_AT="$(date -Iseconds)"
STATUS_TMP="${STATUS_FILE}.tmp"
printf '{"started_at":"%s","finished_at":"%s","status":"%s","exit_code":%d}\n' \
  "$STARTED_AT" "$FINISHED_AT" "$RESULT" "$EXIT_CODE" >"$STATUS_TMP"
mv "$STATUS_TMP" "$STATUS_FILE"
exit "$EXIT_CODE"
