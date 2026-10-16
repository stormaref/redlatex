#!/bin/sh
# Start as root only to make the data directory (e.g. a mounted Render disk) writable, then drop privileges.
set -e
mkdir -p "$REDLATEX_DATA_DIR"
chown app:app "$REDLATEX_DATA_DIR" 2>/dev/null || true
exec setpriv --reuid=app --regid=app --init-groups "$@"
