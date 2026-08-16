#!/usr/bin/env bash
set -euo pipefail

: "${ACQ_BASE_URL:?Set ACQ_BASE_URL to the public API URL}"
: "${ACQ_PASSWORD:?Set ACQ_PASSWORD to the configured application password}"
: "${ACQ_PDF:?Set ACQ_PDF to a real local PDF path}"

cookie_file="${TMPDIR:-/tmp}/acq-smoke-cookie.$$"
trap 'rm -f "$cookie_file"' EXIT

curl -fsS "$ACQ_BASE_URL/healthz" | grep -q '"ok"\|"status"'
curl -fsS "$ACQ_BASE_URL/readyz" | grep -q 'ready'
curl -fsS -c "$cookie_file" -X POST "$ACQ_BASE_URL/api/auth/login" \
  -F "password=$ACQ_PASSWORD" -F 'read_only=false' | grep -q '"ok"'
curl -fsS -b "$cookie_file" "$ACQ_BASE_URL/api/me" | grep -q '"id"'

upload_json="$(curl -fsS -b "$cookie_file" -c "$cookie_file" \
  -F "files=@$ACQ_PDF;type=application/pdf" \
  "$ACQ_BASE_URL/api/uploads")"
batch_id="$(printf '%s' "$upload_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')"

curl -fsS -b "$cookie_file" -X POST "$ACQ_BASE_URL/api/batches/$batch_id/estimate" \
  | grep -q 'estimated_cost_usd'
curl -fsS -b "$cookie_file" -X POST "$ACQ_BASE_URL/api/batches/$batch_id/start" \
  | grep -q 'running\|uploaded'

echo "ACQ smoke test passed through upload, estimate, and start for batch $batch_id"
