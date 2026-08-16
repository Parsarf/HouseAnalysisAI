#!/usr/bin/env bash
set -euo pipefail

destination="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$destination"
pg_dump "${ACQ_DATABASE_URL:-postgresql://acq:acq@localhost:5432/acq}" > "$destination/acq.sql"
tar -czf "$destination/documents.tgz" documents
echo "$destination"
