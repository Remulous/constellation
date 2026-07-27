#!/bin/sh
set -eu
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
