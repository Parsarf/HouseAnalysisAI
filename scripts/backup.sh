#!/usr/bin/env bash
# Nightly backup: pg_dump of the database + tarball of the documents folder.
# usage: backup.sh [DEST_DIR]   (default: backups/<UTC timestamp>)
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "usage: backup.sh [DEST_DIR]" >&2
  exit 0
fi

destination="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
db_url="${ACQ_DATABASE_URL:-postgresql://acq:acq@localhost:5432/acq}"
# settings use the SQLAlchemy form; pg_dump needs a plain postgres URL
db_url="${db_url/postgresql+psycopg:\/\//postgresql:\/\/}"
documents_dir="${ACQ_DOCUMENT_ROOT:-documents}"

for cmd in pg_dump tar; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "backup.sh: missing required command: $cmd" >&2; exit 1; }
done

if [[ -e "$destination" && -n "$(ls -A "$destination" 2>/dev/null || true)" ]]; then
  echo "backup.sh: refusing to write into non-empty directory: $destination" >&2
  exit 1
fi
mkdir -p "$destination"

tmp_sql="$destination/acq.sql.tmp"
pg_dump "$db_url" > "$tmp_sql"
if [[ ! -s "$tmp_sql" ]]; then
  echo "backup.sh: pg_dump produced no output; aborting" >&2
  rm -f "$tmp_sql"
  exit 1
fi
mv "$tmp_sql" "$destination/acq.sql"

if [[ -d "$documents_dir" ]]; then
  tar -czf "$destination/documents.tgz" -C "$(dirname "$documents_dir")" "$(basename "$documents_dir")"
else
  echo "backup.sh: warning: documents directory '$documents_dir' not found; skipping" >&2
fi

echo "$destination"
