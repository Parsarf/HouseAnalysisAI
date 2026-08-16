#!/usr/bin/env bash
# Restore a backup made by backup.sh: replay the SQL dump and extract the
# documents tarball into an explicit target directory (never the cwd).
# usage: restore.sh BACKUP_DIR TARGET_DIR
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: restore.sh BACKUP_DIR TARGET_DIR" >&2
  exit 2
fi

backup_dir="$1"
target_dir="$2"
db_url="${ACQ_DATABASE_URL:-postgresql://acq:acq@localhost:5432/acq}"
db_url="${db_url/postgresql+psycopg:\/\//postgresql:\/\/}"

for cmd in psql tar; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "restore.sh: missing required command: $cmd" >&2; exit 1; }
done

[[ -d "$backup_dir" ]] || { echo "restore.sh: no such backup directory: $backup_dir" >&2; exit 1; }
[[ -f "$backup_dir/acq.sql" ]] || { echo "restore.sh: missing $backup_dir/acq.sql" >&2; exit 1; }

# Never untar into the current working directory: require an explicit,
# distinct target directory.
case "$target_dir" in
  ""|.|./)
    echo "restore.sh: refusing to restore into the current working directory" >&2
    exit 1
    ;;
esac
created_target=0
if [[ ! -d "$target_dir" ]]; then
  mkdir -p "$target_dir"
  created_target=1
fi
target_abs="$(cd "$target_dir" && pwd -P)"
if [[ "$target_abs" == "$(pwd -P)" ]]; then
  echo "restore.sh: refusing to restore into the current working directory" >&2
  [[ "$created_target" == "1" ]] && rmdir "$target_dir" 2>/dev/null
  exit 1
fi

# Refuse archives with absolute paths or parent-directory escapes.
if [[ -f "$backup_dir/documents.tgz" ]]; then
  if tar -tzf "$backup_dir/documents.tgz" | grep -qE '^/|(^|/)\.\.(/|$)'; then
    echo "restore.sh: refusing to extract archive with unsafe paths" >&2
    exit 1
  fi
fi

psql "$db_url" < "$backup_dir/acq.sql"

if [[ -f "$backup_dir/documents.tgz" ]]; then
  tar -xzf "$backup_dir/documents.tgz" -C "$target_abs"
fi

echo "restored database and documents to $target_abs"
