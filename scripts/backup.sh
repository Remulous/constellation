#!/bin/sh
set -eu
data_dir="${CRM_DATA_DIR:-/data}"
backup_dir="${1:-$data_dir/backups}"
database="$data_dir/constellation.db"
test -f "$database" || { echo "Database not found: $database" >&2; exit 1; }
mkdir -p "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$backup_dir/constellation-$stamp.db"
python - "$database" "$destination" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
print(sys.argv[2])
PY

