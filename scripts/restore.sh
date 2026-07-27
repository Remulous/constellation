#!/bin/sh
set -eu
data_dir="${CRM_DATA_DIR:-/data}"
source_db="${1:?Usage: restore.sh /path/to/backup.db}"
target="$data_dir/constellation.db"
test -f "$source_db" || { echo "Backup not found: $source_db" >&2; exit 1; }
python - "$source_db" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    raise SystemExit(f"Backup integrity check failed: {result}")
PY
if [ -f "$target" ]; then
  cp "$target" "$target.before-restore"
fi
cp "$source_db" "$target"
echo "Restored $target (restart the application before use)"

