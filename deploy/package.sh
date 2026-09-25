#!/bin/bash
# Build a clean archive of Claude Control for another account on this server:
# code + docs only - no .env (token), no data/ (registry), no venv, no test leftovers.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="claude-control.tar.gz"   # fixed name: instructions and updates always use the same path
mkdir -p dist
tar -C "$HOME" -czf "dist/$NAME" \
  --exclude='claude-control/.env' --exclude='claude-control/data' --exclude='claude-control/.venv' --exclude='claude-control/.venv-voice' \
  --exclude='__pycache__' --exclude='claude-control/tests/work' --exclude='claude-control/tests/report.json' \
  --exclude='claude-control/tests/last-run.log' --exclude='claude-control/dist' --exclude='claude-control/docs' \
  claude-control
if tar -xzOf "dist/$NAME" 2>/dev/null | grep -qE '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'; then
  echo "Refusing: the archive contains something that looks like a bot token"; rm -f "dist/$NAME"; exit 1
fi
install -m 644 "dist/$NAME" "/tmp/$NAME"   # /tmp is readable by every account on the server
echo "Archive: /tmp/$NAME (copy also in dist/)"
tar -tzf "dist/$NAME" | sed 's/^/  /'
