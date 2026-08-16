#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:?usage: restore.sh BACKUP_DIR}"
psql "${ACQ_DATABASE_URL:-postgresql://acq:acq@localhost:5432/acq}" < "$backup_dir/acq.sql"
tar -xzf "$backup_dir/documents.tgz"
